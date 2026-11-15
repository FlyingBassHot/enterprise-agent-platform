"""Bootstrap the platform and seed the offline audit demo.

Usage:
    uv run python scripts/seed.py
    uv run python scripts/seed.py --admin-password 'change-me-now'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.exceptions import ConflictError  # noqa: E402
from app.domain_plugins.audit import register as register_audit  # noqa: E402
from app.domain_plugins.audit.seed import seed_audit_demo  # noqa: E402
from app.infrastructure.database.registry import *  # noqa: E402,F401,F403
from app.infrastructure.database.session import get_session_factory, init_db  # noqa: E402
from app.modules.agent.service import AgentService  # noqa: E402
from app.modules.knowledge.service import KnowledgeService  # noqa: E402
from app.modules.model.schemas import (  # noqa: E402
    ModelCapabilities,
    ModelConfigCreate,
    ModelParams,
    ProviderCreate,
)
from app.modules.model.service import ModelService  # noqa: E402
from app.modules.permission.service import PermissionService  # noqa: E402
from app.modules.prompt.service import PromptService  # noqa: E402
from app.modules.skill.service import SkillService  # noqa: E402
from app.modules.tool.service import ToolService  # noqa: E402


def _write_initial_password(password: str) -> Path:
    target = Path(get_settings().storage_dir).resolve().parent / "initial_admin_password.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(password + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


async def ensure_admin(
    permissions: PermissionService, username: str, password: str | None
) -> tuple[object, str | None, Path | None]:
    if await permissions.count_users() == 0:
        generated: str | None = None
        password_file: Path | None = None
        if not password:
            generated = secrets.token_urlsafe(24)
            password = generated
            password_file = _write_initial_password(generated)
        admin = await permissions.bootstrap_admin(
            username, password, display_name="Administrator"
        )
        return admin, generated, password_file
    users = await permissions.list_users()
    admins = [user for user in users if user.is_superuser]
    if not admins:
        raise RuntimeError(
            "Users exist but no superuser was found; create one manually before seeding"
        )
    return admins[0], None, None


async def ensure_mock_model(models: ModelService) -> str:
    providers = {provider.name: provider for provider in await models.list_providers()}
    provider = providers.get("mock-local")
    if provider is None:
        provider = await models.create_provider(
            ProviderCreate(
                name="mock-local",
                provider_type="mock",
                description="离线确定性模型，用于演示与测试",
            )
        )
    configs = {config.name: config for config in await models.list_configs()}
    config = configs.get("mock-agent")
    if config is None:
        config = await models.create_config(
            ModelConfigCreate(
                name="mock-agent",
                display_name="Mock Agent",
                provider_id=provider.id,
                model_name="mock-agent",
                params=ModelParams(temperature=0.2, max_tokens=4096),
                capabilities=ModelCapabilities(),
                is_default=True,
                description="离线 Mock 模型；支持脚本化工具调用和审计演示",
            )
        )
    return config.id


async def main() -> None:
    parser = argparse.ArgumentParser()
    settings = get_settings()
    parser.add_argument(
        "--admin-username", default=settings.bootstrap_admin_username or "admin"
    )
    parser.add_argument(
        "--admin-password", default=settings.bootstrap_admin_password
    )
    args = parser.parse_args()

    await init_db()
    async with get_session_factory()() as db:
        permissions = PermissionService(db)
        await permissions.ensure_seed()
        admin, _generated_password, password_file = await ensure_admin(
            permissions, args.admin_username, args.admin_password
        )
        actor = await permissions.load_context(admin.id)

        models = ModelService(db)
        model_config_id = await ensure_mock_model(models)

        register_audit()
        tools = ToolService(db)
        synced = await tools.sync_native()

        result = await seed_audit_demo(
            AgentService(db),
            PromptService(db),
            tools,
            actor,
            model_config_id=model_config_id,
            knowledge_service=KnowledgeService(db),
            skill_service=SkillService(db),
        )
        await db.commit()

    summary = {
        "admin_username": admin.username,
        "admin_password_file": str(password_file) if password_file else None,
        "model_config_id": model_config_id,
        "native_tools_synced": synced,
        **result,
        "next": "uv run uvicorn app.main:app --reload --port 8000",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConflictError as exc:
        print(f"seed failed: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from exc
