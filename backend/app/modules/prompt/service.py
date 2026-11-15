from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.modules.prompt.models import Prompt, PromptVersion
from app.modules.prompt.repository import PromptRepository
from app.modules.prompt.schemas import (
    PromptCreate,
    PromptOut,
    PromptUpdate,
    PromptVariable,
    PromptVersionCreate,
    PromptVersionOut,
    ResolvedPrompt,
    extract_placeholders,
    render_template,
)


class PromptService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = PromptRepository(db)

    # ------------------------------------------------------------------ prompts

    async def list_prompts(self) -> list[PromptOut]:
        return [PromptOut.model_validate(prompt) for prompt in await self.repo.list_prompts()]

    async def get_prompt(self, prompt_id: str) -> PromptOut:
        return PromptOut.model_validate(await self._get_prompt_or_404(prompt_id))

    async def create_prompt(self, payload: PromptCreate) -> PromptOut:
        if await self.repo.get_prompt_by_name(payload.name):
            raise ConflictError("Prompt name already exists")
        prompt = Prompt(
            name=payload.name,
            description=payload.description,
            current_version=0,
            status="draft",
        )
        self.db.add(prompt)
        await self.db.commit()
        await self.db.refresh(prompt)
        return PromptOut.model_validate(prompt)

    async def update_prompt(self, prompt_id: str, payload: PromptUpdate) -> PromptOut:
        prompt = await self._get_prompt_or_404(prompt_id)
        if prompt.status != "draft":
            raise ConflictError("Published prompts are immutable; create a new version instead")
        if payload.name is not None and payload.name != prompt.name:
            if await self.repo.get_prompt_by_name(payload.name):
                raise ConflictError("Prompt name already exists")
            prompt.name = payload.name
        if payload.description is not None:
            prompt.description = payload.description
        await self.db.commit()
        await self.db.refresh(prompt)
        return PromptOut.model_validate(prompt)

    async def delete_prompt(self, prompt_id: str) -> None:
        prompt = await self._get_prompt_or_404(prompt_id)
        await self.db.delete(prompt)
        await self.db.commit()

    # ----------------------------------------------------------------- versions

    async def list_versions(self, prompt_id: str) -> list[PromptVersionOut]:
        await self._get_prompt_or_404(prompt_id)
        return [
            self._version_out(version) for version in await self.repo.list_versions(prompt_id)
        ]

    async def create_version(
        self, prompt_id: str, payload: PromptVersionCreate, user_id: str
    ) -> PromptVersionOut:
        prompt = await self._get_prompt_or_404(prompt_id)
        declared = {variable.name for variable in payload.variables}
        used = extract_placeholders(
            payload.system_prompt, payload.task_prompt, payload.tool_instructions
        )
        unknown = sorted(used - declared)
        if unknown:
            raise ValidationError(
                f"Prompt uses undeclared placeholders: {', '.join(unknown)}",
                details={"unknown_placeholders": unknown},
            )
        version_number = await self.repo.next_version_number(prompt_id)
        version = PromptVersion(
            prompt_id=prompt.id,
            version=version_number,
            system_prompt=payload.system_prompt,
            task_prompt=payload.task_prompt,
            tool_instructions=payload.tool_instructions,
            variables=[variable.model_dump() for variable in payload.variables],
            created_by=user_id,
        )
        self.db.add(version)
        if payload.publish:
            prompt.current_version = version_number
            prompt.status = "published"
        await self.db.commit()
        await self.db.refresh(version)
        return self._version_out(version)

    # ------------------------------------------------------------------ resolve

    async def resolve(
        self,
        prompt_id: str,
        version_id: str | None = None,
        variables: dict[str, str] | None = None,
    ) -> ResolvedPrompt:
        prompt = await self._get_prompt_or_404(prompt_id)
        if version_id:
            version = await self.repo.get_version(version_id)
            if version is None or version.prompt_id != prompt_id:
                raise NotFoundError("Prompt version not found")
        else:
            if prompt.current_version == 0:
                raise ValidationError("Prompt has no published version")
            version = await self.repo.get_version_by_number(prompt_id, prompt.current_version)
            if version is None:
                raise ValidationError("Prompt has no published version")
        definition = [PromptVariable.model_validate(item) for item in (version.variables or [])]
        provided = dict(variables or {})
        declared = {variable.name: variable for variable in definition}
        unknown = sorted(set(provided) - set(declared))
        if unknown:
            raise ValidationError(
                f"Unknown prompt variables: {', '.join(unknown)}",
                details={"unknown_variables": unknown},
            )
        used = extract_placeholders(
            version.system_prompt, version.task_prompt, version.tool_instructions
        )
        missing = sorted(
            name
            for name in used
            if name not in provided and (name not in declared or declared[name].required)
        )
        if missing:
            raise ValidationError(
                f"Missing required prompt variables: {', '.join(missing)}",
                details={"missing_variables": missing},
            )
        values: dict[str, str] = {}
        for name in used:
            if name in provided:
                values[name] = provided[name]
            else:
                values[name] = declared[name].default
        return ResolvedPrompt(
            prompt_id=prompt.id,
            version_id=version.id,
            version=version.version,
            system_prompt=render_template(version.system_prompt, values),
            task_prompt=render_template(version.task_prompt, values),
            tool_instructions=render_template(version.tool_instructions, values),
        )

    # ------------------------------------------------------------------ helpers

    async def _get_prompt_or_404(self, prompt_id: str) -> Prompt:
        prompt = await self.repo.get_prompt(prompt_id)
        if prompt is None:
            raise NotFoundError("Prompt not found")
        return prompt

    def _version_out(self, version: PromptVersion) -> PromptVersionOut:
        return PromptVersionOut(
            id=version.id,
            prompt_id=version.prompt_id,
            version=version.version,
            system_prompt=version.system_prompt,
            task_prompt=version.task_prompt,
            tool_instructions=version.tool_instructions,
            variables=[PromptVariable.model_validate(item) for item in (version.variables or [])],
            created_by=version.created_by,
            created_at=version.created_at,
        )
