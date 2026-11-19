"""Real registry/stdio/runtime contract, using isolated state and a scripted LLM."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.contracts import ToolInvocation
from app.modules.agent.schemas import AgentConfig, AgentCreate
from app.modules.agent.service import AgentService
from app.modules.model.schemas import ModelConfigCreate, ProviderCreate
from app.modules.model.service import ModelService
from app.modules.permission.service import PermissionService
from app.modules.prompt.schemas import PromptCreate, PromptVersionCreate
from app.modules.prompt.service import PromptService
from app.modules.skill.service import SkillService
from app.modules.tool.mcp_service import MCPService
from app.modules.tool.models import MCPServer
from app.modules.tool.schemas import MCPServerUpdate
from app.modules.tool.service import ToolService
from sqlalchemy import select

from extensions.postgraduate import manage


async def test_install_real_stdio_runtime_isolation_and_uninstall(db, tmp_path):
    permission = PermissionService(db)
    await permission.ensure_seed()
    admin = await permission.bootstrap_admin(
        "postgrad-test-admin", "temporary-test-password", display_name="Test Admin"
    )
    actor = await permission.load_context(admin.id)
    models = ModelService(db)
    provider = await models.create_provider(
        ProviderCreate(name="postgrad-test-provider", provider_type="mock")
    )
    model = await models.create_config(
        ModelConfigCreate(
            name="postgrad-test-model", provider_id=provider.id, model_name="mock", is_default=True
        )
    )
    prompts = PromptService(db)
    prompt = await prompts.create_prompt(PromptCreate(name="sentinel-existing-prompt"))
    version = await prompts.create_version(
        prompt.id, PromptVersionCreate(system_prompt="Existing unrelated agent"), actor.user_id
    )
    agents = AgentService(db)
    sentinel = await agents.create_agent(
        AgentCreate(
            name="existing-sentinel",
            config=AgentConfig(
                model={"config_id": model.id},
                prompt={"prompt_id": prompt.id, "version_id": version.id},
            ),
        ),
        actor,
    )
    sentinel = await agents.publish(sentinel.id, actor)
    before = sentinel.model_dump(mode="json")

    installed = await manage.install(db, actor, data_dir=tmp_path / "business", model_id=model.id)
    assert {a["name"] for a in installed} == set(manage.AGENTS.values())
    assert all(a["status"] == "published" for a in installed)
    first_versions = {a["name"]: a["version"] for a in installed}
    again = await manage.install(db, actor, data_dir=tmp_path / "business", model_id=model.id)
    assert {a["name"]: a["version"] for a in again} == first_versions
    assert (await agents.get_agent(sentinel.id, actor)).model_dump(mode="json") == before
    advisor = next(a for a in installed if a["name"] == "postgrad-university-advisor")
    coach = next(a for a in installed if a["name"] == "postgrad-exam-coach")
    assert len(advisor["skills"]) == 6 and len(coach["skills"]) == 8
    assert all("notification" not in t and "exam-knowledge" not in t for t in advisor["tools"])
    assert all("university-data" not in t and "admission-ml" not in t for t in coach["tools"])
    assert "mcp__postgrad-documents__export_docx" not in advisor["tools"]
    assert all(
        "review_proposals" not in t and "upsert_verified_record" not in t
        for a in installed
        for t in a["tools"]
    )
    tools = ToolService(db)
    notification = [
        t
        for t in await tools.list_tools(actor)
        if t.name.endswith(("__send_email", "__send_wecom_group"))
    ]
    assert all(t.risk_level == "high" and t.requires_approval for t in notification)

    ping = "mcp__postgrad-university-data__ping"
    invocation = ToolInvocation(tool_name=ping, arguments={}, user_id=actor.user_id)
    result = await tools.execute(invocation, actor)
    assert result.ok and result.output["status"] == "ok"
    # Exercise actual platform runtime with real MCP tools and the published config.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend/tests"))
    from app.modules.runtime.checkpoint import close_checkpointer, create_checkpointer
    from app.modules.runtime.service import RuntimeService
    from test_runtime import FakeModel, FakeSink, FakeTools, make_tool

    saver = await create_checkpointer(str(tmp_path / "checkpoints.db"))
    try:
        snapshot = await agents.load_published(advisor["id"])
        sink = FakeSink()
        runtime = RuntimeService(
            model_gateway=FakeModel(
                [
                    {"tool_calls": [{"name": ping, "arguments": {}}]},
                    {"content": "MCP ping completed; no admissions data claimed."},
                ]
            ),
            tool_gateway=tools,
            sink=sink,
            skill_gateway=SkillService(db),
            checkpointer=saver,
        )
        completed = await runtime.run(
            execution_id=str(uuid4()), snapshot=snapshot, user=actor, user_text="验证院校工具连通性"
        )
        assert completed.status == "completed"
        assert any(e["type"] == "tool_call_completed" and e["payload"]["ok"] for e in sink.events)
        forbidden = "mcp__postgrad-notification__send_email"
        gateway = FakeTools([make_tool(t) for t in snapshot.tool_names] + [make_tool(forbidden)])
        blocked_sink = FakeSink()
        runtime = RuntimeService(
            model_gateway=FakeModel(
                [
                    {"tool_calls": [{"name": forbidden, "arguments": {}}]},
                    {"content": "Denied unbound notification tool."},
                ]
            ),
            tool_gateway=gateway,
            sink=blocked_sink,
            skill_gateway=SkillService(db),
            checkpointer=saver,
        )
        await runtime.run(
            execution_id=str(uuid4()),
            snapshot=snapshot,
            user=actor,
            user_text="Try an unbound tool",
        )
        assert gateway.invocations == []
        assert any(
            e["type"] == "tool_call_completed"
            and e["payload"].get("tool_name") == forbidden
            and e["payload"].get("status") == "denied"
            and e["payload"].get("error_code") in {"tool_not_bound", "tool_unavailable"}
            for e in blocked_sink.events
        )
    finally:
        await close_checkpointer(saver)

    mcp = MCPService(db)
    server = next(s for s in await mcp.list_servers(actor) if s.name == "postgrad-university-data")
    await mcp.update_server(server.id, MCPServerUpdate(enabled=False), actor)
    denied = await tools.execute(invocation, actor)
    assert not denied.ok and "disabled" in denied.error
    # A disabled Skill blocks this instance, while the other instance resolves.
    skills = SkillService(db)
    skill = await skills.get_skill("university-research")
    await skills.set_enabled(skill.id, False, actor)
    with pytest.raises(Exception, match="Disabled skills"):
        await skills.resolve_for_agent(advisor["skills"], actor)
    assert len(await skills.resolve_for_agent(coach["skills"], actor)) == 8
    # Even same-name, different registrations are not silently overwritten.
    with pytest.raises(RuntimeError, match="administratively disabled"):
        await manage.install(db, actor, data_dir=tmp_path / "business", model_id=model.id)
    removed = await manage.uninstall(db, actor)
    assert set(removed["removed_agents"]) == set(manage.AGENTS.values())
    assert (await agents.get_agent(sentinel.id, actor)).model_dump(mode="json") == before
    assert not any(s.name in manage.SERVERS for s in (await db.scalars(select(MCPServer))).all())
    assert not any(
        s.name in advisor["skills"] + coach["skills"] for s in await skills.list_skills(actor)
    )
    assert removed["business_data"] == "retained"
