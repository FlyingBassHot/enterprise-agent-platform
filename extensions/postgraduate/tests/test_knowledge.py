"""Focused tests for the postgrad exam-knowledge MCP.

Run from the repository root:
    backend/.venv/bin/python -m pytest extensions/postgraduate/tests/test_knowledge.py -q
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1] / "mcp"
sys.path.insert(0, str(MCP_DIR))

import exam_knowledge as ek  # noqa: E402

COURSE = {
    "school_id": "10001",
    "college_id": "CS",
    "program_code": "081200",
    "exam_year": 2026,
    "subject_code": "408",
}
EVIDENCE = {
    "document_ref": "syllabus-408-2026.pdf",
    "title": "2026 年 408 考试大纲",
    "publisher": "Example University",
    "evidence_span": "p.12 第2章 数据结构",
    "retrieved_at": "2026-11-01",
}


def call(server, tool: str, **arguments):
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error, result
    text = "".join(block.text for block in result.content if getattr(block, "type", "") == "text")
    return json.loads(text)


def tool_names(server) -> set[str]:
    return {tool.name for tool in asyncio.run(server.list_tools())}


def make_node(
    server,
    name: str,
    node_type: str = "knowledge_point",
    parent_id: str = "",
    order_index: int = 0,
    course: dict | None = None,
) -> str:
    course = course or COURSE
    proposed = call(
        server,
        "propose_node",
        **course,
        node_type=node_type,
        name=name,
        parent_id=parent_id,
        order_index=order_index,
        evidence=EVIDENCE,
        proposed_by="test-suite",
    )
    assert proposed["ok"], proposed
    reviewed = call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[{"proposal_id": proposed["proposal_id"], "decision": "approve"}],
    )
    assert reviewed["ok"], reviewed
    return reviewed["results"][0]["result_id"]


def test_tool_surface_and_review_env_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTGRAD_ALLOW_REVIEW", raising=False)
    closed = ek.build_server(data_dir=tmp_path)
    assert tool_names(closed) == {
        "ping",
        "get_syllabus",
        "search_knowledge",
        "propose_node",
        "propose_relation",
        "get_prerequisites",
        "find_questions",
        "save_verified_question",
        "record_attempt",
    }
    assert call(closed, "ping")["review_tool_enabled"] is False

    monkeypatch.setenv("POSTGRAD_ALLOW_REVIEW", "1")
    opened = ek.build_server(data_dir=tmp_path)
    assert "review_proposals" in tool_names(opened)
    assert call(opened, "ping")["review_tool_enabled"] is True


def test_five_field_course_identity_and_isolation(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    invalid = call(
        server,
        "propose_node",
        **{**COURSE, "subject_code": "   "},
        node_type="chapter",
        name="数据结构",
        evidence=EVIDENCE,
    )
    assert invalid["error"]["code"] == "invalid_course"

    invalid_year = call(
        server,
        "propose_node",
        **{**COURSE, "exam_year": 1999},
        node_type="chapter",
        name="数据结构",
        evidence=EVIDENCE,
    )
    assert invalid_year["error"]["code"] == "invalid_course"

    make_node(server, "数据结构", node_type="chapter")
    found = call(server, "search_knowledge", **COURSE, query="数据结构")
    assert found["count"] == 1
    assert found["results"][0]["verified"] is True

    other_school = call(
        server,
        "search_knowledge",
        **{**COURSE, "school_id": "10002"},
        query="数据结构",
    )
    assert other_school["count"] == 0
    other_year = call(server, "search_knowledge", **{**COURSE, "exam_year": 2027}, query="数据结构")
    assert other_year["count"] == 0


def test_proposals_invisible_until_approved_and_persisted(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    proposal = call(
        server,
        "propose_node",
        **COURSE,
        node_type="chapter",
        name="操作系统",
        evidence=EVIDENCE,
        proposed_by="student-agent",
    )
    assert proposal["ok"]
    assert proposal["status"] == "pending"
    assert call(server, "search_knowledge", **COURSE, query="操作系统")["count"] == 0
    assert call(server, "get_syllabus", **COURSE)["available"] is False

    approved = call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[{"proposal_id": proposal["proposal_id"], "decision": "approve"}],
    )
    assert approved["results"][0]["status"] == "approved"
    assert call(server, "search_knowledge", **COURSE, query="操作系统")["count"] == 1

    rejected = call(
        server,
        "propose_node",
        **COURSE,
        node_type="chapter",
        name="被拒绝的章节",
        evidence=EVIDENCE,
    )
    call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[{"proposal_id": rejected["proposal_id"], "decision": "reject"}],
    )
    assert call(server, "search_knowledge", **COURSE, query="被拒绝")["count"] == 0

    # Rebuilding the server against the same data dir preserves verified data.
    restarted = ek.build_server(data_dir=tmp_path, allow_review=False)
    syllabus = call(restarted, "get_syllabus", **COURSE)
    assert syllabus["available"] is True
    assert syllabus["chapter_count"] == 1
    assert "review_proposals" not in tool_names(restarted)


def test_verified_prerequisites_and_cycle_rejection(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    first = make_node(server, "数组")
    second = make_node(server, "链表")
    third = make_node(server, "树")

    rel1 = call(
        server,
        "propose_relation",
        **COURSE,
        src_node_id=first,
        dst_node_id=second,
        relation_type="PREREQUISITE_OF",
        evidence=EVIDENCE,
    )
    assert rel1["ok"]
    call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[{"proposal_id": rel1["proposal_id"], "decision": "approve"}],
    )
    rel2 = call(
        server,
        "propose_relation",
        **COURSE,
        src_node_id=second,
        dst_node_id=third,
        relation_type="PREREQUISITE_OF",
        evidence=EVIDENCE,
    )
    call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[{"proposal_id": rel2["proposal_id"], "decision": "approve"}],
    )

    prereqs = call(server, "get_prerequisites", **COURSE, node_id=third)
    assert prereqs["count"] == 2
    depths = {item["node"]["name"]: item["depth"] for item in prereqs["prerequisites"]}
    assert depths == {"链表": 1, "数组": 2}
    assert all(item["via"]["evidence"]["title"] for item in prereqs["prerequisites"])

    cycle = call(
        server,
        "propose_relation",
        **COURSE,
        src_node_id=third,
        dst_node_id=first,
        relation_type="PREREQUISITE_OF",
        evidence=EVIDENCE,
    )
    assert cycle["error"]["code"] == "prerequisite_cycle"

    unknown = call(
        server,
        "propose_relation",
        **COURSE,
        src_node_id=first,
        dst_node_id="node_missing",
        relation_type="RELATED_TO",
        evidence=EVIDENCE,
    )
    assert unknown["error"]["code"] == "unknown_node"

    bad_evidence = call(
        server,
        "propose_relation",
        **COURSE,
        src_node_id=first,
        dst_node_id=second,
        relation_type="RELATED_TO",
        evidence={**EVIDENCE, "document_ref": None, "url": "ftp://example.com/x"},
    )
    assert bad_evidence["error"]["code"] == "invalid_evidence"


def test_past_paper_requires_evidence_copyright_and_labels(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    node = make_node(server, "二叉树")

    no_source = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="解释二叉树的遍历方式。",
        answer="前序、中序、后序。",
        node_ids=[node],
        source_type="past_paper",
        paper_year=2025,
    )
    assert no_source["error"]["code"] == "evidence_required"

    no_copyright = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="解释二叉树的遍历方式。",
        answer="前序、中序、后序。",
        node_ids=[node],
        source_type="past_paper",
        paper_year=2025,
        source=EVIDENCE,
    )
    assert no_copyright["error"]["code"] == "copyright_note_required"

    saved = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="解释二叉树的遍历方式。",
        answer="前序、中序、后序。",
        node_ids=[node],
        source_type="past_paper",
        paper_year=2025,
        source=EVIDENCE,
        copyright_note="公开真题索引，仅摘录，未批量复制。",
    )
    assert saved["ok"]
    assert saved["originality"] == "past_paper"
    assert saved["copyright_status"] == "declared"
    assert saved["verified"] is True
    assert saved["verified_as_past_paper"] is True
    assert saved["verification_status"] == "admin_imported_past_paper"
    assert "not_online_authenticated" in saved["authenticity_check"]
    found = call(server, "find_questions", **COURSE, source_type="past_paper")
    assert found["count"] == 1
    assert found["results"][0]["originality"] == "past_paper"
    assert found["results"][0]["verified"] is True
    assert found["results"][0]["source"]["title"] == EVIDENCE["title"]

    original = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="请说明完全二叉树的性质。",
        answer="除最后一层外每层都满。",
        node_ids=[node],
        source_type="original",
    )
    assert original["ok"]
    assert original["originality"] == "original"
    assert original["source"] is None
    assert original["verified"] is False
    assert original["verification_status"] == "original_unverified"

    adapted = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="改编：分析平衡树的旋转。",
        answer="左旋或右旋。",
        node_ids=[node],
        source_type="adapted",
    )
    assert adapted["error"]["code"] == "evidence_required"

    adapted_ok = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="改编：分析平衡树的旋转。",
        answer="左旋或右旋。",
        node_ids=[node],
        source_type="adapted",
        source=EVIDENCE,
        copyright_note="管理员离线改编，附来源；未在线鉴定。",
    )
    assert adapted_ok["ok"]
    assert adapted_ok["verified"] is True
    assert adapted_ok["verified_as_past_paper"] is False
    assert adapted_ok["verification_status"] == "admin_imported_adapted"

    invalid_label = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="无效标签题目。",
        answer="x",
        node_ids=[node],
        source_type="ai_generated",
    )
    assert invalid_label["error"]["code"] == "invalid_source_type"

    pending = call(
        server,
        "propose_node",
        **COURSE,
        node_type="knowledge_point",
        name="未审核考点",
        evidence=EVIDENCE,
    )
    unverified_link = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="不能链接未审核考点的题目。",
        answer="x",
        node_ids=[pending["proposal_id"]],
        source_type="original",
    )
    assert unverified_link["error"]["code"] == "unknown_node"


def test_question_search_attempts_and_course_isolation(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    node = make_node(server, "图的遍历")
    saved = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="简述广度优先搜索的队列作用。",
        answer="保证按层访问。",
        node_ids=[node],
        source_type="original",
        difficulty="medium",
    )
    assert saved["ok"]
    assert call(server, "find_questions", **{**COURSE, "school_id": "X"})["count"] == 0

    found = call(server, "find_questions", **COURSE, query="广度优先")
    assert found["count"] == 1
    question_id = found["results"][0]["question_id"]

    first = call(
        server,
        "record_attempt",
        **COURSE,
        question_id=question_id,
        user_ref="user-1",
        answer="队列",
        is_correct=True,
        attempt_key="attempt-1",
    )
    assert first["ok"]
    assert first["stats"] == {"attempts": 1, "correct": 1, "accuracy": 1.0}

    duplicate = call(
        server,
        "record_attempt",
        **COURSE,
        question_id=question_id,
        user_ref="user-1",
        answer="队列",
        is_correct=True,
        attempt_key="attempt-1",
    )
    assert duplicate["duplicate"] is True

    second = call(
        server,
        "record_attempt",
        **COURSE,
        question_id=question_id,
        user_ref="user-1",
        answer="栈",
        is_correct=False,
        attempt_key="attempt-2",
    )
    assert second["stats"]["attempts"] == 2
    assert second["stats"]["correct"] == 1
    assert second["stats"]["accuracy"] == 0.5


def test_review_batch_is_atomic_and_duplicate_free(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    proposal = call(
        server,
        "propose_node",
        **COURSE,
        node_type="chapter",
        name="计算机网络",
        evidence=EVIDENCE,
    )
    duplicate_batch = call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[
            {"proposal_id": proposal["proposal_id"], "decision": "approve"},
            {"proposal_id": proposal["proposal_id"], "decision": "reject"},
        ],
    )
    assert duplicate_batch["error"]["code"] == "invalid_input"
    assert call(server, "search_knowledge", **COURSE, query="计算机网络")["count"] == 0

    approved = call(
        server,
        "review_proposals",
        reviewer="admin",
        decisions=[{"proposal_id": proposal["proposal_id"], "decision": "approve"}],
    )
    assert approved["ok"]
    assert call(server, "search_knowledge", **COURSE, query="计算机网络")["count"] == 1


def test_review_tool_rejects_when_not_admin(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=False)
    assert "review_proposals" not in tool_names(server)
    # Even a direct method call must fail closed.
    store = ek.KnowledgeStore(tmp_path / "exam_knowledge.sqlite3")
    store.allow_review = False
    result = store.review_proposals("admin", [])
    assert result["error"]["code"] == "forbidden"


def test_ordinary_server_cannot_save_past_paper_or_adapted(tmp_path):
    admin = ek.build_server(data_dir=tmp_path, allow_review=True)
    node = make_node(admin, "二叉树")
    ordinary = ek.build_server(data_dir=tmp_path, allow_review=False)

    for source_type in ("past_paper", "adapted"):
        denied = call(
            ordinary,
            "save_verified_question",
            **COURSE,
            question_type="short_answer",
            stem=f"普通 Agent 尝试保存 {source_type}。",
            answer="不应写入。",
            node_ids=[node],
            source_type=source_type,
            paper_year=2025 if source_type == "past_paper" else None,
            source=EVIDENCE,
            copyright_note="格式合法的来源与版权说明。",
        )
        assert denied["error"]["code"] == "admin_required"

    assert call(ordinary, "find_questions", **COURSE, source_type="past_paper")["count"] == 0

    original = call(
        ordinary,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="普通 Agent 可以保存原创练习题。",
        answer="这是原创题，不是真题。",
        node_ids=[node],
        source_type="original",
    )
    assert original["ok"]
    assert original["verified"] is False
    assert original["verified_as_past_paper"] is False
    assert original["verification_status"] == "original_unverified"

    found = call(ordinary, "find_questions", **COURSE)
    assert found["count"] == 1
    assert found["results"][0]["originality"] == "original"
    assert found["results"][0]["verified"] is False
    assert found["results"][0]["verification_status"] == "original_unverified"


def test_legacy_past_paper_is_not_verified_or_returned_by_default(tmp_path):
    server = ek.build_server(data_dir=tmp_path, allow_review=True)
    node = make_node(server, "二叉树")
    saved = call(
        server,
        "save_verified_question",
        **COURSE,
        question_type="short_answer",
        stem="管理员导入的真题样本。",
        answer="答案。",
        node_ids=[node],
        source_type="past_paper",
        paper_year=2025,
        source=EVIDENCE,
        copyright_note="管理员离线导入，仅索引摘录。",
    )
    assert saved["verified"] is True
    question_id = saved["question_id"]

    # Simulate a pre-fix database row: no admin evidence-import marker.
    with sqlite3.connect(tmp_path / "exam_knowledge.sqlite3") as conn:
        conn.execute(
            "UPDATE questions SET imported_by_admin = 0, verification_basis = '' "
            "WHERE id = ?",
            [question_id],
        )

    default = call(server, "find_questions", **COURSE)
    assert default["count"] == 0
    legacy = call(server, "find_questions", **COURSE, include_unverified=True)
    assert legacy["count"] == 1
    item = legacy["results"][0]
    assert item["verified"] is False
    assert item["verified_as_past_paper"] is False
    assert item["verification_status"] == "legacy_unverified_past_paper"
    assert "not_online_authenticated" in item["authenticity_check"]
