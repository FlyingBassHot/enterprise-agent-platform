from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.modules.agent.service import AgentService
from app.modules.knowledge.service import KnowledgeService
from app.modules.model.service import ModelService
from app.modules.permission.service import PermissionService
from app.modules.tool.service import ToolService
from app.modules.workflow.exceptions import (
    WorkflowDisabledError,
    WorkflowNotPublishedError,
)
from app.modules.workflow.models import Workflow, WorkflowExecution, WorkflowVersion
from app.modules.workflow.repository import WorkflowRepository
from app.modules.workflow.schemas import (
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowOut,
    WorkflowSnapshot,
    WorkflowUpdate,
    WorkflowValidationResult,
    WorkflowVersionOut,
    validate_definition,
)

_HIGH_RISK_LEVELS = {"high", "critical"}


class WorkflowService:
    """Versioned workflow definitions with publish-time binding validation."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = WorkflowRepository(db)

    # -------------------------------------------------------------------- CRUD

    async def create_workflow(
        self, actor: UserContext, payload: WorkflowCreate
    ) -> WorkflowOut:
        await PermissionService(self.db).require_permission(actor, "workflows:write")
        if await self.repo.get_workflow_by_name(payload.name):
            raise ConflictError("Workflow name already exists")
        workflow = Workflow(
            name=payload.name,
            description=payload.description,
            status="draft",
            owner_id=actor.user_id,
            current_version=0,
            draft_definition=payload.definition.model_dump(mode="json"),
        )
        self.db.add(workflow)
        await self.db.commit()
        await self.db.refresh(workflow)
        return await self._out(workflow)

    async def list_workflows(self, actor: UserContext) -> list[WorkflowOut]:
        await PermissionService(self.db).require_permission(actor, "workflows:read")
        return [await self._out(workflow) for workflow in await self.repo.list_workflows()]

    async def get_workflow(self, actor: UserContext, workflow_id: str) -> WorkflowOut:
        await PermissionService(self.db).require_permission(actor, "workflows:read")
        return await self._out(await self._get_or_404(workflow_id))

    async def update_workflow(
        self, actor: UserContext, workflow_id: str, payload: WorkflowUpdate
    ) -> WorkflowOut:
        workflow = await self._get_authorized(
            workflow_id, actor, "workflows:write", "write"
        )
        if payload.name is not None and payload.name != workflow.name:
            if await self.repo.get_workflow_by_name(payload.name):
                raise ConflictError("Workflow name already exists")
            workflow.name = payload.name
        if payload.description is not None:
            workflow.description = payload.description
        if payload.definition is not None:
            workflow.draft_definition = payload.definition.model_dump(mode="json")
        await self.db.commit()
        await self.db.refresh(workflow)
        return await self._out(workflow)

    async def delete_workflow(self, actor: UserContext, workflow_id: str) -> None:
        workflow = await self._get_authorized(
            workflow_id, actor, "workflows:write", "write"
        )
        if workflow.status == "published":
            raise ConflictError(
                "Published workflows cannot be deleted; disable it first"
            )
        await self.db.delete(workflow)
        await self.db.commit()

    async def list_versions(
        self, actor: UserContext, workflow_id: str
    ) -> list[WorkflowVersionOut]:
        await PermissionService(self.db).require_permission(actor, "workflows:read")
        await self._get_or_404(workflow_id)
        return [
            self._version_out(version)
            for version in await self.repo.list_versions(workflow_id)
        ]

    # ---------------------------------------------------------------- lifecycle

    async def validate(
        self, actor: UserContext, workflow_id: str
    ) -> WorkflowValidationResult:
        await PermissionService(self.db).require_permission(actor, "workflows:read")
        workflow = await self._get_or_404(workflow_id)
        return await self._validate_workflow(workflow, actor)

    async def publish(self, actor: UserContext, workflow_id: str) -> WorkflowOut:
        workflow = await self._get_authorized(
            workflow_id, actor, "workflows:publish", "publish"
        )
        result = await self._validate_workflow(workflow, actor)
        if not result.valid:
            raise ValidationError(
                "Workflow definition is invalid",
                details={"errors": result.errors, "warnings": result.warnings},
            )
        definition = WorkflowDefinition.model_validate(workflow.draft_definition)
        version_number = await self.repo.next_version_number(workflow.id)
        now = datetime.now(UTC)
        version = WorkflowVersion(
            workflow_id=workflow.id,
            version=version_number,
            definition=definition.model_dump(mode="json"),
            status="published",
            created_by=actor.user_id,
            published_at=now,
        )
        self.db.add(version)
        await self.db.flush()
        workflow.current_version_id = version.id
        workflow.current_version = version_number
        workflow.status = "published"
        await self.db.commit()
        await self.db.refresh(workflow)
        return await self._out(workflow)

    async def disable(self, actor: UserContext, workflow_id: str) -> WorkflowOut:
        workflow = await self._get_authorized(
            workflow_id, actor, "workflows:publish", "publish"
        )
        workflow.status = "disabled"
        await self.db.commit()
        await self.db.refresh(workflow)
        return await self._out(workflow)

    # ----------------------------------------------------------------- loaders

    async def validate_agent_binding(
        self, workflow_id: str, actor: UserContext
    ) -> WorkflowSnapshot:
        permission_service = PermissionService(self.db)
        await permission_service.require_permission(actor, "workflows:read")
        workflow = await self._get_or_404(workflow_id)
        if not await permission_service.can_access_resource(
            actor,
            resource_type="workflow",
            resource_id=workflow.id,
            action="read",
            owner_id=workflow.owner_id,
        ):
            raise PermissionDeniedError(
                f"No read access to workflow {workflow.id}"
            )
        version = await self._require_published_version(workflow)
        return self._snapshot(workflow, version)

    async def load_published(self, workflow_id: str) -> WorkflowSnapshot:
        workflow = await self._get_or_404(workflow_id)
        version = await self._require_published_version(workflow)
        return self._snapshot(workflow, version)

    async def load_version(
        self, workflow_id: str, version_id: str
    ) -> WorkflowSnapshot:
        workflow = await self._get_or_404(workflow_id)
        version = await self.repo.get_version(version_id)
        if version is None or version.workflow_id != workflow.id:
            raise NotFoundError("Workflow version not found")
        return self._snapshot(workflow, version)

    # -------------------------------------------------------------- executions

    async def record_execution(
        self,
        *,
        workflow_id: str,
        user_id: str,
        workflow_version_id: str | None = None,
        execution_id: str | None = None,
        status: str = "running",
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> str:
        """Insert a workflow_executions row and return its id.

        ``workflow_version_id`` defaults to the workflow's current version.
        """
        workflow = await self._get_or_404(workflow_id)
        version_id = workflow_version_id or workflow.current_version_id
        if not version_id:
            raise ValidationError("Workflow has no version to execute")
        version = await self.repo.get_version(version_id)
        if version is None or version.workflow_id != workflow.id:
            raise NotFoundError("Workflow version not found")
        if finished_at is None and status in {"succeeded", "failed", "cancelled"}:
            finished_at = datetime.now(UTC)
        execution = WorkflowExecution(
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            execution_id=execution_id,
            user_id=user_id,
            status=status,
            input=dict(input or {}),
            output=output,
            error=error,
            finished_at=finished_at,
        )
        self.repo.add_execution(execution)
        await self.db.commit()
        await self.db.refresh(execution)
        return execution.id

    async def update_execution(
        self,
        execution_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        row = await self.repo.get_execution_by_execution_id(execution_id)
        if row is None:
            return
        row.status = status
        if output is not None:
            row.output = output
        if error is not None:
            row.error = error
        if finished_at is not None:
            row.finished_at = finished_at
        await self.db.commit()

    # ----------------------------------------------------------------- helpers

    async def _validate_workflow(
        self, workflow: Workflow, actor: UserContext
    ) -> WorkflowValidationResult:
        try:
            definition = WorkflowDefinition.model_validate(workflow.draft_definition)
        except PydanticValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors()
            ]
            return WorkflowValidationResult(valid=False, errors=errors, warnings=[])
        result = validate_definition(definition)
        errors = list(result.errors)
        if errors:
            return WorkflowValidationResult(
                valid=False, errors=errors, warnings=list(result.warnings)
            )
        errors = await self._binding_errors(definition, actor)
        return WorkflowValidationResult(
            valid=not errors, errors=errors, warnings=list(result.warnings)
        )

    async def _binding_errors(
        self, definition: WorkflowDefinition, actor: UserContext
    ) -> list[str]:
        tool_service = ToolService(self.db)
        agent_service = AgentService(self.db)
        knowledge_service = KnowledgeService(self.db)
        model_service = ModelService(self.db)
        errors: list[str] = []

        for node in definition.nodes:
            if node.type == "tool":
                tool_name = node.config.get("tool_name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                try:
                    descriptors = await tool_service.describe([tool_name], actor)
                except AppError as exc:
                    errors.append(f"tool node {node.id}: {exc.message}")
                else:
                    if not any(item.name == tool_name for item in descriptors):
                        errors.append(
                            f"tool node {node.id}: tool {tool_name} is unavailable or "
                            "not permitted"
                        )
            elif node.type == "agent":
                agent_id = node.config.get("agent_id")
                if not isinstance(agent_id, str) or not agent_id:
                    continue
                try:
                    snapshot = await agent_service.load_published(agent_id)
                except AppError as exc:
                    errors.append(f"agent node {node.id}: {exc.message}")
                    continue
                if snapshot.mode != "agent":
                    errors.append(
                        f"agent node {node.id}: agent {snapshot.agent_name} runs in "
                        f"'{snapshot.mode}' mode; only plain agents can be workflow nodes"
                    )
                    continue
                errors.extend(
                    await self._agent_tool_errors(node.id, snapshot.tool_names, actor)
                )
            elif node.type == "knowledge":
                base_ids = node.config.get("knowledge_base_ids")
                if not isinstance(base_ids, list) or not base_ids:
                    continue
                try:
                    await knowledge_service.validate_bindings(
                        [str(base) for base in base_ids], actor
                    )
                except AppError as exc:
                    errors.append(f"knowledge node {node.id}: {exc.message}")
            elif node.type == "llm":
                config_id = node.config.get("model_config_id")
                try:
                    await model_service.resolve(
                        config_id
                        if isinstance(config_id, str) and config_id
                        else None
                    )
                except AppError as exc:
                    errors.append(f"llm node {node.id}: {exc.message}")
        return errors

    async def _agent_tool_errors(
        self, node_id: str, tool_names: list[str], actor: UserContext
    ) -> list[str]:
        if not tool_names:
            return []
        try:
            descriptors = await ToolService(self.db).describe(tool_names, actor)
        except AppError as exc:
            return [f"agent node {node_id}: {exc.message}"]
        found = {descriptor.name for descriptor in descriptors}
        missing = [name for name in tool_names if name not in found]
        errors = [
            f"agent node {node_id}: agent tool {name} is unavailable or not permitted"
            for name in missing
        ]
        risky = [
            descriptor.name
            for descriptor in descriptors
            if descriptor.risk_level in _HIGH_RISK_LEVELS or descriptor.requires_approval
        ]
        if risky:
            errors.append(
                f"agent node {node_id}: agent tools require approval and cannot pause "
                "inside a workflow: " + ", ".join(sorted(risky))
            )
        return errors

    async def _require_published_version(self, workflow: Workflow) -> WorkflowVersion:
        if workflow.status == "disabled":
            raise WorkflowDisabledError(
                f"Workflow {workflow.name} is disabled"
            )
        if workflow.status != "published":
            raise WorkflowNotPublishedError(
                f"Workflow {workflow.name} is not published (status={workflow.status})"
            )
        if not workflow.current_version_id:
            raise WorkflowNotPublishedError(
                f"Workflow {workflow.name} has no published version"
            )
        version = await self.repo.get_version(workflow.current_version_id)
        if version is None:
            raise WorkflowNotPublishedError("Published workflow version is missing")
        return version

    def _snapshot(
        self, workflow: Workflow, version: WorkflowVersion
    ) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            owner_id=workflow.owner_id,
            version_id=version.id,
            version=version.version,
            definition=WorkflowDefinition.model_validate(version.definition),
        )

    async def _get_or_404(self, workflow_id: str) -> Workflow:
        workflow = await self.repo.get_workflow(workflow_id)
        if workflow is None:
            raise NotFoundError("Workflow not found")
        return workflow

    async def _get_authorized(
        self,
        workflow_id: str,
        actor: UserContext,
        code: str,
        action: str,
    ) -> Workflow:
        permission_service = PermissionService(self.db)
        await permission_service.require_permission(actor, code)
        workflow = await self._get_or_404(workflow_id)
        if not await permission_service.can_access_resource(
            actor,
            resource_type="workflow",
            resource_id=workflow.id,
            action=action,
            owner_id=workflow.owner_id,
        ):
            raise PermissionDeniedError(
                f"No {action} access to workflow {workflow.id}"
            )
        return workflow

    async def _out(self, workflow: Workflow) -> WorkflowOut:
        published_at: str | None = None
        if workflow.current_version_id:
            version = await self.repo.get_version(workflow.current_version_id)
            if version is not None and version.published_at is not None:
                published_at = version.published_at.isoformat()
        return WorkflowOut(
            id=workflow.id,
            name=workflow.name,
            description=workflow.description,
            status=workflow.status,
            owner_id=workflow.owner_id,
            current_version=workflow.current_version,
            current_version_id=workflow.current_version_id,
            definition=WorkflowDefinition.model_validate(workflow.draft_definition),
            published_at=published_at,
            created_at=workflow.created_at.isoformat(),
            updated_at=workflow.updated_at.isoformat(),
        )

    def _version_out(self, version: WorkflowVersion) -> WorkflowVersionOut:
        return WorkflowVersionOut(
            id=version.id,
            workflow_id=version.workflow_id,
            version=version.version,
            definition=version.definition,
            status=version.status,
            created_by=version.created_by,
            published_at=(
                version.published_at.isoformat() if version.published_at else None
            ),
            created_at=version.created_at.isoformat(),
        )
