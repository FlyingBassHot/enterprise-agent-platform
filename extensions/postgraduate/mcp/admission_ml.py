"""postgrad-admission-ml MCP server (stdio).

Run directly::

    python extensions/postgraduate/mcp/admission_ml.py

MVP contract: this build ships **no trained, evaluated and calibrated model**.
Every prediction path therefore returns ``prediction_unavailable`` with
``probability: null``.  Nothing here reads a model file, seeds a model, or
converts a historical score line into a probability.

Model artifacts, when introduced later, must be produced offline by an admin
job and must carry temporal hold-out validation, calibration metrics
(Brier score, reliability), a feature schema, applicability limits and a
version.  Until such a registry exists, ``list_models`` is empty by design.
Storage directory (for future model metadata) follows ``POSTGRAD_DATA_DIR``
and defaults to ``extensions/postgraduate/data``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

SERVER_NAME = "postgrad-admission-ml"
SERVER_VERSION = "0.1.0"
DATA_DIR_ENV = "POSTGRAD_DATA_DIR"
MIN_EXAM_YEAR = 2000
MAX_EXAM_YEAR = 2100
MAX_TOTAL_SCORE = 1000.0
MAX_SINGLE_SUBJECT_SCORE = 300.0

_UNAVAILABLE_REASON = "no_evaluated_calibrated_model"
_UNAVAILABLE_TEXT = (
    "No trained model with temporal hold-out validation and probability calibration is installed. "
    "No admission probability is reported."
)
_WARNINGS = [
    "This server does not provide an admission probability without a validated, calibrated model.",
    "Historical score lines are not admission probabilities and must not be treated as such.",
]


def data_dir() -> Path:
    """Return the extension storage directory, honouring ``POSTGRAD_DATA_DIR``."""
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data"


def model_registry_path() -> Path:
    return data_dir() / "models" / "registry.json"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_request(
    exam_year: Any,
    school: Any,
    program_code: Any,
    college: Any,
    campus: Any,
    estimated_total_score: Any,
    subject_scores: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not _is_int(exam_year) or not MIN_EXAM_YEAR <= exam_year <= MAX_EXAM_YEAR:
        errors.append(f"exam_year must be an integer between {MIN_EXAM_YEAR} and {MAX_EXAM_YEAR}")
    for label, value in (("school", school), ("program_code", program_code)):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label} is required")
    normalized_college = None
    if college is not None:
        if not isinstance(college, str) or not college.strip():
            errors.append("college must be a non-empty string when provided")
        else:
            normalized_college = " ".join(college.split())
    normalized_campus = None
    if campus is not None:
        if not isinstance(campus, str) or not campus.strip():
            errors.append("campus must be a non-empty string when provided")
        else:
            normalized_campus = " ".join(campus.split())
    total_score = None
    if estimated_total_score is not None:
        if isinstance(estimated_total_score, bool) or not isinstance(estimated_total_score, (int, float)):
            errors.append("estimated_total_score must be a number when provided")
        else:
            total_score = float(estimated_total_score)
            if not math.isfinite(total_score) or not 0.0 <= total_score <= MAX_TOTAL_SCORE:
                errors.append(f"estimated_total_score must be finite and within 0..{MAX_TOTAL_SCORE:g}")
    normalized_subjects: dict[str, float] | None = None
    if subject_scores is not None:
        if not isinstance(subject_scores, dict) or not subject_scores:
            errors.append("subject_scores must be a non-empty object mapping subject code to score")
        else:
            normalized_subjects = {}
            for key, value in subject_scores.items():
                if not isinstance(key, str) or not key.strip():
                    errors.append("subject_scores keys must be non-empty subject codes")
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"subject_scores[{key!r}] must be a number")
                    continue
                number = float(value)
                if not math.isfinite(number) or not 0.0 <= number <= MAX_SINGLE_SUBJECT_SCORE:
                    errors.append(
                        f"subject_scores[{key!r}] must be finite and within 0..{MAX_SINGLE_SUBJECT_SCORE:g}"
                    )
                    continue
                normalized_subjects[key.strip()] = number
    if errors:
        return None, errors
    return (
        {
            "exam_year": exam_year,
            "school": " ".join(school.split()),
            "program_code": " ".join(program_code.split()),
            "college": normalized_college,
            "campus": normalized_campus,
            "estimated_total_score": total_score,
            "subject_scores": normalized_subjects,
        },
        [],
    )


def _unavailable(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "prediction_unavailable",
        "prediction_unavailable": True,
        "probability": None,
        "model_id": None,
        "model_version": None,
        "applicability": None,
        "difficulty_band": None,
        "confidence_notes": _UNAVAILABLE_TEXT,
        "top_factors": [],
        "metrics": None,
        "source_ids": [],
        "warnings": list(_WARNINGS),
        "reason": _UNAVAILABLE_REASON,
    }
    payload.update(extra)
    return payload


def list_models() -> dict[str, Any]:
    """List models that are trained, validated and calibrated.

    This MVP has no model registry, so the list is always empty.  An empty list
    is the truthful answer, not a placeholder for a hidden model.
    """

    return {
        "status": "ok",
        "models": [],
        "count": 0,
        "prediction_available": False,
        "registry_present": model_registry_path().is_file(),
        "message": _UNAVAILABLE_TEXT,
        "model_requirements": [
            "offline training with a recorded data window and feature schema",
            "temporal hold-out validation by exam year",
            "probability calibration reported with Brier score and reliability",
            "explicit applicability limits and model version",
        ],
        "warnings": list(_WARNINGS),
    }


def get_model_info(model_id: str) -> dict[str, Any]:
    """Return metadata for one validated model, or ``model_not_found``.

    No model is installed in this MVP; the call never returns invented
    metadata.
    """

    if not isinstance(model_id, str) or not model_id.strip():
        return {"status": "invalid_request", "model_id": model_id, "model": None, "errors": ["model_id is required"]}
    return {
        "status": "model_not_found",
        "model_id": model_id.strip(),
        "model": None,
        "message": _UNAVAILABLE_TEXT,
        "reason": _UNAVAILABLE_REASON,
        "warnings": list(_WARNINGS),
    }


def predict_admission(
    exam_year: int,
    school: str,
    program_code: str,
    college: str | None = None,
    campus: str | None = None,
    estimated_total_score: float | None = None,
    subject_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Predict admission for one program/year, or report why it cannot.

    Returns ``status="prediction_unavailable"`` with ``probability: null``
    whenever no evaluated and calibrated model applies.  Inputs are validated
    and echoed back; no input is turned into a probability or difficulty band
    by heuristics.
    """

    request, errors = _validate_request(
        exam_year,
        school,
        program_code,
        college,
        campus,
        estimated_total_score,
        subject_scores,
    )
    if errors or request is None:
        return {
            "status": "invalid_request",
            "prediction_unavailable": True,
            "probability": None,
            "errors": errors,
            "request": {
                "exam_year": exam_year,
                "school": school,
                "program_code": program_code,
                "college": college,
                "campus": campus,
            },
        }
    return _unavailable(request=request)


def compare_models(model_ids: list[str] | None = None) -> dict[str, Any]:
    """Compare validated models; empty and unavailable in this MVP.

    ``comparison`` stays empty and no metric is synthesized.  When model ids
    are supplied they are echoed as ``requested_model_ids`` for auditability.
    """

    errors: list[str] = []
    requested: list[str] = []
    if model_ids is not None:
        if not isinstance(model_ids, list) or not model_ids:
            errors.append("model_ids must be a non-empty list of model ids when provided")
        else:
            for model_id in model_ids:
                if not isinstance(model_id, str) or not model_id.strip():
                    errors.append("model_ids entries must be non-empty strings")
                else:
                    requested.append(model_id.strip())
    if errors:
        return {
            "status": "invalid_request",
            "prediction_unavailable": True,
            "probability": None,
            "errors": errors,
            "comparison": [],
        }
    return _unavailable(requested_model_ids=requested, comparison=[])


def explain_prediction(prediction_id: str, model_id: str | None = None) -> dict[str, Any]:
    """Explain a previous prediction, or state that none exists.

    This MVP has no predictions to explain: ``contributions`` is empty,
    ``probability`` is null and no feature attribution is fabricated.
    """

    errors: list[str] = []
    if not isinstance(prediction_id, str) or not prediction_id.strip():
        errors.append("prediction_id is required")
    if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
        errors.append("model_id must be a non-empty string when provided")
    if errors:
        return {
            "status": "invalid_request",
            "prediction_unavailable": True,
            "probability": None,
            "prediction_id": prediction_id,
            "contributions": [],
            "errors": errors,
        }
    return _unavailable(
        prediction_id=prediction_id.strip(),
        requested_model_id=model_id.strip() if model_id else None,
        contributions=[],
    )


def get_validation_metrics(model_id: str) -> dict[str, Any]:
    """Return validation and calibration metrics for one model, if any.

    No model is installed, so ``metrics`` and ``validation`` are null and the
    status is ``unavailable``.  Metrics are never estimated or back-filled.
    """

    if not isinstance(model_id, str) or not model_id.strip():
        return {
            "status": "invalid_request",
            "model_id": model_id,
            "metrics": None,
            "validation": None,
            "errors": ["model_id is required"],
        }
    return {
        "status": "unavailable",
        "model_id": model_id.strip(),
        "metrics": None,
        "validation": None,
        "message": _UNAVAILABLE_TEXT,
        "reason": _UNAVAILABLE_REASON,
        "warnings": list(_WARNINGS),
    }


mcp = MCPServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "Admission prediction interface with no bundled model. Until an evaluated, calibrated model "
        "is installed, every prediction returns prediction_unavailable and probability null. Never "
        "present historical score lines as admission probabilities."
    ),
)

mcp.tool()(list_models)
mcp.tool()(get_model_info)
mcp.tool()(predict_admission)
mcp.tool()(compare_models)
mcp.tool()(explain_prediction)
mcp.tool()(get_validation_metrics)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
