"""Tests for the postgrad-university-data MCP (business logic + stdio)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import university_data as ud  # noqa: E402

SOURCE: dict[str, Any] = {
    "url": "https://yz.example.edu.cn/2026/retest-line.html",
    "title": "2026年硕士研究生复试基本分数线",
    "publisher": "示例大学研究生院",
    "published_at": "2026-03-20",
    "retrieved_at": "2026-11-18T02:00:00+00:00",
}


def make_program(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "school": "示例大学",
        "school_code": "10001",
        "college": "计算机学院",
        "program_code": "085400",
        "program_name": "电子信息",
        "campus": "主校区",
        "degree_type": "professional",
        "study_mode": "full_time",
        "exam_year": 2026,
        "exam_subjects": [{"code": "101", "name": "思想政治理论"}, {"code": "408", "name": "计算机学科专业基础"}],
        "enrollment": 30,
    }
    data.update(overrides)
    return data


def make_record(score_type: str, score: float = 350.0, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "score_type": score_type,
        "score": score,
        "score_scope": "total",
        "evidence_span": f"学院复试基本分数线：{score}（{score_type}，示例）",
    }
    data.update(overrides)
    return data


def write(source: dict[str, Any] | None, program: dict[str, Any] | None, record: dict[str, Any] | None) -> dict[str, Any]:
    return ud.upsert_verified_record(
        source=source if source is not None else dict(SOURCE),
        program=program if program is not None else make_program(),
        record=record if record is not None else make_record("program_retest_line"),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "postgrad-data"
    monkeypatch.setenv("POSTGRAD_DATA_DIR", str(target))
    monkeypatch.delenv("POSTGRAD_ALLOW_VERIFIED_WRITE", raising=False)
    return target


@pytest.fixture
def writable(store: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("POSTGRAD_ALLOW_VERIFIED_WRITE", "1")
    return store


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    assert result.content
    return json.loads(result.content[0].text)


def test_ping_reports_empty_store_without_creating_database(store: Path) -> None:
    result = ud.ping()

    assert result["status"] == "ok"
    assert result["server"] == "postgrad-university-data"
    assert result["write_enabled"] is False
    assert result["database_exists"] is False
    assert result["counts"] == {"programs": 0, "admission_records": 0, "sources": 0}
    assert not ud.database_path().exists()


def test_search_programs_on_missing_database_returns_empty(store: Path) -> None:
    result = ud.search_programs(school="示例大学")

    assert result["status"] == "empty"
    assert result["count"] == 0
    assert result["items"] == []
    assert not ud.database_path().exists()


def test_upsert_is_hidden_when_write_gate_is_closed(store: Path) -> None:
    result = write(None, None, None)

    assert result["status"] == "disabled"
    assert "POSTGRAD_ALLOW_VERIFIED_WRITE" in result["message"]
    assert not ud.database_path().exists()


def test_verified_write_rejects_missing_evidence_and_untrusted_payloads(writable: Path) -> None:
    no_span = make_record("program_retest_line")
    no_span["evidence_span"] = "  "
    cases = [
        (None, None, no_span),
        ({**SOURCE, "url": "ftp://yz.example.edu.cn/line.html"}, None, None),
        ({**SOURCE, "retrieved_at": "2026-11-18"}, None, None),
        ({**SOURCE, "verified": True}, None, None),
        (None, {**make_program(), "degree_type": "unknown"}, None),
        (None, None, make_record("program_retest_line", score=1200)),
    ]

    for source, program, record in cases:
        result = write(source, program, record)
        assert result["status"] == "rejected", (source, program, record, result)
        assert result["errors"]
    assert not ud.database_path().exists()


def test_search_keeps_campus_and_year_separate(writable: Path) -> None:
    assert write(None, make_program(campus="主校区", exam_year=2026), make_record("program_retest_line", 350))[
        "status"
    ] == "created"
    assert write(None, make_program(campus="分校区", exam_year=2026), make_record("program_retest_line", 330))[
        "status"
    ] == "created"
    assert write(None, make_program(campus="主校区", exam_year=2025), make_record("program_retest_line", 340))[
        "status"
    ] == "created"

    all_rows = ud.search_programs(school="示例大学", program_code="085400")
    assert all_rows["count"] == 3

    main_2026 = ud.search_programs(school="示例大学", campus="主校区", exam_year=2026)
    assert main_2026["count"] == 1
    assert main_2026["items"][0]["campus"] == "主校区"
    assert main_2026["items"][0]["exam_year"] == 2026
    assert main_2026["items"][0]["record_count"] == 1

    other_campus = ud.search_programs(school="示例大学", campus="分校区")
    assert other_campus["count"] == 1
    assert other_campus["items"][0]["exam_year"] == 2026


def test_academic_and_professional_programs_are_distinct(writable: Path) -> None:
    first = write(None, make_program(degree_type="professional"), make_record("school_line"))
    second = write(None, make_program(degree_type="academic"), make_record("school_line"))

    assert first["status"] == "created"
    assert second["status"] == "created"
    assert first["program_id"] != second["program_id"]
    assert ud.search_programs(school="示例大学", degree_type="academic")["count"] == 1
    assert ud.search_programs(school="示例大学", degree_type="professional")["count"] == 1


def test_five_score_lines_stay_distinct_and_update_in_place(writable: Path) -> None:
    scores = {
        "national_line": 273,
        "school_line": 310,
        "college_line": 320,
        "program_retest_line": 335,
        "admitted_min_score": 341,
    }
    program_id = None
    for score_type, score in scores.items():
        result = write(None, None, make_record(score_type, score))
        assert result["status"] == "created"
        assert result["retrieved_at"].startswith("2026-11-18T02:00:00")
        program_id = result["program_id"]
    assert program_id is not None

    history = ud.get_admission_history(program_id)
    assert history["status"] == "ok"
    assert history["count"] == 5
    assert history["years"] == [2026]
    assert set(history["score_type_labels"]) == set(scores)
    assert set(history["score_type_notes"]) == set(scores)
    by_type = {row["score_type"]: row for row in history["records"]}
    assert set(by_type) == set(scores)
    for score_type, score in scores.items():
        assert by_type[score_type]["score"] == score
        assert by_type[score_type]["score_type_label"] == ud.SCORE_TYPE_LABELS[score_type]
        assert by_type[score_type]["evidence_span"]
        assert by_type[score_type]["retrieved_at"].startswith("2026-11-18T02:00:00")
    assert by_type["national_line"]["score"] != by_type["admitted_min_score"]["score"]

    updated = write(None, None, make_record("admitted_min_score", 344, admitted_count=28))
    assert updated["status"] == "updated"
    assert updated["record_id"] == by_type["admitted_min_score"]["record_id"]

    after = ud.get_admission_history(program_id)
    assert after["count"] == 5
    admitted = {row["score_type"]: row for row in after["records"]}["admitted_min_score"]
    assert admitted["score"] == 344
    assert admitted["admitted_count"] == 28


def test_history_reports_unknown_program_instead_of_inventing_data(store: Path) -> None:
    result = ud.get_admission_history("prog_missing")

    assert result["status"] == "not_found"
    assert result["program"] is None
    assert result["records"] == []


def test_source_evidence_links_programs_and_every_record(writable: Path) -> None:
    first = write(None, None, make_record("school_line", 310))
    second = write(None, None, make_record("admitted_min_score", 341))
    assert first["source_id"] == second["source_id"]

    evidence = ud.get_source_evidence(first["source_id"])
    assert evidence["status"] == "ok"
    assert evidence["source"]["url"] == SOURCE["url"]
    assert evidence["source"]["retrieved_at"].startswith("2026-11-18T02:00:00")
    assert evidence["source"]["published_at"] == "2026-03-20"
    assert evidence["count"] == 2
    assert {row["score_type"] for row in evidence["records"]} == {"school_line", "admitted_min_score"}
    assert all(row["evidence_span"] for row in evidence["records"])
    assert all(row["retrieved_at"].startswith("2026-11-18T02:00:00") for row in evidence["records"])
    assert len(evidence["programs"]) == 1

    missing = ud.get_source_evidence("src_missing")
    assert missing["status"] == "not_found"
    assert missing["source"] is None


def test_invalid_search_arguments_are_rejected(writable: Path) -> None:
    assert ud.search_programs(exam_year=1990)["status"] == "invalid_request"
    assert ud.search_programs(limit=0)["status"] == "invalid_request"
    assert ud.search_programs(school=123)["status"] == "invalid_request"
    assert ud.search_programs(degree_type="doctoral")["status"] == "invalid_request"


def test_stdio_server_advertises_read_tools_and_ping(store: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> tuple[set[str], dict[str, Any]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_DIR / "university_data.py")],
            env={"POSTGRAD_DATA_DIR": str(store)},
        )
        async with (
            stdio_client(params) as (read, write_stream),
            ClientSession(read, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            output_types = {(tool.output_schema or {}).get("type") for tool in listed.tools}
            assert output_types == {"object"}
            result = await session.call_tool("ping", {})
            assert result.is_error is False
            return names, _payload(result)

    names, payload = asyncio.run(scenario())
    assert names == {"ping", "search_programs", "get_admission_history", "get_source_evidence"}
    assert payload["status"] == "ok"
    assert payload["write_enabled"] is False


def test_stdio_server_exposes_write_tool_only_behind_gate(store: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> set[str]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_DIR / "university_data.py")],
            env={"POSTGRAD_DATA_DIR": str(store), "POSTGRAD_ALLOW_VERIFIED_WRITE": "1"},
        )
        async with (
            stdio_client(params) as (read, write_stream),
            ClientSession(read, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            return {tool.name for tool in listed.tools}

    names = asyncio.run(scenario())
    assert "upsert_verified_record" in names
    assert len(names) == 5
