from __future__ import annotations

import io
import json
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.contracts import ToolInvocation, UserContext
from app.domain_plugins.audit import register as register_audit
from app.modules.execution.artifacts import ArtifactService
from app.modules.permission.service import PermissionService
from app.modules.tool.service import ToolService
from tests.conftest import admin_headers

ROOT = Path(__file__).resolve().parents[1]


async def _actor(client: AsyncClient, db: AsyncSession) -> UserContext:
    headers = await admin_headers(client)
    me = await client.get("/api/v1/auth/me", headers=headers)
    return await PermissionService(db).load_context(me.json()["id"])


async def _create_artifact(db: AsyncSession, actor: UserContext) -> str:
    data = (ROOT / "seed" / "audit_sample.xlsx").read_bytes()
    upload = UploadFile(
        file=io.BytesIO(data), filename="audit_sample.xlsx", size=len(data)
    )
    artifact = await ArtifactService(db).save(actor, upload)
    return artifact.id


async def test_audit_tools_execute_through_real_artifact_and_tool_service(
    client: AsyncClient, db: AsyncSession
) -> None:
    actor = await _actor(client, db)
    artifact_id = await _create_artifact(db, actor)

    register_audit()
    tools = ToolService(db)
    await tools.sync_native()

    analysis = await tools.execute(
        ToolInvocation(
            tool_name="analyze_transactions",
            arguments={"artifact_id": artifact_id},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert analysis.ok, analysis.error
    output = analysis.output
    assert output["filename"] == "audit_sample.xlsx"
    assert output["summary"]["total_transactions"] > 0
    assert output["anomalies"]

    labels = json.loads((ROOT / "seed" / "audit_labels.json").read_text())
    expected = {
        (item["rule"], item["row_number"])
        for item in labels["anomalies"]
        if item["rule"] in labels["labeled_rules"]
    }
    detected_all = {
        (item["rule"], item["row_number"]) for item in output["anomalies"]
    }
    detected = {
        (rule, row) for rule, row in detected_all if rule in labels["labeled_rules"]
    }
    true_positives = expected & detected
    precision = len(true_positives) / len(detected) if detected else 1.0
    recall = len(true_positives) / len(expected) if expected else 1.0
    print(f"tool audit precision={precision:.3f} recall={recall:.3f}")
    assert recall >= 0.9
    assert precision >= 0.8

    row_numbers = {item["row_number"] for item in output["anomalies"]}
    assert all(isinstance(row, int) and row >= 2 for row in row_numbers)

    report = await tools.execute(
        ToolInvocation(
            tool_name="generate_audit_report",
            arguments={"artifact_id": artifact_id},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert report.ok, report.error
    markdown = report.output["markdown"] if isinstance(report.output, dict) else report.output
    assert "重复付款" in markdown
    assert "审计" in markdown


async def test_audit_tool_denies_read_of_foreign_artifact(
    client: AsyncClient, db: AsyncSession
) -> None:
    actor = await _actor(client, db)
    artifact_id = await _create_artifact(db, actor)

    response = await client.post(
        "/api/v1/users",
        headers=await admin_headers(client),
        json={
            "username": "audit-developer",
            "password": "developer-pass-123",
            "display_name": "Audit Developer",
            "role_names": ["developer"],
        },
    )
    assert response.status_code in (201, 409), response.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "audit-developer", "password": "developer-pass-123"},
    )
    assert login.status_code == 200
    other = await PermissionService(db).load_context(login.json()["user"]["id"])

    register_audit()
    tools = ToolService(db)
    await tools.sync_native()
    result = await tools.execute(
        ToolInvocation(
            tool_name="analyze_transactions",
            arguments={"artifact_id": artifact_id},
            user_id=other.user_id,
        ),
        other,
    )
    assert result.ok is False
    assert "artifact" in (result.error or "").lower() or "读取" in (result.error or "")


async def test_audit_tool_respects_workspace_scope(
    client: AsyncClient, db: AsyncSession
) -> None:
    from app.core.ids import new_id
    from app.modules.execution.models import AgentExecution
    from app.modules.execution.repository import ExecutionRepository

    actor = await _actor(client, db)
    artifact_id = await _create_artifact(db, actor)
    register_audit()
    tools = ToolService(db)
    await tools.sync_native()

    execution = AgentExecution(
        id=new_id(),
        agent_id="agent-1",
        user_id=actor.user_id,
        status="running",
        input_text="audit",
        input_payload={"input": "audit", "artifact_ids": []},
    )
    await ExecutionRepository(db).create_execution(execution)
    await db.commit()

    invocation = ToolInvocation(
        tool_name="analyze_transactions",
        arguments={"artifact_id": artifact_id},
        user_id=actor.user_id,
        execution_id=execution.id,
    )
    outside = await tools.execute(invocation, actor)
    assert outside.ok is False
    assert "workspace" in (outside.error or "")

    execution.input_payload = {
        **(execution.input_payload or {}),
        "artifact_ids": [artifact_id],
    }
    await db.commit()

    inside = await tools.execute(invocation, actor)
    assert inside.ok, inside.error
    assert inside.output["filename"] == "audit_sample.xlsx"
