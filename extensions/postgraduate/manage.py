"""Explicit, idempotent registration through the platform's existing services.

Run with the backend interpreter, from backend/ (or inside its container).
No bootstrap, schema creation, core monkey patches, or external messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[1]
for platform_root in (REPOSITORY / "backend", REPOSITORY):
    # Repository: <repo>/backend/app. Container: /app/app.
    if (platform_root / "app/__init__.py").is_file():
        sys.path.insert(0, str(platform_root))
        break

AGENTS = {
    "university-advisor": "postgrad-university-advisor",
    "exam-coach": "postgrad-exam-coach",
}
SERVERS = {
    "postgrad-university-data": "university_data.py",
    "postgrad-admission-ml": "admission_ml.py",
    "postgrad-exam-knowledge": "exam_knowledge.py",
    "postgrad-notification": "notification.py",
    "postgrad-research": "research.py",
    "postgrad-documents": "documents.py",
}
READ_TOOLS = {
    "postgrad-university-data": [
        "ping",
        "search_programs",
        "get_admission_history",
        "get_source_evidence",
    ],
    "postgrad-admission-ml": [
        "list_models",
        "get_model_info",
        "predict_admission",
        "compare_models",
        "explain_prediction",
        "get_validation_metrics",
    ],
    "postgrad-exam-knowledge": [
        "ping",
        "get_syllabus",
        "search_knowledge",
        "propose_node",
        "propose_relation",
        "get_prerequisites",
        "find_questions",
        "save_verified_question",
        "record_attempt",
    ],
    "postgrad-notification": ["send_email", "send_wecom_group", "get_delivery_status"],
    "postgrad-research": ["search_web", "fetch_public_page", "geocode_campus"],
    "postgrad-documents": ["parse_document", "export_docx"],
}


def tool_names(servers: list[str]) -> list[str]:
    return [f"mcp__{server}__{name}" for server in servers for name in READ_TOOLS[server]]


async def install(
    db,
    actor,
    *,
    python: str = sys.executable,
    data_dir: Path | None = None,
    model_id: str | None = None,
    server_env: dict[str, dict[str, str]] | None = None,
) -> list[dict]:
    from app.modules.agent.schemas import AgentConfig, AgentCreate, AgentUpdate
    from app.modules.agent.service import AgentService
    from app.modules.model.service import ModelService
    from app.modules.prompt.schemas import PromptCreate, PromptVersionCreate
    from app.modules.prompt.service import PromptService
    from app.modules.skill.loader import scan_skills
    from app.modules.skill.models import Skill
    from app.modules.skill.service import SkillService
    from app.modules.tool.mcp_service import MCPService
    from app.modules.tool.schemas import MCPServerCreate, ToolUpdate
    from app.modules.tool.service import ToolService
    from sqlalchemy import select

    if not actor.is_superuser:
        raise RuntimeError("Installation requires an existing active platform superuser")
    if server_env is not None and (
        not isinstance(server_env, dict) or set(server_env) - set(SERVERS)
    ):
        raise RuntimeError("Unknown MCP server in environment configuration")
    models = ModelService(db)
    configs = [
        c for c in await models.list_configs() if c.enabled and c.capabilities.supports_tools
    ]
    if model_id:
        selected = next((c for c in configs if c.id == model_id), None)
    else:
        selected = next((c for c in configs if c.is_default), None)
        selected = selected or next((c for c in configs if c.provider_type != "mock"), None)
        selected = selected or next(iter(configs), None)
    if selected is None:
        raise RuntimeError(
            "No enabled tool-capable model; configure one in the existing model page"
        )
    await models.resolve(selected.id)

    agents = AgentService(db)
    existing_agents = {a.name: a for a in await agents.list_agents(actor)}
    for name in AGENTS.values():
        existing = existing_agents.get(name)
        if existing and existing.draft_config.metadata.get("extension") != "postgraduate":
            raise RuntimeError(f"Refusing to replace unrelated Agent: {name}")
    definitions = scan_skills(ROOT / "agents")
    for definition in definitions:
        existing = await db.scalar(select(Skill).where(Skill.name == definition.name))
        if existing and existing.metadata_json.get("extension") != "postgraduate":
            raise RuntimeError(f"Refusing to replace unrelated Skill: {definition.name}")

    mcp = MCPService(db)
    tools = ToolService(db)
    registered = {s.name: s for s in await mcp.list_servers(actor)}
    data_path = (data_dir or ROOT / "data").resolve()
    for name, filename in SERVERS.items():
        script = ROOT / "mcp" / filename
        if not script.is_file():
            raise RuntimeError(f"Missing MCP implementation: {script}")
        env = {"POSTGRAD_DATA_DIR": str(data_path)}
        # Only explicitly configured secrets are templated; values never enter DB/logs.
        if name == "postgrad-research":
            for secret in ("BRAVE_API_KEY", "AMAP_API_KEY"):
                if os.getenv(f"TOOL_SECRET_POSTGRAD_{secret}"):
                    env[secret] = "${TOOL_SECRET_POSTGRAD_" + secret + "}"
            env["POSTGRAD_PUBLIC_HOSTS"] = os.getenv("POSTGRAD_PUBLIC_HOSTS", "")
        overrides = (server_env or {}).get(name, {})
        allowed = {
            "postgrad-research": {"BRAVE_API_KEY", "AMAP_API_KEY", "POSTGRAD_PUBLIC_HOSTS"},
            "postgrad-notification": {
                "POSTGRAD_NOTIFICATION_DRY_RUN",
                "POSTGRAD_EMAIL_ALLOWLIST",
                "POSTGRAD_WECOM_ALLOWLIST",
                "POSTGRAD_WECOM_WEBHOOKS",
                "POSTGRAD_SMTP_HOST",
                "POSTGRAD_SMTP_PORT",
                "POSTGRAD_SMTP_USER",
                "POSTGRAD_SMTP_PASSWORD",
                "POSTGRAD_SMTP_FROM",
                "POSTGRAD_SMTP_SSL",
                "POSTGRAD_SMTP_STARTTLS",
                "POSTGRAD_SMTP_TIMEOUT",
                "POSTGRAD_NOTIFICATION_RATE_LIMIT_PER_HOUR",
                "POSTGRAD_NOTIFICATION_STALE_SECONDS",
            },
        }.get(name, set())
        if not isinstance(overrides, dict) or set(overrides) - allowed:
            raise RuntimeError(f"Unsupported environment override for {name}")
        if "POSTGRAD_WECOM_WEBHOOKS" in overrides and not overrides[
            "POSTGRAD_WECOM_WEBHOOKS"
        ].startswith("${TOOL_SECRET_"):
            raise RuntimeError("Webhook mappings must use a TOOL_SECRET environment reference")
        env.update(overrides)
        payload = MCPServerCreate(
            name=name, transport="stdio", command=python, args=[str(script)], env=env, enabled=True
        )
        if name in registered:
            server = registered[name]
            # Never silently repoint a same-name server to different executable code.
            if server.command != python or server.args != [str(script)] or server.env != env:
                raise RuntimeError(
                    f"MCP registration differs for {name}; inspect/remove it before relocating or reconfiguring"
                )
            if not server.enabled:
                raise RuntimeError(f"MCP {name} is administratively disabled; do not auto-enable")
        else:
            server = await mcp.create_server(payload, actor)
        result = await mcp.sync(server.id, actor)
        actual = {t.name for t in result.tools}
        expected = set(tool_names([name]))
        if expected != actual:
            raise RuntimeError(
                f"MCP contract differs for {name}: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        for tool in result.tools:
            remote = tool.config.get("remote_name")
            if name == "postgrad-notification" and remote in {"send_email", "send_wecom_group"}:
                await tools.update_tool(
                    tool.id,
                    ToolUpdate(
                        risk_level="high",
                        requires_approval=True,
                        sensitive_fields=["recipient", "to", "body", "content", "webhook"],
                    ),
                    actor,
                )

    skills = SkillService(db, scan_root=ROOT / "agents")
    synced = await skills.sync(actor)
    for skill in synced:
        if not skill.enabled:
            await skills.set_enabled(skill.id, True, actor)

    prompts = PromptService(db)
    known_prompts = {p.name: p for p in await prompts.list_prompts()}
    results = []
    for folder, name in AGENTS.items():
        spec = json.loads((ROOT / "agents" / folder / "agent.json").read_text())
        system = (ROOT / "agents" / folder / "system.md").read_text()
        prompt_name = name + "-system"
        prompt = known_prompts.get(prompt_name)
        if prompt is None:
            prompt = await prompts.create_prompt(
                PromptCreate(name=prompt_name, description="postgraduate extension owned prompt")
            )
        elif prompt.description != "postgraduate extension owned prompt":
            raise RuntimeError(f"Unowned prompt name collision: {prompt_name}")
        versions = await prompts.list_versions(prompt.id)
        latest = max(versions, key=lambda v: v.version) if versions else None
        if latest is None or latest.system_prompt != system:
            latest = await prompts.create_version(
                prompt.id, PromptVersionCreate(system_prompt=system), actor.user_id
            )
        config = AgentConfig.model_validate(
            {
                "model": {"config_id": selected.id, "max_tokens": 4096, "temperature": 0.2},
                "prompt": {"prompt_id": prompt.id, "version_id": latest.id},
                "tools": [
                    t for t in tool_names(spec["servers"]) if t not in spec.get("exclude_tools", [])
                ],
                "skill_names": spec["skills"],
                "retrieval": {"enabled": False},
                "execution": {"max_iterations": 24, "timeout_seconds": 600},
                "permissions": {"required_permissions": ["tools:execute"]},
                "metadata": {
                    "extension": "postgraduate",
                    "extension_version": "1.0.0",
                    "agent_slug": name,
                    "model_provider_type": selected.provider_type,
                    "external_data_status": "query_mcp_for_current_status",
                    "delivery_mode": "query_notification_configuration; default dry_run",
                },
            }
        )
        agent = existing_agents.get(name)
        if agent is None:
            agent = await agents.create_agent(
                AgentCreate(name=name, description=spec["description"], config=config), actor
            )
        elif agent.draft_config != config:
            agent = await agents.update_agent(
                agent.id, AgentUpdate(description=spec["description"], config=config), actor
            )
        current = await agents.list_versions(agent.id, actor)
        published = next((v for v in current if v.id == agent.current_version_id), None)
        if (
            agent.status != "published"
            or published is None
            or published.config != config.model_dump(mode="json")
        ):
            agent = await agents.publish(agent.id, actor)
        results.append(
            {
                "name": name,
                "id": agent.id,
                "status": agent.status,
                "version": agent.current_version,
                "model_id": selected.id,
                "model_provider_type": selected.provider_type,
                "tools": config.tools,
                "skills": config.skill_names,
            }
        )
    return results


async def uninstall(db, actor) -> dict:
    """Remove owned registrations, retain business data and execution history."""
    from app.modules.agent.models import Agent
    from app.modules.agent.service import AgentService
    from app.modules.prompt.service import PromptService
    from app.modules.skill.models import Skill
    from app.modules.tool.mcp_service import MCPService
    from app.modules.workflow.models import Workflow, WorkflowVersion
    from sqlalchemy import select

    if not actor.is_superuser:
        raise RuntimeError("Uninstallation requires a superuser")
    rows = list((await db.scalars(select(Agent))).all())
    owned = [
        a
        for a in rows
        if a.name in AGENTS.values()
        and a.draft_config.get("metadata", {}).get("extension") == "postgraduate"
    ]
    foreign = [a for a in rows if a not in owned]
    referenced_prompts = set()
    owned_ids = {a.id for a in owned}
    owned_skills = {
        s.name
        for s in (await db.scalars(select(Skill))).all()
        if s.metadata_json.get("extension") == "postgraduate"
    }
    owned_prompt_ids = {
        p.id
        for p in await PromptService(db).list_prompts()
        if p.name in {name + "-system" for name in AGENTS.values()}
        and p.description == "postgraduate extension owned prompt"
    }
    workflow_targets = owned_ids | owned_skills | owned_prompt_ids | set(tool_names(list(SERVERS)))

    def references(value, targets):
        if isinstance(value, dict):
            return any(references(v, targets) for v in value.values())
        if isinstance(value, list):
            return any(references(v, targets) for v in value)
        return isinstance(value, str) and value in targets

    for workflow in (await db.scalars(select(Workflow))).all():
        definitions = [workflow.draft_definition]
        definitions.extend(
            v.definition
            for v in (
                await db.scalars(
                    select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id)
                )
            ).all()
        )
        if any(references(d, workflow_targets) for d in definitions):
            raise RuntimeError(
                f"Workflow references extension resources: {workflow.name}; remove its binding first"
            )
    for a in foreign:
        configs = [a.draft_config]
        configs.extend(v.config for v in await AgentService(db).list_versions(a.id, actor))
        referenced_prompts.update(c.get("prompt", {}).get("prompt_id") for c in configs)
        if any(
            any(t.startswith(tuple(f"mcp__{s}__" for s in SERVERS)) for t in c.get("tools", []))
            for c in configs
        ):
            raise RuntimeError(
                f"Other Agent references extension tools: {a.name}; remove its bindings first"
            )
        if any(owned_skills.intersection(c.get("skill_names", [])) for c in configs):
            raise RuntimeError(f"Other Agent references extension Skills: {a.name}")
    removed = []
    for a in owned:
        removed.append(a.name)
        await AgentService(db).delete_agent(a.id, actor)
    mcp = MCPService(db)
    for server in await mcp.list_servers(actor):
        if server.name in SERVERS and server.args == [str(ROOT / "mcp" / SERVERS[server.name])]:
            await mcp.delete_server(server.id, actor)
    for skill in (await db.scalars(select(Skill))).all():
        if skill.metadata_json.get("extension") == "postgraduate":
            await db.delete(skill)
    await db.commit()
    prompts = PromptService(db)
    retained_prompts = []
    for p in await prompts.list_prompts():
        if (
            p.name in {n + "-system" for n in AGENTS.values()}
            and p.description == "postgraduate extension owned prompt"
        ):
            if p.id in referenced_prompts:
                retained_prompts.append(p.name)
            else:
                await prompts.delete_prompt(p.id)
    return {
        "removed_agents": removed,
        "business_data": "retained",
        "execution_history": "retained",
        "retained_shared_prompts": retained_prompts,
    }


async def main(args) -> None:
    import app.infrastructure.database.registry  # noqa: F401
    from app.infrastructure.database.session import get_session_factory
    from app.modules.permission.models import User
    from app.modules.permission.service import PermissionService
    from sqlalchemy import select

    async with get_session_factory()() as db:
        user = await db.scalar(
            select(User).where(User.username == args.actor, User.is_active.is_(True))
        )
        if user is None:
            raise RuntimeError(
                "Existing active admin account required; this CLI never creates users"
            )
        actor = await PermissionService(db).load_context(user.id)
        result = await (
            uninstall(db, actor)
            if args.command == "uninstall"
            else install(
                db,
                actor,
                python=args.python,
                data_dir=Path(args.data_dir) if args.data_dir else None,
                model_id=args.model_id,
                server_env=json.loads(Path(args.server_env_file).read_text())
                if args.server_env_file
                else None,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "uninstall"])
    parser.add_argument("--actor", default="admin")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="MCP runtime interpreter, can be an independent venv",
    )
    parser.add_argument("--data-dir")
    parser.add_argument("--model-id")
    parser.add_argument(
        "--server-env-file",
        help="Optional JSON mapping server names to nonsecret values / TOOL_SECRET references",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(main(arguments))
    except Exception as exc:
        print(f"postgraduate registration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
