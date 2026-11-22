from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.execution.models import AgentExecution, AgentTask
from tests.test_agents import (  # noqa: F401  (imports register models)
    create_agent,
    create_model_config,
    create_prompt,
    create_user,
)

API = "/api/v1"
WORKSPACES = f"{API}/workspaces"
TERMINAL = {"completed", "failed", "incomplete", "cancelled"}


async def wait_for_execution(
    client: AsyncClient, headers: dict[str, str], execution_id: str, timeout: float = 15.0
) -> dict:
    """Let scheduled runs finish so they do not hold SQLite locks into later tests."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        response = await client.get(f"{API}/executions/{execution_id}", headers=headers)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["status"] in TERMINAL:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"execution {execution_id} did not finish in {timeout}s")


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def published_agent(
    client: AsyncClient, admin: dict[str, str]
) -> dict:
    config_id = await create_model_config(client, admin)
    prompt_bundle = await create_prompt(client, admin)
    agent = await create_agent(
        client, admin, config_id=config_id, prompt_id=prompt_bundle["prompt"]["id"]
    )
    response = await client.post(f"{API}/agents/{agent['id']}/publish", headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


async def create_root(client: AsyncClient, admin: dict[str, str]) -> dict:
    response = await client.post(
        f"{API}/file-roots",
        headers=admin,
        json={"name": unique_name("root"), "description": "workspace root"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def grant_agent(
    client: AsyncClient, admin: dict[str, str], root_id: str, agent_id: str, permission: str
) -> None:
    response = await client.post(
        f"{API}/file-roots/{root_id}/permissions",
        headers=admin,
        json={"agent_id": agent_id, "permission": permission},
    )
    assert response.status_code == 201, response.text


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    agent_id: str,
    root_id: str,
    path: str,
    data: bytes = b"hello",
):
    return await client.post(
        f"{WORKSPACES}/agents/{agent_id}/file-roots/{root_id}/files",
        headers=headers,
        data={"path": path},
        files={"file": ("upload.txt", data, "text/plain")},
    )


async def test_usable_agents_honor_snapshot_permissions(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    published = await published_agent(client, admin)

    draft_config = await create_model_config(client, admin)
    draft_prompt = await create_prompt(client, admin)
    draft = await create_agent(
        client,
        admin,
        config_id=draft_config,
        prompt_id=draft_prompt["prompt"]["id"],
    )

    listed = await client.get(f"{WORKSPACES}/agents", headers=admin)
    assert listed.status_code == 200, listed.text
    ids = {agent["id"] for agent in listed.json()}
    assert published["id"] in ids
    assert draft["id"] not in ids
    entry = next(agent for agent in listed.json() if agent["id"] == published["id"])
    assert entry["current_version"] == 1
    assert entry["status"] == "published"
    assert entry["published_at"] is not None

    _, viewer_headers, _ = await create_user(client, admin, role="viewer")
    viewer_list = await client.get(f"{WORKSPACES}/agents", headers=viewer_headers)
    # Viewers can read executions but cannot enter the work center.
    assert viewer_list.status_code == 403, viewer_list.text

    _, developer_headers, _ = await create_user(client, admin, role="developer")
    developer_list = await client.get(f"{WORKSPACES}/agents", headers=developer_headers)
    assert developer_list.status_code == 200, developer_list.text
    assert published["id"] in {agent["id"] for agent in developer_list.json()}


async def test_conversations_are_scoped_to_path_agent(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent_a = await published_agent(client, admin)
    agent_b = await published_agent(client, admin)

    created = await client.post(
        f"{WORKSPACES}/agents/{agent_a['id']}/conversations",
        headers=admin,
        json={"title": "A only"},
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["agent_id"] == agent_a["id"]

    listed_a = await client.get(
        f"{WORKSPACES}/agents/{agent_a['id']}/conversations", headers=admin
    )
    assert listed_a.status_code == 200
    assert conversation["id"] in {row["id"] for row in listed_a.json()}

    listed_b = await client.get(
        f"{WORKSPACES}/agents/{agent_b['id']}/conversations", headers=admin
    )
    assert listed_b.status_code == 200
    assert conversation["id"] not in {row["id"] for row in listed_b.json()}

    cross = await client.get(
        f"{WORKSPACES}/agents/{agent_b['id']}/conversations/{conversation['id']}",
        headers=admin,
    )
    assert cross.status_code == 404, cross.text
    cross_messages = await client.get(
        f"{WORKSPACES}/agents/{agent_b['id']}/conversations/{conversation['id']}/messages",
        headers=admin,
    )
    assert cross_messages.status_code == 404, cross_messages.text

    same = await client.get(
        f"{WORKSPACES}/agents/{agent_a['id']}/conversations/{conversation['id']}",
        headers=admin,
    )
    assert same.status_code == 200, same.text
    messages = await client.get(
        f"{WORKSPACES}/agents/{agent_a['id']}/conversations/{conversation['id']}/messages",
        headers=admin,
    )
    assert messages.status_code == 200, messages.text


async def test_executions_and_tasks_are_agent_scoped(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    agent_a = await published_agent(client, admin)
    agent_b = await published_agent(client, admin)
    me = await client.get(f"{API}/auth/me", headers=admin)
    user_id = me.json()["id"]

    task_a = AgentTask(agent_id=agent_a["id"], user_id=user_id, goal="task A")
    task_b = AgentTask(agent_id=agent_b["id"], user_id=user_id, goal="task B")
    db.add_all([task_a, task_b])
    await db.flush()
    execution_a = AgentExecution(
        agent_id=agent_a["id"],
        agent_version_id=agent_a["current_version_id"],
        agent_version=1,
        user_id=user_id,
        mode="agent",
        status="completed",
        input_text="A",
    )
    execution_b = AgentExecution(
        agent_id=agent_b["id"],
        agent_version_id=agent_b["current_version_id"],
        agent_version=1,
        user_id=user_id,
        mode="agent",
        status="completed",
        input_text="B",
    )
    db.add_all([execution_a, execution_b])
    await db.commit()

    executions_a = await client.get(
        f"{WORKSPACES}/agents/{agent_a['id']}/executions", headers=admin
    )
    assert executions_a.status_code == 200, executions_a.text
    execution_ids = {row["id"] for row in executions_a.json()}
    assert execution_a.id in execution_ids
    assert execution_b.id not in execution_ids
    assert {row["agent_id"] for row in executions_a.json()} == {agent_a["id"]}

    tasks_a = await client.get(
        f"{WORKSPACES}/agents/{agent_a['id']}/tasks", headers=admin
    )
    assert tasks_a.status_code == 200, tasks_a.text
    task_ids = {row["id"] for row in tasks_a.json()}
    assert task_a.id in task_ids
    assert task_b.id not in task_ids
    assert {row["agent_id"] for row in tasks_a.json()} == {agent_a["id"]}


async def test_file_root_upload_read_and_path_safety(
    client: AsyncClient, admin: dict[str, str], tmp_path: Path
) -> None:
    agent = await published_agent(client, admin)
    root = await create_root(client, admin)
    await grant_agent(client, admin, root["id"], agent["id"], "write")

    roots = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots", headers=admin
    )
    assert roots.status_code == 200, roots.text
    assert [item["id"] for item in roots.json()] == [root["id"]]
    assert roots.json()[0]["permission"] == "write"

    uploaded = await upload(
        client, admin, agent["id"], root["id"], "docs/hello.txt", b"hello root"
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["path"] == "docs/hello.txt"

    entries = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/entries",
        headers=admin,
        params={"path": "docs"},
    )
    assert entries.status_code == 200, entries.text
    assert [(row["name"], row["type"]) for row in entries.json()] == [
        ("hello.txt", "file")
    ]

    content = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "docs/hello.txt"},
    )
    assert content.status_code == 200, content.text
    assert content.content == b"hello root"

    for bad in ("../escape.txt", "/etc/passwd", "docs/../../escape.txt"):
        response = await client.get(
            f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
            headers=admin,
            params={"path": bad},
        )
        assert response.status_code == 422, (bad, response.text)
    bad_upload = await upload(
        client, admin, agent["id"], root["id"], "../escape.txt"
    )
    assert bad_upload.status_code == 422, bad_upload.text

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    root_dir = Path(get_settings().storage_dir) / "file_roots" / root["id"]
    (root_dir / "outside-link").symlink_to(outside)
    escaped = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "outside-link"},
    )
    assert escaped.status_code == 422, escaped.text

    listed_root = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/entries",
        headers=admin,
    )
    assert listed_root.status_code == 200
    assert "outside-link" not in {row["name"] for row in listed_root.json()}


async def test_task_cannot_be_continued_across_agents(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    agent_a = await published_agent(client, admin)
    agent_b = await published_agent(client, admin)
    me = await client.get(f"{API}/auth/me", headers=admin)
    task = AgentTask(agent_id=agent_a["id"], user_id=me.json()["id"], goal="A task")
    db.add(task)
    await db.commit()

    response = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": agent_b["id"], "input": "continue", "task_id": task.id},
    )
    assert response.status_code == 409, response.text


async def test_file_access_intersects_user_and_agent_permissions(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent = await published_agent(client, admin)
    root = await create_root(client, admin)
    await grant_agent(client, admin, root["id"], agent["id"], "write")
    uploaded = await upload(
        client, admin, agent["id"], root["id"], "shared/note.txt", b"shared"
    )
    assert uploaded.status_code == 201, uploaded.text

    _, developer_headers, developer_id = await create_user(
        client, admin, role="developer"
    )

    # Agent grant alone is not enough for a non-owner user.
    roots = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots", headers=developer_headers
    )
    assert roots.status_code == 200, roots.text
    assert roots.json() == []
    denied = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
        headers=developer_headers,
        params={"path": "shared/note.txt"},
    )
    assert denied.status_code == 404, denied.text
    denied_upload = await upload(
        client, developer_headers, agent["id"], root["id"], "shared/other.txt"
    )
    assert denied_upload.status_code == 404, denied_upload.text

    grant = await client.post(
        f"{API}/permissions/grant",
        headers=admin,
        json={
            "resource_type": "file_root",
            "resource_id": root["id"],
            "subject_type": "user",
            "subject_id": developer_id,
            "actions": ["read"],
        },
    )
    assert grant.status_code == 201, grant.text

    roots = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots", headers=developer_headers
    )
    assert roots.status_code == 200, roots.text
    assert [item["id"] for item in roots.json()] == [root["id"]]
    # Effective permission is the intersection: agent write + user read.
    assert roots.json()[0]["permission"] == "read"
    readable = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
        headers=developer_headers,
        params={"path": "shared/note.txt"},
    )
    assert readable.status_code == 200, readable.text
    assert readable.content == b"shared"
    still_denied = await upload(
        client, developer_headers, agent["id"], root["id"], "shared/other.txt"
    )
    assert still_denied.status_code == 404, still_denied.text

    regrant = await client.post(
        f"{API}/permissions/grant",
        headers=admin,
        json={
            "resource_type": "file_root",
            "resource_id": root["id"],
            "subject_type": "user",
            "subject_id": developer_id,
            "actions": ["read", "write"],
        },
    )
    assert regrant.status_code == 201, regrant.text
    allowed = await upload(
        client, developer_headers, agent["id"], root["id"], "shared/other.txt"
    )
    assert allowed.status_code == 201, allowed.text


async def test_shared_root_and_revocation(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent_a = await published_agent(client, admin)
    agent_b = await published_agent(client, admin)
    root = await create_root(client, admin)
    await grant_agent(client, admin, root["id"], agent_a["id"], "write")
    await grant_agent(client, admin, root["id"], agent_b["id"], "read")

    uploaded = await upload(
        client, admin, agent_a["id"], root["id"], "shared/report.txt", b"shared root"
    )
    assert uploaded.status_code == 201, uploaded.text

    grants = await client.get(
        f"{API}/file-roots/{root['id']}/permissions", headers=admin
    )
    assert grants.status_code == 200, grants.text
    by_agent = {row["agent_id"]: row for row in grants.json()}
    assert by_agent[agent_a["id"]]["permission"] == "write"
    assert by_agent[agent_b["id"]]["permission"] == "read"
    assert by_agent[agent_b["id"]]["agent_name"] == agent_b["name"]

    # T07: both agents can read the shared root.
    read_b = await client.get(
        f"{WORKSPACES}/agents/{agent_b['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "shared/report.txt"},
    )
    assert read_b.status_code == 200, read_b.text
    assert read_b.content == b"shared root"
    read_only_upload = await upload(
        client, admin, agent_b["id"], root["id"], "shared/b.txt"
    )
    assert read_only_upload.status_code == 403, read_only_upload.text

    # T08/T10: revoking B takes effect on the next request; A is unaffected.
    revoked = await client.delete(
        f"{API}/file-roots/{root['id']}/permissions/{agent_b['id']}", headers=admin
    )
    assert revoked.status_code == 204, revoked.text
    denied = await client.get(
        f"{WORKSPACES}/agents/{agent_b['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "shared/report.txt"},
    )
    assert denied.status_code == 404, denied.text
    roots_b = await client.get(
        f"{WORKSPACES}/agents/{agent_b['id']}/file-roots", headers=admin
    )
    assert roots_b.json() == []
    still_ok = await client.get(
        f"{WORKSPACES}/agents/{agent_a['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "shared/report.txt"},
    )
    assert still_ok.status_code == 200, still_ok.text

    # Revoking again is a 404, and roles without agents:write cannot list grants.
    again = await client.delete(
        f"{API}/file-roots/{root['id']}/permissions/{agent_b['id']}", headers=admin
    )
    assert again.status_code == 404, again.text
    _, operator_headers, _ = await create_user(client, admin, role="operator")
    hidden = await client.get(
        f"{API}/file-roots/{root['id']}/permissions", headers=operator_headers
    )
    assert hidden.status_code == 403, hidden.text


async def test_file_delete_requires_write_and_stays_in_root(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent = await published_agent(client, admin)
    root = await create_root(client, admin)
    await grant_agent(client, admin, root["id"], agent["id"], "write")
    uploaded = await upload(client, admin, agent["id"], root["id"], "docs/a.txt", b"a")
    assert uploaded.status_code == 201, uploaded.text

    await grant_agent(client, admin, root["id"], agent["id"], "read")
    read_only = await client.delete(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/files",
        headers=admin,
        params={"path": "docs/a.txt"},
    )
    assert read_only.status_code == 403, read_only.text

    await grant_agent(client, admin, root["id"], agent["id"], "write")
    bad = await client.delete(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/files",
        headers=admin,
        params={"path": "../escape.txt"},
    )
    assert bad.status_code == 422, bad.text
    missing = await client.delete(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/files",
        headers=admin,
        params={"path": "docs/missing.txt"},
    )
    assert missing.status_code == 404, missing.text

    deleted = await client.delete(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/files",
        headers=admin,
        params={"path": "docs/a.txt"},
    )
    assert deleted.status_code == 204, deleted.text
    gone = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "docs/a.txt"},
    )
    assert gone.status_code == 404, gone.text


async def test_disabled_agent_is_hidden_and_rejects_new_work(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent = await published_agent(client, admin)
    created = await client.post(
        f"{WORKSPACES}/agents/{agent['id']}/conversations",
        headers=admin,
        json={"title": "before disable"},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    disabled = await client.post(f"{API}/agents/{agent['id']}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text

    # T14: the agent disappears from the work center and new work is rejected.
    listed = await client.get(f"{WORKSPACES}/agents", headers=admin)
    assert agent["id"] not in {row["id"] for row in listed.json()}
    rejected = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": agent["id"], "input": "still there?"},
    )
    assert rejected.status_code == 409, rejected.text
    history = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/conversations/{conversation_id}",
        headers=admin,
    )
    assert history.status_code == 404, history.text


async def test_enabled_agent_cannot_be_used_by_wrong_role(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    config_id = await create_model_config(client, admin)
    prompt_bundle = await create_prompt(client, admin)
    agent = await create_agent(
        client, admin, config_id=config_id, prompt_id=prompt_bundle["prompt"]["id"]
    )
    published = await client.post(f"{API}/agents/{agent['id']}/publish", headers=admin)
    assert published.status_code == 200, published.text

    _, operator_headers, _ = await create_user(client, admin, role="operator")
    listed = await client.get(f"{WORKSPACES}/agents", headers=operator_headers)
    assert listed.status_code == 200, listed.text
    assert agent["id"] not in {row["id"] for row in listed.json()}
    denied = await client.post(
        f"{API}/executions",
        headers=operator_headers,
        json={"agent_id": agent["id"], "input": "blocked"},
    )
    assert denied.status_code == 403, denied.text


async def test_execution_requires_explicit_agent(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    response = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"input": "no agent selected"},
    )
    assert response.status_code == 422, response.text


async def test_execution_rejects_cross_agent_conversation(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    agent_a = await published_agent(client, admin)
    agent_b = await published_agent(client, admin)
    created = await client.post(
        f"{WORKSPACES}/agents/{agent_a['id']}/conversations",
        headers=admin,
        json={"title": "A only"},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    response = await client.post(
        f"{API}/executions",
        headers=admin,
        json={
            "agent_id": agent_b["id"],
            "conversation_id": conversation_id,
            "input": "try to cross",
        },
    )
    assert response.status_code == 409, response.text

    from app.modules.execution.repository import ExecutionRepository

    messages = await ExecutionRepository(db).list_messages(conversation_id)
    assert messages == []


async def test_runs_bind_the_published_agent_version(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent = await published_agent(client, admin)
    first = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": agent["id"], "input": "version one"},
    )
    assert first.status_code == 201, first.text
    first_execution = first.json()
    assert first_execution["agent_version"] == 1
    assert first_execution["agent_version_id"] == agent["current_version_id"]
    await wait_for_execution(client, admin, first_execution["id"])

    second_publish = await client.post(f"{API}/agents/{agent['id']}/publish", headers=admin)
    assert second_publish.status_code == 200, second_publish.text
    assert second_publish.json()["current_version"] == 2

    second = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": agent["id"], "input": "version two"},
    )
    assert second.status_code == 201, second.text
    second_execution = second.json()
    assert second_execution["agent_version"] == 2
    assert second_execution["agent_version_id"] == second_publish.json()["current_version_id"]
    await wait_for_execution(client, admin, second_execution["id"])

    detail = await client.get(
        f"{API}/executions/{first_execution['id']}", headers=admin
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["agent_version"] == 1


async def test_legacy_conversation_requires_usable_agent(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    missing = await client.post(
        f"{API}/conversations",
        headers=admin,
        json={"agent_id": "does-not-exist", "title": "legacy"},
    )
    assert missing.status_code == 404, missing.text

    agent = await published_agent(client, admin)
    disabled = await client.post(f"{API}/agents/{agent['id']}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text
    rejected = await client.post(
        f"{API}/conversations",
        headers=admin,
        json={"agent_id": agent["id"], "title": "legacy"},
    )
    assert rejected.status_code == 404, rejected.text


async def test_resume_rechecks_current_agent_access(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    agent = await published_agent(client, admin)
    me = await client.get(f"{API}/auth/me", headers=admin)
    execution = AgentExecution(
        agent_id=agent["id"],
        agent_version_id=agent["current_version_id"],
        agent_version=1,
        user_id=me.json()["id"],
        mode="agent",
        status="failed",
        input_text="resume me",
    )
    db.add(execution)
    await db.commit()

    disabled = await client.post(f"{API}/agents/{agent['id']}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text
    resumed = await client.post(
        f"{API}/executions/{execution.id}/resume", headers=admin
    )
    assert resumed.status_code == 409, resumed.text


async def test_file_root_administration_is_owner_scoped(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    _, developer_headers, _ = await create_user(client, admin, role="developer")
    own = await client.post(
        f"{API}/file-roots",
        headers=developer_headers,
        json={"name": unique_name("dev-root"), "description": "developer owned"},
    )
    assert own.status_code == 201, own.text
    admin_root = await create_root(client, admin)

    listed = await client.get(f"{API}/file-roots", headers=developer_headers)
    assert listed.status_code == 200, listed.text
    ids = {row["id"] for row in listed.json()}
    assert own.json()["id"] in ids
    assert admin_root["id"] not in ids

    agent = await published_agent(client, admin)
    denied = await client.post(
        f"{API}/file-roots/{admin_root['id']}/permissions",
        headers=developer_headers,
        json={"agent_id": agent["id"], "permission": "write"},
    )
    assert denied.status_code == 404, denied.text

    all_roots = await client.get(f"{API}/file-roots", headers=admin)
    assert {row["id"] for row in all_roots.json()} >= {
        own.json()["id"],
        admin_root["id"],
    }


async def test_deleting_a_symlink_keeps_the_target(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent = await published_agent(client, admin)
    root = await create_root(client, admin)
    await grant_agent(client, admin, root["id"], agent["id"], "write")
    uploaded = await upload(
        client, admin, agent["id"], root["id"], "docs/keep.txt", b"keep me"
    )
    assert uploaded.status_code == 201, uploaded.text

    root_dir = Path(get_settings().storage_dir) / "file_roots" / root["id"] / "docs"
    (root_dir / "link.txt").symlink_to(root_dir / "keep.txt")

    deleted = await client.delete(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/files",
        headers=admin,
        params={"path": "docs/link.txt"},
    )
    assert deleted.status_code == 204, deleted.text
    assert not (root_dir / "link.txt").exists()
    kept = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/file-roots/{root['id']}/content",
        headers=admin,
        params={"path": "docs/keep.txt"},
    )
    assert kept.status_code == 200, kept.text
    assert kept.content == b"keep me"


async def test_conversations_and_runs_bind_file_root_workspaces(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    agent = await published_agent(client, admin)
    root_a = await create_root(client, admin)
    root_b = await create_root(client, admin)
    await grant_agent(client, admin, root_a["id"], agent["id"], "write")
    await grant_agent(client, admin, root_b["id"], agent["id"], "write")

    created = await client.post(
        f"{WORKSPACES}/agents/{agent['id']}/conversations",
        headers=admin,
        json={"title": "workspace A", "root_id": root_a["id"]},
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["root_id"] == root_a["id"]

    execution = await client.post(
        f"{API}/executions",
        headers=admin,
        json={
            "agent_id": agent["id"],
            "root_id": root_a["id"],
            "conversation_id": conversation["id"],
            "input": "inside workspace A",
        },
    )
    assert execution.status_code == 201, execution.text
    await wait_for_execution(client, admin, execution.json()["id"])

    fetched = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/conversations/{conversation['id']}",
        headers=admin,
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["root_id"] == root_a["id"]

    autogenerated = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": agent["id"], "root_id": root_b["id"], "input": "new B session"},
    )
    assert autogenerated.status_code == 201, autogenerated.text
    await wait_for_execution(client, admin, autogenerated.json()["id"])
    auto_conversation = await client.get(
        f"{WORKSPACES}/agents/{agent['id']}/conversations/{autogenerated.json()['conversation_id']}",
        headers=admin,
    )
    assert auto_conversation.status_code == 200, auto_conversation.text
    assert auto_conversation.json()["root_id"] == root_b["id"]

    cross = await client.post(
        f"{API}/executions",
        headers=admin,
        json={
            "agent_id": agent["id"],
            "root_id": root_b["id"],
            "conversation_id": conversation["id"],
            "input": "wrong workspace",
        },
    )
    assert cross.status_code == 409, cross.text

    ungranted = await create_root(client, admin)
    denied = await client.post(
        f"{WORKSPACES}/agents/{agent['id']}/conversations",
        headers=admin,
        json={"title": "denied", "root_id": ungranted["id"]},
    )
    assert denied.status_code == 404, denied.text
