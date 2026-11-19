"""postgrad-university-data MCP server (stdio).

Run directly::

    python extensions/postgraduate/mcp/university_data.py

Storage: one SQLite database at ``$POSTGRAD_DATA_DIR/university_data.sqlite3``
(default ``extensions/postgraduate/data``).  There is no seed data and no
network access: every query returns only what an operator previously stored
through :func:`upsert_verified_record`, which stays unregistered unless
``POSTGRAD_ALLOW_VERIFIED_WRITE=1``.

Rules enforced by this server:

* Every admission row must cite a public http(s) URL, a verbatim evidence
  span and a timezone-aware retrieval timestamp.
* The five score lines (national / school / college / program retest /
  admitted minimum) are distinct ``score_type`` values and are never merged
  or interpolated.
* A program is identified by school + college + program code + campus +
  study mode + exam year; missing values stay ``null``, never zero.
* With no stored data every read returns ``status="empty"`` and empty
  collections.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer

SERVER_NAME = "postgrad-university-data"
SERVER_VERSION = "0.1.0"
DATA_DIR_ENV = "POSTGRAD_DATA_DIR"
WRITE_ENV = "POSTGRAD_ALLOW_VERIFIED_WRITE"
DATABASE_FILENAME = "university_data.sqlite3"

SCORE_TYPE_LABELS: dict[str, str] = {
    "national_line": "国家线",
    "school_line": "校线",
    "college_line": "院线",
    "program_retest_line": "专业复试线",
    "admitted_min_score": "拟录取最低分",
}
SCORE_TYPE_NOTES: dict[str, str] = {
    "national_line": "全国统一最低要求（按学科门类/专业学位类别），不是该专业的录取线。",
    "school_line": "招生单位自划线或复试基本线，不等同于学院或专业线。",
    "college_line": "学院层面公布的复试线，不等同于具体专业方向线。",
    "program_retest_line": "具体专业（方向）的复试线，不等同于最终录取最低分。",
    "admitted_min_score": "拟录取考生的最低初试总分，不是个人录取概率。",
}
SCORE_SCOPES = ("total", "single_subject_100", "single_subject_150", "single_subject_300")
SCORE_SCOPE_LIMITS: dict[str, float] = {
    "total": 1000.0,
    "single_subject_100": 100.0,
    "single_subject_150": 150.0,
    "single_subject_300": 300.0,
}
DEGREE_TYPES = ("academic", "professional")
STUDY_MODES = ("full_time", "part_time")
MIN_EXAM_YEAR = 2000
MAX_EXAM_YEAR = 2100
MAX_TEXT = 500
MAX_URL = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    retrieved_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs (
    program_id TEXT PRIMARY KEY,
    school TEXT NOT NULL,
    school_code TEXT,
    college TEXT NOT NULL,
    college_code TEXT,
    program_code TEXT NOT NULL,
    program_name TEXT NOT NULL,
    campus TEXT NOT NULL DEFAULT '',
    degree_type TEXT NOT NULL CHECK (degree_type IN ('academic', 'professional')),
    study_mode TEXT NOT NULL CHECK (study_mode IN ('full_time', 'part_time')),
    exam_year INTEGER NOT NULL,
    exam_subjects TEXT NOT NULL DEFAULT '[]',
    enrollment INTEGER,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (school, college, program_code, campus, degree_type, study_mode, exam_year)
);
CREATE TABLE IF NOT EXISTS admission_records (
    record_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL REFERENCES programs(program_id),
    exam_year INTEGER NOT NULL,
    score_type TEXT NOT NULL CHECK (
        score_type IN ('national_line', 'school_line', 'college_line',
                       'program_retest_line', 'admitted_min_score')
    ),
    score REAL NOT NULL,
    score_scope TEXT NOT NULL CHECK (
        score_scope IN ('total', 'single_subject_100', 'single_subject_150',
                        'single_subject_300')
    ),
    admitted_count INTEGER,
    retest_count INTEGER,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    evidence_span TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (program_id, exam_year, score_type, score_scope)
);
CREATE INDEX IF NOT EXISTS idx_programs_lookup
    ON programs (school, program_code, exam_year);
CREATE INDEX IF NOT EXISTS idx_records_program
    ON admission_records (program_id, exam_year);
"""


def _utc_now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def data_dir() -> Path:
    """Return the storage directory, honouring ``POSTGRAD_DATA_DIR`` per call."""
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data"


def database_path() -> Path:
    return data_dir() / DATABASE_FILENAME


def verified_write_enabled() -> bool:
    return os.environ.get(WRITE_ENV, "").strip() == "1"


def _sha(prefix: str, *parts: str) -> str:
    return prefix + sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def _program_id(
    school: str,
    college: str,
    program_code: str,
    campus: str,
    degree_type: str,
    study_mode: str,
    exam_year: int,
) -> str:
    return _sha(
        "prog_",
        school.casefold(),
        college.casefold(),
        program_code.casefold(),
        campus.casefold(),
        degree_type,
        study_mode,
        str(exam_year),
    )


def _record_id(program_id: str, exam_year: int, score_type: str, score_scope: str) -> str:
    return _sha("rec_", program_id, str(exam_year), score_type, score_scope)


def _source_id(url: str) -> str:
    return _sha("src_", url)


def _clean(value: Any, label: str, errors: list[str], *, required: bool = True, max_len: int = MAX_TEXT) -> str | None:
    if value is None:
        if required:
            errors.append(f"{label} is required")
        return None
    if not isinstance(value, str):
        errors.append(f"{label} must be a string")
        return None
    text = " ".join(value.split())
    if not text:
        if required:
            errors.append(f"{label} must not be empty")
        return None
    if len(text) > max_len:
        errors.append(f"{label} exceeds {max_len} characters")
        return None
    return text


def _evidence(value: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{label} must be a string")
        return None
    text = value.strip()
    if len(text) < 4:
        errors.append(f"{label} must be a verbatim quote of at least 4 characters")
        return None
    if len(text) > 4000:
        errors.append(f"{label} exceeds 4000 characters")
        return None
    return text


def _integer(
    value: Any,
    label: str,
    errors: list[str],
    *,
    required: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        if required:
            errors.append(f"{label} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{label} must be an integer")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"{label} must be >= {minimum}")
        return None
    if maximum is not None and value > maximum:
        errors.append(f"{label} must be <= {maximum}")
        return None
    return value


def _number(
    value: Any,
    label: str,
    errors: list[str],
    *,
    required: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        if required:
            errors.append(f"{label} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} must be a number")
        return None
    number = float(value)
    if not math.isfinite(number):
        errors.append(f"{label} must be finite")
        return None
    if minimum is not None and number < minimum:
        errors.append(f"{label} must be >= {minimum}")
        return None
    if maximum is not None and number > maximum:
        errors.append(f"{label} must be <= {maximum}")
        return None
    return number


def _timestamp(value: Any, label: str, errors: list[str], *, required: bool, require_timezone: bool) -> str | None:
    if value is None:
        if required:
            errors.append(f"{label} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be an ISO-8601 date or datetime string")
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        errors.append(f"{label} must be ISO-8601, got {value!r}")
        return None
    if require_timezone and parsed.tzinfo is None:
        errors.append(f"{label} must include a timezone offset")
        return None
    # Keep an explicit date as a date; do not invent a time of day.
    if len(value.strip()) == 10 and parsed.hour == parsed.minute == parsed.second == 0:
        return value.strip()
    return parsed.isoformat()


def _url(value: Any, label: str, errors: list[str]) -> str | None:
    text = _clean(value, label, errors, max_len=MAX_URL)
    if text is None:
        return None
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        errors.append(f"{label} is not a valid URL")
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        errors.append(f"{label} must be an absolute http(s) URL")
        return None
    if parts.username or parts.password:
        errors.append(f"{label} must not embed credentials")
        return None
    return text


def _mapping(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _reject_unknown(payload: dict[str, Any], allowed: set[str], label: str, errors: list[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        errors.append(f"{label} has unsupported fields: {', '.join(unknown)}")


def _validate_source(source: Any, errors: list[str]) -> dict[str, Any] | None:
    payload = _mapping(source, "source", errors)
    _reject_unknown(payload, {"url", "title", "publisher", "published_at", "retrieved_at"}, "source", errors)
    url = _url(payload.get("url"), "source.url", errors)
    title = _clean(payload.get("title"), "source.title", errors, required=False) or ""
    publisher = _clean(payload.get("publisher"), "source.publisher", errors, required=False) or ""
    published_at = _timestamp(
        payload.get("published_at"), "source.published_at", errors, required=False, require_timezone=False
    )
    retrieved_at = _timestamp(
        payload.get("retrieved_at"), "source.retrieved_at", errors, required=True, require_timezone=True
    )
    if errors:
        return None
    if url is None or retrieved_at is None:
        return None
    return {
        "url": url,
        "title": title,
        "publisher": publisher,
        "published_at": published_at,
        "retrieved_at": retrieved_at,
        "source_id": _source_id(url),
    }


def _validate_program(program: Any, source_id: str, errors: list[str]) -> dict[str, Any] | None:
    payload = _mapping(program, "program", errors)
    _reject_unknown(
        payload,
        {
            "school",
            "school_code",
            "college",
            "college_code",
            "program_code",
            "program_name",
            "campus",
            "degree_type",
            "study_mode",
            "exam_year",
            "exam_subjects",
            "enrollment",
        },
        "program",
        errors,
    )
    school = _clean(payload.get("school"), "program.school", errors)
    school_code = _clean(payload.get("school_code"), "program.school_code", errors, required=False)
    college = _clean(payload.get("college"), "program.college", errors)
    college_code = _clean(payload.get("college_code"), "program.college_code", errors, required=False)
    program_code = _clean(payload.get("program_code"), "program.program_code", errors)
    program_name = _clean(payload.get("program_name"), "program.program_name", errors)
    campus = _clean(payload.get("campus"), "program.campus", errors, required=False) or ""
    degree_type = payload.get("degree_type")
    if degree_type not in DEGREE_TYPES:
        errors.append(f"program.degree_type must be one of {', '.join(DEGREE_TYPES)}")
        degree_type = None
    study_mode = payload.get("study_mode")
    if study_mode not in STUDY_MODES:
        errors.append(f"program.study_mode must be one of {', '.join(STUDY_MODES)}")
        study_mode = None
    exam_year = _integer(
        payload.get("exam_year"),
        "program.exam_year",
        errors,
        required=True,
        minimum=MIN_EXAM_YEAR,
        maximum=MAX_EXAM_YEAR,
    )
    enrollment = _integer(payload.get("enrollment"), "program.enrollment", errors, minimum=0)
    subjects = payload.get("exam_subjects")
    exam_subjects: list[dict[str, str]] = []
    if subjects is not None:
        if not isinstance(subjects, list) or not subjects:
            errors.append("program.exam_subjects must be a non-empty list when provided")
        else:
            for index, item in enumerate(subjects):
                entry = _mapping(item, f"program.exam_subjects[{index}]", errors)
                _reject_unknown(entry, {"code", "name"}, f"program.exam_subjects[{index}]", errors)
                code = _clean(entry.get("code"), f"program.exam_subjects[{index}].code", errors, required=False)
                name = _clean(entry.get("name"), f"program.exam_subjects[{index}].name", errors, required=False)
                if not code and not name:
                    errors.append(f"program.exam_subjects[{index}] needs a code or a name")
                    continue
                exam_subjects.append({"code": code or "", "name": name or ""})
    if errors:
        return None
    if None in (school, college, program_code, program_name, degree_type, study_mode, exam_year):
        return None
    assert school and college and program_code and program_name and exam_year is not None
    assert degree_type is not None and study_mode is not None
    return {
        "program_id": _program_id(school, college, program_code, campus, degree_type, study_mode, exam_year),
        "school": school,
        "school_code": school_code,
        "college": college,
        "college_code": college_code,
        "program_code": program_code,
        "program_name": program_name,
        "campus": campus,
        "degree_type": degree_type,
        "study_mode": study_mode,
        "exam_year": exam_year,
        "exam_subjects": exam_subjects,
        "enrollment": enrollment,
        "source_id": source_id,
    }


def _validate_record(record: Any, errors: list[str]) -> dict[str, Any] | None:
    payload = _mapping(record, "record", errors)
    _reject_unknown(
        payload,
        {"score_type", "score", "score_scope", "admitted_count", "retest_count", "evidence_span"},
        "record",
        errors,
    )
    score_type = payload.get("score_type")
    if score_type not in SCORE_TYPE_LABELS:
        errors.append(f"record.score_type must be one of {', '.join(SCORE_TYPE_LABELS)}")
        score_type = None
    score_scope = payload.get("score_scope", "total")
    if score_scope not in SCORE_SCOPES:
        errors.append(f"record.score_scope must be one of {', '.join(SCORE_SCOPES)}")
        score_scope = None
    score = _number(
        payload.get("score"),
        "record.score",
        errors,
        required=True,
        minimum=0.0,
        maximum=SCORE_SCOPE_LIMITS.get(score_scope or "", 1000.0),
    )
    if score == 0.0:
        errors.append("record.score must be greater than 0")
    admitted_count = _integer(payload.get("admitted_count"), "record.admitted_count", errors, minimum=0)
    retest_count = _integer(payload.get("retest_count"), "record.retest_count", errors, minimum=0)
    evidence_span = _evidence(payload.get("evidence_span"), "record.evidence_span", errors)
    if errors:
        return None
    if score_type is None or score_scope is None or score is None or evidence_span is None:
        return None
    return {
        "score_type": score_type,
        "score_scope": score_scope,
        "score": score,
        "admitted_count": admitted_count,
        "retest_count": retest_count,
        "evidence_span": evidence_span,
    }


def _read_connection() -> sqlite3.Connection | None:
    path = database_path()
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _write_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(admission_records)")}
    if "retrieved_at" not in columns:
        connection.execute(
            "ALTER TABLE admission_records ADD COLUMN retrieved_at TEXT NOT NULL DEFAULT ''"
        )
    return connection


def _has_tables(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('sources', 'programs', 'admission_records')"
    ).fetchone()
    return bool(row and row[0] == 3)


def _empty(message: str) -> dict[str, Any]:
    return {"status": "empty", "count": 0, "items": [], "message": message}


def _program_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["exam_subjects"] = json.loads(data.get("exam_subjects") or "[]")
    except json.JSONDecodeError:
        data["exam_subjects"] = []
    return data


def _record_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["score_type_label"] = SCORE_TYPE_LABELS.get(data["score_type"], data["score_type"])
    return data


def ping() -> dict[str, Any]:
    """Report server identity, storage location and write-gate state.

    Returns counts of stored rows; all counts are zero when no database exists.
    Safe to call at any time and never creates the database.
    """

    path = database_path()
    counts: dict[str, int] = {"programs": 0, "admission_records": 0, "sources": 0}
    database_exists = path.is_file()
    if database_exists:
        connection = _read_connection()
        if connection is not None:
            try:
                if _has_tables(connection):
                    for table in counts:
                        counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                connection.close()
    return {
        "status": "ok",
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "write_enabled": verified_write_enabled(),
        "data_dir": str(data_dir()),
        "database_path": str(path),
        "database_exists": database_exists,
        "counts": counts,
        "notes": [
            "No data is bundled or generated; empty counts mean nothing has been verified yet.",
            "Only upsert_verified_record writes, and only while POSTGRAD_ALLOW_VERIFIED_WRITE=1.",
        ],
    }


def search_programs(
    school: str | None = None,
    college: str | None = None,
    program_code: str | None = None,
    program_name: str | None = None,
    campus: str | None = None,
    exam_year: int | None = None,
    degree_type: str | None = None,
    study_mode: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search verified program records by exact filters.

    ``program_name`` matches a substring; every other filter matches exactly.
    Results keep school, college, program code, campus and exam year separate.
    Returns ``status="empty"`` with ``items=[]`` when nothing is stored or
    nothing matches; no placeholder rows are produced.
    """

    errors: list[str] = []
    exam_year = _integer(exam_year, "exam_year", errors, minimum=MIN_EXAM_YEAR, maximum=MAX_EXAM_YEAR)
    limit = _integer(limit, "limit", errors, minimum=1, maximum=100) or 20
    if degree_type is not None and degree_type not in DEGREE_TYPES:
        errors.append(f"degree_type must be one of {', '.join(DEGREE_TYPES)}")
    if study_mode is not None and study_mode not in STUDY_MODES:
        errors.append(f"study_mode must be one of {', '.join(STUDY_MODES)}")
    filters = {
        "school": _clean(school, "school", errors, required=False),
        "college": _clean(college, "college", errors, required=False),
        "program_code": _clean(program_code, "program_code", errors, required=False),
        "program_name": _clean(program_name, "program_name", errors, required=False),
        "campus": _clean(campus, "campus", errors, required=False),
        "exam_year": exam_year,
        "degree_type": degree_type,
        "study_mode": study_mode,
    }
    if errors:
        return {"status": "invalid_request", "count": 0, "items": [], "errors": errors}

    connection = _read_connection()
    if connection is None:
        return {**_empty("No verified university data is stored yet."), "filters": filters}
    try:
        if not _has_tables(connection):
            return {**_empty("The database exists but has no admission tables yet."), "filters": filters}
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("school", filters["school"]),
            ("college", filters["college"]),
            ("campus", filters["campus"]),
            ("degree_type", filters["degree_type"]),
            ("study_mode", filters["study_mode"]),
        ):
            if value is not None:
                clauses.append(f"p.{column} = ?")
                params.append(value)
        if filters["program_code"] is not None:
            clauses.append("lower(p.program_code) = lower(?)")
            params.append(filters["program_code"])
        if filters["program_name"] is not None:
            clauses.append("p.program_name LIKE ?")
            params.append(f"%{filters['program_name']}%")
        if filters["exam_year"] is not None:
            clauses.append("p.exam_year = ?")
            params.append(filters["exam_year"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM admission_records r WHERE r.program_id = p.program_id) AS record_count
            FROM programs p
            {where}
            ORDER BY p.exam_year DESC, p.school, p.college, p.program_code, p.campus
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    finally:
        connection.close()

    items = [_program_row(row) for row in rows]
    if not items:
        return {**_empty("No verified program matches these filters."), "filters": filters}
    return {"status": "ok", "count": len(items), "items": items, "filters": filters}


def get_admission_history(program_id: str) -> dict[str, Any]:
    """Return every stored score line for one program across exam years.

    Records stay split by ``score_type`` (national line, school line, college
    line, program retest line, admitted minimum) and ``score_scope``; values are
    never averaged, merged or inferred.  ``status="not_found"`` means the
    program is unknown, ``status="empty"`` means it exists with no scores.
    """

    if not isinstance(program_id, str) or not program_id.strip():
        return {"status": "invalid_request", "errors": ["program_id is required"]}
    program_id = program_id.strip()

    connection = _read_connection()
    if connection is None:
        return {
            "status": "not_found",
            "program_id": program_id,
            "program": None,
            "records": [],
            "message": "No verified university data is stored yet.",
        }
    try:
        if not _has_tables(connection):
            return {
                "status": "not_found",
                "program_id": program_id,
                "program": None,
                "records": [],
                "message": "The database exists but has no admission tables yet.",
            }
        anchor = connection.execute("SELECT * FROM programs WHERE program_id = ?", (program_id,)).fetchone()
        if anchor is None:
            return {
                "status": "not_found",
                "program_id": program_id,
                "program": None,
                "records": [],
                "message": "No program with this id is stored.",
            }
        siblings = connection.execute(
            """
            SELECT * FROM programs
            WHERE school = ? AND college = ? AND program_code = ?
              AND campus = ? AND degree_type = ? AND study_mode = ?
            ORDER BY exam_year DESC
            """,
            (
                anchor["school"],
                anchor["college"],
                anchor["program_code"],
                anchor["campus"],
                anchor["degree_type"],
                anchor["study_mode"],
            ),
        ).fetchall()
        rows = connection.execute(
            """
            SELECT r.*
            FROM admission_records r
            JOIN programs p ON p.program_id = r.program_id
            WHERE p.school = ? AND p.college = ? AND p.program_code = ?
              AND p.campus = ? AND p.degree_type = ? AND p.study_mode = ?
            ORDER BY r.exam_year DESC,
                     CASE r.score_type
                        WHEN 'national_line' THEN 1
                        WHEN 'school_line' THEN 2
                        WHEN 'college_line' THEN 3
                        WHEN 'program_retest_line' THEN 4
                        WHEN 'admitted_min_score' THEN 5
                        ELSE 6
                     END,
                     r.score_scope
            """,
            (
                anchor["school"],
                anchor["college"],
                anchor["program_code"],
                anchor["campus"],
                anchor["degree_type"],
                anchor["study_mode"],
            ),
        ).fetchall()
    finally:
        connection.close()

    program = _program_row(anchor)
    history_programs = [_program_row(row) for row in siblings]
    records = [_record_row(row) for row in rows]
    years = sorted({int(row["exam_year"]) for row in rows}, reverse=True)
    if not records:
        return {
            "status": "empty",
            "program": program,
            "programs_in_history": history_programs,
            "count": 0,
            "records": [],
            "years": years,
            "message": "This program is stored but has no admission scores yet.",
        }
    return {
        "status": "ok",
        "program": program,
        "programs_in_history": history_programs,
        "count": len(records),
        "records": records,
        "years": years,
        "score_type_labels": SCORE_TYPE_LABELS,
        "score_type_notes": SCORE_TYPE_NOTES,
        "message": "Score lines are reported per type and year; they are not admission probabilities.",
    }


def get_source_evidence(source_id: str) -> dict[str, Any]:
    """Return one stored source and every program/score record citing it.

    Each score record includes the verbatim ``evidence_span`` captured at write
    time.  Unknown ids return ``status="not_found"`` rather than an invented
    citation.
    """

    if not isinstance(source_id, str) or not source_id.strip():
        return {"status": "invalid_request", "errors": ["source_id is required"]}
    source_id = source_id.strip()

    connection = _read_connection()
    if connection is None:
        return {"status": "not_found", "source_id": source_id, "source": None, "programs": [], "records": []}
    try:
        if not _has_tables(connection):
            return {"status": "not_found", "source_id": source_id, "source": None, "programs": [], "records": []}
        source = connection.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if source is None:
            return {"status": "not_found", "source_id": source_id, "source": None, "programs": [], "records": []}
        programs = connection.execute(
            "SELECT * FROM programs WHERE source_id = ? ORDER BY exam_year DESC, school, program_code",
            (source_id,),
        ).fetchall()
        records = connection.execute(
            """
            SELECT r.*, p.school, p.college, p.program_code, p.campus
            FROM admission_records r
            JOIN programs p ON p.program_id = r.program_id
            WHERE r.source_id = ?
            ORDER BY r.exam_year DESC, p.school, p.program_code
            """,
            (source_id,),
        ).fetchall()
    finally:
        connection.close()

    return {
        "status": "ok",
        "source": dict(source),
        "programs": [_program_row(row) for row in programs],
        "records": [_record_row(row) for row in records],
        "count": len(records),
    }


def upsert_verified_record(source: dict[str, Any], program: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Store one evidence-backed admission record, atomically.

    Hidden unless ``POSTGRAD_ALLOW_VERIFIED_WRITE=1``; the check is repeated on
    every call.  All three objects are required:

    * ``source``: ``url`` (http/https), ``retrieved_at`` (ISO-8601 with
      timezone); optional ``title``, ``publisher``, ``published_at``.
    * ``program``: ``school``, ``college``, ``program_code``, ``program_name``,
      ``degree_type`` (academic|professional), ``study_mode``
      (full_time|part_time), ``exam_year``; optional ``campus``,
      ``school_code``, ``college_code``, ``exam_subjects`` (list of
      ``{code,name}``), ``enrollment``.
    * ``record``: ``score_type`` (national_line|school_line|college_line|
      program_retest_line|admitted_min_score), ``score``, ``evidence_span``
      (verbatim >=4 chars); optional ``score_scope`` (total|single_subject_100|
      single_subject_150|single_subject_300), ``admitted_count``,
      ``retest_count``.

    Unknown fields are rejected so callers cannot smuggle an unverifiable
    ``verified`` flag.  Missing counts stay ``null``.  Re-submitting the same
    program/score line updates it in place.
    """

    if not verified_write_enabled():
        return {
            "status": "disabled",
            "message": f"Verified writes are disabled; set {WRITE_ENV}=1 to expose this tool.",
        }

    errors: list[str] = []
    source_row = _validate_source(source, errors)
    source_id = source_row["source_id"] if source_row else ""
    program_row = _validate_program(program, source_id, errors)
    record_row = _validate_record(record, errors)
    if errors or source_row is None or program_row is None or record_row is None:
        return {"status": "rejected", "errors": errors or ["Payload validation failed"]}

    exam_year = program_row["exam_year"]
    record_id = _record_id(program_row["program_id"], exam_year, record_row["score_type"], record_row["score_scope"])
    now = _utc_now()
    try:
        connection = _write_connection()
    except (OSError, sqlite3.Error) as exc:
        return {"status": "storage_error", "errors": [f"Cannot open the database: {exc}"]}

    try:
        with connection:
            program_existed = (
                connection.execute(
                    "SELECT 1 FROM programs WHERE program_id = ?", (program_row["program_id"],)
                ).fetchone()
                is not None
            )
            record_existed = (
                connection.execute("SELECT 1 FROM admission_records WHERE record_id = ?", (record_id,)).fetchone()
                is not None
            )
            connection.execute(
                """
                INSERT INTO sources (source_id, url, title, publisher, published_at, retrieved_at, created_at)
                VALUES (:source_id, :url, :title, :publisher, :published_at, :retrieved_at, :created_at)
                ON CONFLICT(source_id) DO UPDATE SET
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE sources.title END,
                    publisher = CASE WHEN excluded.publisher <> '' THEN excluded.publisher ELSE sources.publisher END,
                    published_at = COALESCE(excluded.published_at, sources.published_at),
                    retrieved_at = excluded.retrieved_at
                """,
                {**source_row, "created_at": now},
            )
            connection.execute(
                """
                INSERT INTO programs (
                    program_id, school, school_code, college, college_code, program_code,
                    program_name, campus, degree_type, study_mode, exam_year,
                    exam_subjects, enrollment, source_id, created_at, updated_at
                ) VALUES (
                    :program_id, :school, :school_code, :college, :college_code, :program_code,
                    :program_name, :campus, :degree_type, :study_mode, :exam_year,
                    :exam_subjects, :enrollment, :source_id, :created_at, :updated_at
                )
                ON CONFLICT(program_id) DO UPDATE SET
                    school_code = COALESCE(excluded.school_code, programs.school_code),
                    college_code = COALESCE(excluded.college_code, programs.college_code),
                    program_name = excluded.program_name,
                    exam_subjects = excluded.exam_subjects,
                    enrollment = COALESCE(excluded.enrollment, programs.enrollment),
                    source_id = excluded.source_id,
                    updated_at = excluded.updated_at
                """,
                {
                    **program_row,
                    "exam_subjects": json.dumps(program_row["exam_subjects"], ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                """
                INSERT INTO admission_records (
                    record_id, program_id, exam_year, score_type, score, score_scope,
                    admitted_count, retest_count, source_id, evidence_span, retrieved_at,
                    created_at, updated_at
                ) VALUES (
                    :record_id, :program_id, :exam_year, :score_type, :score, :score_scope,
                    :admitted_count, :retest_count, :source_id, :evidence_span, :retrieved_at,
                    :created_at, :updated_at
                )
                ON CONFLICT(record_id) DO UPDATE SET
                    score = excluded.score,
                    admitted_count = COALESCE(excluded.admitted_count, admission_records.admitted_count),
                    retest_count = COALESCE(excluded.retest_count, admission_records.retest_count),
                    source_id = excluded.source_id,
                    evidence_span = excluded.evidence_span,
                    retrieved_at = excluded.retrieved_at,
                    updated_at = excluded.updated_at
                """,
                {
                    "record_id": record_id,
                    "program_id": program_row["program_id"],
                    "exam_year": exam_year,
                    **record_row,
                    "source_id": source_id,
                    "retrieved_at": source_row["retrieved_at"],
                    "created_at": now,
                    "updated_at": now,
                },
            )
    except sqlite3.Error as exc:
        return {"status": "storage_error", "errors": [f"Database write failed: {exc}"]}
    finally:
        connection.close()

    return {
        "status": "updated" if record_existed else "created",
        "program_created": not program_existed,
        "program_id": program_row["program_id"],
        "record_id": record_id,
        "source_id": source_id,
        "exam_year": exam_year,
        "score_type": record_row["score_type"],
        "score_scope": record_row["score_scope"],
        "score": record_row["score"],
        "evidence_span": record_row["evidence_span"],
        "retrieved_at": source_row["retrieved_at"],
        "database_path": str(database_path()),
    }


mcp = MCPServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "Verified Chinese postgraduate admission data only. Score lines are kept as five distinct "
        "types; absent data is reported as empty, never fabricated. Writes require an explicit "
        "source URL, verbatim evidence and retrieval timestamp."
    ),
)

mcp.tool()(ping)
mcp.tool()(search_programs)
mcp.tool()(get_admission_history)
mcp.tool()(get_source_evidence)
if verified_write_enabled():
    mcp.tool()(upsert_verified_record)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
