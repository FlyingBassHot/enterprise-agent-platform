"""Tests for the postgrad-admission-ml MCP (business logic + stdio)."""

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

import admission_ml as ml  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "postgrad-data"
    monkeypatch.setenv("POSTGRAD_DATA_DIR", str(target))
    return target


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    assert result.content
    return json.loads(result.content[0].text)


def assert_unavailable(payload: dict[str, Any]) -> None:
    assert payload["status"] == "prediction_unavailable"
    assert payload["prediction_unavailable"] is True
    assert payload["probability"] is None
    assert payload["difficulty_band"] is None
    assert payload["model_id"] is None
    assert payload["metrics"] is None
    assert payload["warnings"]


def test_list_models_is_empty_and_does_not_seed_artifacts(store: Path) -> None:
    result = ml.list_models()

    assert result["status"] == "ok"
    assert result["models"] == []
    assert result["count"] == 0
    assert result["prediction_available"] is False
    assert result["registry_present"] is False
    assert not ml.model_registry_path().exists()
    assert len(result["model_requirements"]) >= 3


def test_get_model_info_never_invents_metadata() -> None:
    result = ml.get_model_info("model_does_not_exist")

    assert result["status"] == "model_not_found"
    assert result["model"] is None
    assert result["reason"] == "no_evaluated_calibrated_model"
    assert ml.get_model_info("  ")["status"] == "invalid_request"


def test_predict_without_calibrated_model_returns_null_probability() -> None:
    result = ml.predict_admission(
        exam_year=2026,
        school="示例大学",
        program_code="085400",
        college="计算机学院",
        campus="主校区",
        estimated_total_score=350,
        subject_scores={"101": 72, "201": 68, "301": 110},
    )

    assert_unavailable(result)
    assert result["request"] == {
        "exam_year": 2026,
        "school": "示例大学",
        "program_code": "085400",
        "college": "计算机学院",
        "campus": "主校区",
        "estimated_total_score": 350.0,
        "subject_scores": {"101": 72.0, "201": 68.0, "301": 110.0},
    }


def test_predict_rejects_invalid_inputs_without_a_probability() -> None:
    cases = [
        {"exam_year": 1999, "school": "示例大学", "program_code": "085400"},
        {"exam_year": 2026, "school": "", "program_code": "085400"},
        {"exam_year": 2026, "school": "示例大学", "program_code": ""},
        {"exam_year": 2026, "school": "示例大学", "program_code": "085400", "estimated_total_score": 5000},
        {"exam_year": 2026, "school": "示例大学", "program_code": "085400", "estimated_total_score": float("nan")},
        {"exam_year": 2026, "school": "示例大学", "program_code": "085400", "subject_scores": {}},
        {"exam_year": 2026, "school": "示例大学", "program_code": "085400", "subject_scores": {"101": 400}},
        {"exam_year": True, "school": "示例大学", "program_code": "085400"},
    ]

    for kwargs in cases:
        result = ml.predict_admission(**kwargs)
        assert result["status"] == "invalid_request", (kwargs, result)
        assert result["prediction_unavailable"] is True
        assert result["probability"] is None
        assert result["errors"]


def test_compare_models_returns_no_synthetic_comparison() -> None:
    empty = ml.compare_models()
    assert_unavailable(empty)
    assert empty["comparison"] == []

    requested = ml.compare_models(["model-a", "model-b"])
    assert_unavailable(requested)
    assert requested["requested_model_ids"] == ["model-a", "model-b"]
    assert requested["comparison"] == []

    invalid = ml.compare_models(["model-a", " "])
    assert invalid["status"] == "invalid_request"
    assert invalid["probability"] is None
    assert ml.compare_models([])["status"] == "invalid_request"


def test_explain_prediction_has_no_fabricated_attribution() -> None:
    result = ml.explain_prediction("pred_missing", model_id="model-a")

    assert_unavailable(result)
    assert result["prediction_id"] == "pred_missing"
    assert result["requested_model_id"] == "model-a"
    assert result["contributions"] == []
    assert ml.explain_prediction("  ")["status"] == "invalid_request"


def test_validation_metrics_are_unavailable_not_estimated() -> None:
    result = ml.get_validation_metrics("model-a")

    assert result["status"] == "unavailable"
    assert result["model_id"] == "model-a"
    assert result["metrics"] is None
    assert result["validation"] is None
    assert result["reason"] == "no_evaluated_calibrated_model"
    assert ml.get_validation_metrics("")["status"] == "invalid_request"


def test_stdio_server_advertises_exactly_six_tools(store: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> tuple[set[str], dict[str, Any], dict[str, Any]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_DIR / "admission_ml.py")],
            env={"POSTGRAD_DATA_DIR": str(store)},
        )
        async with (
            stdio_client(params) as (read, write_stream),
            ClientSession(read, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {(tool.output_schema or {}).get("type") for tool in listed.tools} == {"object"}
            prediction = await session.call_tool(
                "predict_admission",
                {"exam_year": 2026, "school": "示例大学", "program_code": "085400"},
            )
            assert prediction.is_error is False
            models = await session.call_tool("list_models", {})
            assert models.is_error is False
            return names, _payload(prediction), _payload(models)

    names, prediction, models = asyncio.run(scenario())
    assert names == {
        "list_models",
        "get_model_info",
        "predict_admission",
        "compare_models",
        "explain_prediction",
        "get_validation_metrics",
    }
    assert_unavailable(prediction)
    assert models["models"] == []
