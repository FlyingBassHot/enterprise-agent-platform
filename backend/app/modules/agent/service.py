from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import AgentSnapshot, ToolDescriptor, UserContext
from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import new_id
from app.modules.agent.models import Agent, AgentVersion
from app.modules.agent.repository import AgentRepository
from app.modules.agent.schemas import (
    AgentConfig,
    AgentCreate,
    AgentOut,
    AgentUpdate,
    AgentValidationResult,
    AgentVersionOut,
)
from app.modules.permission.service import PermissionService

ToolCatalog = Callable[[Sequence[str], UserContext], Awaitable[Sequence[ToolDescriptor]]]


class AgentService:
    def __init__(self, db: AsyncSession, *, tool_catalog: ToolCatalog | None = None) -> None:
        self.db = db
        self.repo = AgentRepository(db)
        self._tool_catalog: ToolCatalog = tool_catalog or self._default_tool_catalog

    # -------------------------------------------------------------------- CRUD

    async def list_agents(self, actor: UserContext) -> list[AgentOut]:
        await PermissionService(self.db).require_permission(actor, "agents:read")
        return [await self._agent_out(agent) for agent in await self.repo.list_agents()]

    async def get_agent(self, agent_id: str, actor: UserContext) -> AgentOut:
        await PermissionService(self.db).require_permission(actor, "agents:read")
        agent = await self._get_agent_or_404(agent_id)
        return await self._agent_out(agent)

    async def create_agent(self, payload: AgentCreate, actor: UserContext) -> AgentOut:
        await PermissionService(self.db).require_permission(actor, "agents:write")
        if await self.repo.get_agent_by_name(payload.name):
            raise ConflictError("Agent name already exists")
        agent = Agent(
            name=payload.name,
            description=payload.description,
            status="draft",
            owner_id=actor.user_id,
            draft_config=payload.config.model_dump(mode="json"),
        )
        self.db.add(agent)
        await self.db.commit()
        await self.db.refresh(agent)
        return await self._agent_out(agent)

    async def update_agent(
        self, agent_id: str, payload: AgentUpdate, actor: UserContext
    ) -> AgentOut:
        agent = await self._get_authorized(agent_id, actor, "agents:write", "write")
        if payload.name is not None and payload.name != agent.name:
            if await self.repo.get_agent_by_name(payload.name):
                raise ConflictError("Agent name already exists")
            agent.name = payload.name
        if payload.description is not None:
            agent.description = payload.description
        if payload.config is not None:
            agent.draft_config = payload.config.model_dump(mode="json")
        await self.db.commit()
        await self.db.refresh(agent)
        return await self._agent_out(agent)

    async def delete_agent(self, agent_id: str, actor: UserContext) -> None:
        agent = await self._get_authorized(agent_id, actor, "agents:write", "write")
        await self.db.delete(agent)
        await self.db.commit()

    # ---------------------------------------------------------------- lifecycle

    async def validate(self, agent_id: str, actor: UserContext) -> AgentValidationResult:
        await PermissionService(self.db).require_permission(actor, "agents:read")
        agent = await self._get_agent_or_404(agent_id)
        return await self._collect_validation(agent, actor)

    async def publish(self, agent_id: str, actor: UserContext) -> AgentOut:
        agent = await self._get_authorized(agent_id, actor, "agents:publish", "publish")
        result = await self._collect_validation(agent, actor)
        if not result.valid:
            raise ValidationError(
                "Agent configuration is invalid",
                details={"errors": result.errors, "warnings": result.warnings},
            )
        config = AgentConfig.model_validate(agent.draft_config)
        resolved = await self._prompt_service().resolve(
            config.prompt.prompt_id, config.prompt.version_id, config.prompt.variables
        )
        tool_descriptors = (
            await self._tool_catalog(config.tools, actor) if config.tools else []
        )
        tool_versions = {
            descriptor.name: descriptor.version for descriptor in tool_descriptors
        }
        workflow_snapshot = None
        if config.execution.mode in {"workflow", "hybrid"}:
            from app.modules.workflow.service import WorkflowService

            workflow_snapshot = await WorkflowService(self.db).validate_agent_binding(
                config.execution.workflow_id, actor  # type: ignore[arg-type]
            )
        now = datetime.now(UTC)
        version_number = await self.repo.next_version_number(agent.id)
        version_id = new_id()
        snapshot = AgentSnapshot(
            agent_id=agent.id,
            agent_name=agent.name,
            version_id=version_id,
            version=version_number,
            owner_id=agent.owner_id,
            mode=config.execution.mode,
            model={
                "config_id": config.model.config_id,
                "temperature": config.model.temperature,
                "max_tokens": config.model.max_tokens,
                "top_p": config.model.top_p,
            },
            system_prompt=resolved.system_prompt,
            task_prompt=resolved.task_prompt,
            tool_instructions=resolved.tool_instructions,
            prompt_variables=dict(config.prompt.variables),
            tool_names=list(config.tools),
            tool_versions=tool_versions,
            skill_names=list(config.skill_names),
            knowledge_base_ids=list(config.knowledge_base_ids),
            retrieval_enabled=bool(config.retrieval.enabled and config.knowledge_base_ids),
            retrieval_top_k=config.retrieval.top_k,
            max_iterations=config.execution.max_iterations,
            timeout_seconds=config.execution.timeout_seconds,
            approval_risk_levels=list(config.execution.approval_risk_levels),
            workflow_id=(
                workflow_snapshot.workflow_id if workflow_snapshot else None
            ),
            workflow_version_id=(
                workflow_snapshot.version_id if workflow_snapshot else None
            ),
            required_permissions=list(config.permissions.required_permissions),
            allowed_roles=list(config.permissions.allowed_roles),
            metadata=dict(config.metadata),
        )

        version = AgentVersion(
            id=version_id,
            agent_id=agent.id,
            version=version_number,
            config=config.model_dump(mode="json"),
            snapshot=snapshot.model_dump(mode="json"),
            status="published",
            published_by=actor.user_id,
            published_at=now,
        )
        self.db.add(version)
        agent.status = "published"
        agent.current_version_id = version_id
        agent.published_at = now
        await self.db.commit()
        await self.db.refresh(agent)
        return await self._agent_out(agent)

    async def disable(self, agent_id: str, actor: UserContext) -> AgentOut:
        agent = await self._get_authorized(agent_id, actor, "agents:publish", "publish")
        agent.status = "disabled"
        await self.db.commit()
        await self.db.refresh(agent)
        return await self._agent_out(agent)

    async def list_versions(self, agent_id: str, actor: UserContext) -> list[AgentVersionOut]:
        await PermissionService(self.db).require_permission(actor, "agents:read")
        await self._get_agent_or_404(agent_id)
        return [
            AgentVersionOut.model_validate(version)
            for version in await self.repo.list_versions(agent_id)
        ]

    # ------------------------------------------------------------------- loader

    async def load_version(self, agent_id: str, version_id: str) -> AgentSnapshot:
        agent = await self._get_agent_or_404(agent_id)
        version = await self.repo.get_version(version_id)
        if version is None or version.agent_id != agent.id:
            raise NotFoundError("Agent version not found")
        return AgentSnapshot.model_validate(version.snapshot)

    async def load_published(self, agent_id: str) -> AgentSnapshot:
        agent = await self._get_agent_or_404(agent_id)
        if agent.status != "published":
            raise ConflictError(f"Agent {agent.name} is not published (status={agent.status})")
        if not agent.current_version_id:
            raise ConflictError(f"Agent {agent.name} has no published version")
        version = await self.repo.get_version(agent.current_version_id)
        if version is None:
            raise ConflictError("Published agent version is missing")
        return AgentSnapshot.model_validate(version.snapshot)

    # -------------------------------------------------------------------- helpers

    async def _collect_validation(
        self, agent: Agent, actor: UserContext
    ) -> AgentValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            config = AgentConfig.model_validate(agent.draft_config)
        except PydanticValidationError as exc:
            for item in exc.errors():
                location = ".".join(str(part) for part in item["loc"])
                errors.append(f"{location}: {item['msg']}")
            return AgentValidationResult(valid=False, errors=errors, warnings=warnings)

        model_service = self._model_service()
        try:
            await model_service.resolve(config.model.config_id)
        except AppError as exc:
            errors.append(f"Model configuration: {exc.message}")
        else:
            if config.tools:
                try:
                    supports_tools = await model_service.supports_tools(config.model.config_id)
                except AppError as exc:
                    errors.append(f"Model tool support: {exc.message}")
                else:
                    if not supports_tools:
                        errors.append("Selected model does not support tool calls")

        prompt_service = self._prompt_service()
        try:
            prompt = await prompt_service.get_prompt(config.prompt.prompt_id)
        except AppError as exc:
            errors.append(f"Prompt: {exc.message}")
        else:
            if prompt.current_version < 1:
                errors.append("Prompt has no published version")
            try:
                await prompt_service.resolve(
                    config.prompt.prompt_id,
                    config.prompt.version_id,
                    config.prompt.variables,
                )
            except AppError as exc:
                errors.append(f"Prompt: {exc.message}")

        if config.tools:
            try:
                descriptors = await self._tool_catalog(list(config.tools), actor)
            except AppError as exc:
                errors.append(f"Tools: {exc.message}")
            else:
                found = {descriptor.name for descriptor in descriptors}
                missing = [name for name in config.tools if name not in found]
                if missing:
                    errors.append(
                        "Tools unavailable or not permitted: " + ", ".join(missing)
                    )
                disabled = sorted(
                    descriptor.name for descriptor in descriptors if not descriptor.enabled
                )
                if disabled:
                    errors.append("Tools disabled: " + ", ".join(disabled))

        if config.execution.mode in {"workflow", "hybrid"}:
            if not config.execution.workflow_id:
                errors.append("Execution mode requires a workflow_id")
            else:
                try:
                    from app.modules.workflow.service import WorkflowService

                    await WorkflowService(self.db).validate_agent_binding(
                        config.execution.workflow_id, actor
                    )
                except AppError as exc:
                    errors.append(f"Workflow: {exc.message}")
        elif config.execution.workflow_id:
            errors.append("workflow_id is only allowed for workflow/hybrid mode")

        if config.knowledge_base_ids:
            try:
                from app.modules.knowledge.service import KnowledgeService

                await KnowledgeService(self.db).validate_bindings(
                    config.knowledge_base_ids, actor
                )
            except AppError as exc:
                errors.append(f"Knowledge: {exc.message}")
        elif config.retrieval.enabled:
            warnings.append(
                "Retrieval is enabled but no knowledge base is bound; retrieval will be skipped"
            )

        if config.skill_names:
            try:
                from app.modules.skill.service import SkillService

                await SkillService(self.db).validate_bindings(
                    config.skill_names, actor, bound_tools=config.tools
                )
            except AppError as exc:
                errors.append(f"Skills: {exc.message}")

        permission_service = PermissionService(self.db)
        known_permissions = {
            permission.code for permission in await permission_service.repo.list_permissions()
        }
        unknown_permissions = sorted(
            set(config.permissions.required_permissions) - known_permissions
        )
        if unknown_permissions:
            errors.append("Unknown permissions: " + ", ".join(unknown_permissions))
        known_roles = {role.name for role in await permission_service.repo.list_roles()}
        unknown_roles = sorted(set(config.permissions.allowed_roles) - known_roles)
        if unknown_roles:
            errors.append("Unknown roles: " + ", ".join(unknown_roles))

        if not config.tools:
            warnings.append("No tools configured; the agent can only respond with text")
        return AgentValidationResult(valid=not errors, errors=errors, warnings=warnings)

    async def _get_agent_or_404(self, agent_id: str) -> Agent:
        agent = await self.repo.get_agent(agent_id)
        if agent is None:
            raise NotFoundError("Agent not found")
        return agent

    async def _get_authorized(
        self, agent_id: str, actor: UserContext, code: str, action: str
    ) -> Agent:
        permission_service = PermissionService(self.db)
        await permission_service.require_permission(actor, code)
        agent = await self._get_agent_or_404(agent_id)
        if not await permission_service.can_access_resource(
            actor,
            resource_type="agent",
            resource_id=agent.id,
            action=action,
            owner_id=agent.owner_id,
        ):
            raise PermissionDeniedError(f"No {action} access to agent {agent.id}")
        return agent

    async def _agent_out(self, agent: Agent) -> AgentOut:
        current_version: int | None = None
        if agent.current_version_id:
            version = await self.repo.get_version(agent.current_version_id)
            current_version = version.version if version else None
        return AgentOut(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            status=agent.status,
            owner_id=agent.owner_id,
            draft_config=AgentConfig.model_validate(agent.draft_config),
            current_version_id=agent.current_version_id,
            current_version=current_version,
            published_at=agent.published_at,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )

    def _model_service(self):  # noqa: ANN202
        from app.modules.model.service import ModelService

        return ModelService(self.db)

    def _workflow_service(self):  # noqa: ANN202
        from app.modules.workflow.service import WorkflowService

        return WorkflowService(self.db)

    def _knowledge_service(self):  # noqa: ANN202
        from app.modules.knowledge.service import KnowledgeService

        return KnowledgeService(self.db)

    def _skill_service(self):  # noqa: ANN202
        from app.modules.skill.service import SkillService

        return SkillService(self.db)

    def _prompt_service(self):  # noqa: ANN202
        from app.modules.prompt.service import PromptService

        return PromptService(self.db)

    async def _default_tool_catalog(
        self, names: Sequence[str], actor: UserContext
    ) -> Sequence[ToolDescriptor]:
        from app.modules.tool.service import ToolService

        return await ToolService(self.db).describe(list(names), actor)
