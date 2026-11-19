"""Independent security regressions for the postgraduate extension.

No subprocesses are started here: every check is in-process, so this file is
safe in sandboxes where the stdio integration suite blocks.

These tests were written by an independent reviewer before the fixes. Every
control is required to pass; no expected-failure exemptions remain.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[1]
MCP_DIR = EXTENSION / "mcp"
REPO = Path(__file__).resolve().parents[3]
for entry in (str(MCP_DIR), str(REPO)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import documents  # noqa: E402
import notification  # noqa: E402
import research  # noqa: E402


def test_research_pins_the_resolved_ip_not_the_hostname(monkeypatch):
    """The TLS connection must use the address that was safety-checked."""

    monkeypatch.setattr(research, "_public_addresses", lambda host: ["93.184.216.34"])
    connected: list[tuple[str, int]] = []

    class RawSocket:
        pass

    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            assert server_hostname == "allowed.edu.cn"
            return raw

    monkeypatch.setattr(
        research.socket,
        "create_connection",
        lambda address, timeout: connected.append(address) or RawSocket(),
    )
    monkeypatch.setattr(research.ssl, "create_default_context", lambda: Context())

    connection = research._PinnedHTTPS("allowed.edu.cn", timeout=5)
    connection.connect()

    assert connected == [("93.184.216.34", 443)]
    assert connection.sock is not None


def test_research_rejects_multicast_resolution(monkeypatch):
    monkeypatch.setattr(
        research.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("239.1.1.1", 443))],
    )
    with pytest.raises(ValueError):
        research._public_addresses("allowed.edu.cn")


def test_documents_rejects_symlinked_documents_root(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRAD_DATA_DIR", str(tmp_path / "data"))
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not-imported", encoding="utf-8")
    (data / "documents").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        documents.parse_document("secret.txt")


def test_notification_explicit_empty_wecom_allowlist_blocks_sends(tmp_path, monkeypatch):
    for key in (
        "POSTGRAD_WECOM_ALLOWLIST",
        "POSTGRAD_WECOM_WEBHOOKS",
        "POSTGRAD_NOTIFICATION_DRY_RUN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "POSTGRAD_WECOM_WEBHOOKS",
        json.dumps(
            {"study-group": ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=review-key")}
        ),
    )
    monkeypatch.setenv("POSTGRAD_WECOM_ALLOWLIST", "")

    config = notification.NotificationConfig.from_env()
    assert config.wecom_allowlist == frozenset()


def test_notification_per_call_dry_run_is_honored(tmp_path, monkeypatch):
    for key in (
        "POSTGRAD_EMAIL_ALLOWLIST",
        "POSTGRAD_NOTIFICATION_DRY_RUN",
        "POSTGRAD_SMTP_HOST",
        "POSTGRAD_SMTP_FROM",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    monkeypatch.setenv("POSTGRAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_FROM", "bot@example.com")
    server = notification.build_server(data_dir=tmp_path)
    sent: list[tuple] = []
    monkeypatch.setattr(notification, "_smtp_send", lambda *args, **kwargs: sent.append(args) or "")

    result = asyncio.run(
        server.call_tool(
            "send_email",
            {
                "to": ["allowed@example.com"],
                "subject": "dry-run preview",
                "body": "must not leave the process",
                "idempotency_key": "review-dry-run-1",
                "authorization": {
                    "channel": "email",
                    "authorized_by": "review",
                    "authorization_ref": "review-approval",
                },
                "dry_run": True,
            },
        )
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "dry_run"
    assert sent == []


async def test_uninstall_does_not_delete_a_prompt_referenced_by_another_agent(db):
    from app.modules.agent.schemas import AgentConfig, AgentCreate
    from app.modules.agent.service import AgentService
    from app.modules.model.schemas import ModelConfigCreate, ProviderCreate
    from app.modules.model.service import ModelService
    from app.modules.permission.service import PermissionService
    from app.modules.prompt.schemas import PromptCreate, PromptVersionCreate
    from app.modules.prompt.service import PromptService

    from extensions.postgraduate import manage

    permission = PermissionService(db)
    await permission.ensure_seed()
    admin = await permission.bootstrap_admin(
        "review-admin", "temporary-test-password", display_name="Review Admin"
    )
    actor = await permission.load_context(admin.id)
    models = ModelService(db)
    provider = await models.create_provider(
        ProviderCreate(name="review-provider", provider_type="mock")
    )
    model = await models.create_config(
        ModelConfigCreate(
            name="review-model", provider_id=provider.id, model_name="mock", is_default=True
        )
    )
    prompts = PromptService(db)
    owned = await prompts.create_prompt(
        PromptCreate(
            name="postgrad-university-advisor-system",
            description="postgraduate extension owned prompt",
        )
    )
    version = await prompts.create_version(
        owned.id, PromptVersionCreate(system_prompt="extension prompt"), actor.user_id
    )
    agents = AgentService(db)
    foreign = await agents.create_agent(
        AgentCreate(
            name="foreign-review-agent",
            config=AgentConfig(
                model={"config_id": model.id},
                prompt={"prompt_id": owned.id, "version_id": version.id},
            ),
        ),
        actor,
    )
    assert foreign.id

    await manage.uninstall(db, actor)

    remaining = {prompt.name for prompt in await prompts.list_prompts()}
    assert "postgrad-university-advisor-system" in remaining


def test_past_paper_without_copyright_note_is_rejected(tmp_path):
    import exam_knowledge

    server = exam_knowledge.build_server(data_dir=tmp_path, allow_review=True)
    course = {
        "school_id": "10001",
        "college_id": "CS",
        "program_code": "081200",
        "exam_year": 2026,
        "subject_code": "408",
    }
    evidence = {
        "document_ref": "syllabus-408-2026.pdf",
        "title": "2026 年 408 考试大纲",
        "publisher": "Example University",
        "evidence_span": "p.12 第2章 数据结构",
        "retrieved_at": "2026-11-01",
    }

    def call(tool: str, **arguments):
        result = asyncio.run(server.call_tool(tool, arguments))
        assert result.is_error is False, result
        return json.loads(result.content[0].text)

    proposal = call(
        "propose_node",
        **course,
        node_type="knowledge_point",
        name="二叉树",
        evidence=evidence,
    )
    reviewed = call(
        "review_proposals",
        reviewer="review",
        decisions=[{"proposal_id": proposal["proposal_id"], "decision": "approve"}],
    )
    node_id = reviewed["results"][0]["result_id"]
    saved = call(
        "save_verified_question",
        **course,
        question_type="short_answer",
        stem="解释二叉树的遍历方式。",
        answer="前序、中序、后序。",
        node_ids=[node_id],
        source_type="past_paper",
        paper_year=2025,
        source=evidence,
    )
    assert saved["error"]["code"] == "copyright_note_required"


def test_manage_read_tools_match_every_advertised_surface(tmp_path):
    """The strict install contract must hold before registration touches a DB."""

    import admission_ml
    import exam_knowledge
    import university_data

    from extensions.postgraduate import manage

    servers = {
        "postgrad-university-data": university_data.mcp,
        "postgrad-admission-ml": admission_ml.mcp,
        "postgrad-exam-knowledge": exam_knowledge.build_server(
            data_dir=tmp_path / "knowledge", allow_review=False
        ),
        "postgrad-notification": notification.build_server(data_dir=tmp_path / "notification"),
        "postgrad-research": research.server,
        "postgrad-documents": documents.server,
    }

    for name, server in servers.items():
        actual = {tool.name for tool in asyncio.run(server.list_tools())}
        assert actual == set(manage.READ_TOOLS[name]), name


async def test_uninstall_preserves_workflow_bound_directly_to_extension_tool(db):
    from uuid import uuid4

    from app.core.contracts import UserContext
    from app.modules.workflow.models import Workflow

    from extensions.postgraduate import manage

    actor = UserContext(user_id=str(uuid4()), username="workflow-review", is_superuser=True)
    workflow = Workflow(
        name="foreign-tool-workflow",
        owner_id=actor.user_id,
        draft_definition={
            "nodes": [
                {"type": "tool", "config": {"tool_name": "mcp__postgrad-research__search_web"}}
            ]
        },
    )
    db.add(workflow)
    await db.commit()
    with pytest.raises(RuntimeError, match="Workflow references extension resources"):
        await manage.uninstall(db, actor)
    assert await db.get(Workflow, workflow.id) is not None
