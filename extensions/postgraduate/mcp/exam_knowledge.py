"""Postgraduate exam knowledge MCP (stdio).

Self-contained SQLite store for exam syllabi, verified knowledge nodes,
verified prerequisite relations, verified questions and practice attempts.

Design rules:
- course identity is always (school, college, program, exam year, subject);
- agents and LLM output may only create *proposals*; proposals are not visible
  to retrieval until an administrator approves them;
- ``review_proposals`` is never registered unless ``POSTGRAD_ALLOW_REVIEW=1``;
- retrieval only reads verified tables;
- every question node and every relation keeps source evidence;
- past-paper questions require source evidence and an explicit copyright note;
- unknown/invalid input fails closed with a structured error.

Run with:
    python extensions/postgraduate/mcp/exam_knowledge.py

Environment:
    POSTGRAD_DATA_DIR       default: <repo>/extensions/postgraduate/data
    POSTGRAD_ALLOW_REVIEW   exactly "1" enables the review tool
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

SERVER_NAME = "postgrad-exam-knowledge"
SERVER_VERSION = "0.1.0"
DATA_DIR_ENV = "POSTGRAD_DATA_DIR"
REVIEW_ENV = "POSTGRAD_ALLOW_REVIEW"

NODE_TYPES = frozenset({"chapter", "knowledge_point"})
RELATION_TYPES = frozenset(
    {
        "CONTAINS",
        "PREREQUISITE_OF",
        "RELATED_TO",
        "ASSESSED_BY",
        "SUPPORTED_BY",
        "APPLIES_TO",
    }
)
QUESTION_TYPES = frozenset(
    {
        "single_choice",
        "multiple_choice",
        "true_false",
        "short_answer",
        "essay",
        "calculation",
    }
)
SOURCE_TYPES = frozenset({"original", "adapted", "past_paper"})
DIFFICULTIES = frozenset({"easy", "medium", "hard"})
MAX_QUERY_CHARS = 200
MAX_TOKENS = 8
MAX_LIMIT = 50


class UserError(Exception):
    """Expected, user-correctable failure. Never turns into an MCP crash."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str, **extra: object) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}, **extra}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _norm_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise UserError("invalid_course", f"{label} must be a string")
    text = value.strip()
    if not text:
        raise UserError("invalid_course", f"{label} is required")
    if len(text) > 128:
        raise UserError("invalid_course", f"{label} is too long")
    if "\x00" in text or "\r" in text or "\n" in text:
        raise UserError("invalid_course", f"{label} contains control characters")
    return text.upper()


def _course(
    school_id: str,
    college_id: str,
    program_code: str,
    exam_year: int,
    subject_code: str,
) -> dict:
    """Normalize the mandatory five-field course identity."""

    if isinstance(exam_year, bool) or not isinstance(exam_year, int):
        raise UserError("invalid_course", "exam_year must be an integer")
    if not 2000 <= exam_year <= 2100:
        raise UserError("invalid_course", "exam_year must be between 2000 and 2100")
    return {
        "school_id": _norm_id(school_id, "school_id"),
        "college_id": _norm_id(college_id, "college_id"),
        "program_code": _norm_id(program_code, "program_code"),
        "exam_year": exam_year,
        "subject_code": _norm_id(subject_code, "subject_code"),
    }


def _course_where(course: dict) -> tuple[str, list]:
    sql = (
        "school_id = ? AND college_id = ? AND program_code = ? "
        "AND exam_year = ? AND subject_code = ?"
    )
    return sql, [
        course["school_id"],
        course["college_id"],
        course["program_code"],
        course["exam_year"],
        course["subject_code"],
    ]


def _clean_text(value: object, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise UserError("invalid_input", f"{label} must be a string")
    text = value.strip()
    if len(text) < minimum:
        raise UserError("invalid_input", f"{label} is required")
    if len(text) > maximum:
        raise UserError("invalid_input", f"{label} is too long (max {maximum})")
    if "\x00" in text:
        raise UserError("invalid_input", f"{label} contains NUL")
    return text


def _parse_time(value: str, label: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise UserError("invalid_evidence", f"{label} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserError("invalid_evidence", f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class Evidence(BaseModel):
    """A concrete pointer into an official document or public source."""

    url: str | None = None
    document_ref: str | None = None
    title: str
    publisher: str = ""
    evidence_span: str
    retrieved_at: str


class ProposalDecision(BaseModel):
    proposal_id: str
    decision: Literal["approve", "reject"]
    note: str = ""


def _validated_evidence(evidence: Evidence | dict | None) -> dict:
    if evidence is None:
        raise UserError("evidence_required", "source evidence is required")
    if isinstance(evidence, dict):
        try:
            evidence = Evidence(**evidence)
        except Exception as exc:  # pydantic validation is reported as a user error
            raise UserError("invalid_evidence", "source evidence is invalid") from exc
    title = _clean_text(evidence.title, "evidence.title", 1, 500)
    span = _clean_text(evidence.evidence_span, "evidence.evidence_span", 1, 2000)
    publisher = evidence.publisher.strip() if evidence.publisher else ""
    if len(publisher) > 300:
        raise UserError("invalid_evidence", "evidence.publisher is too long")
    url = (evidence.url or "").strip() or None
    document_ref = (evidence.document_ref or "").strip() or None
    if url is None and document_ref is None:
        raise UserError(
            "invalid_evidence", "evidence requires an http(s) url or a document_ref"
        )
    if url is not None:
        if len(url) > 2000 or "\r" in url or "\n" in url or "\x00" in url:
            raise UserError("invalid_evidence", "evidence.url is invalid")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise UserError("invalid_evidence", "evidence.url must be http(s)")
        if parts.username or parts.password:
            raise UserError("invalid_evidence", "evidence.url must not contain credentials")
    if document_ref is not None and len(document_ref) > 1000:
        raise UserError("invalid_evidence", "evidence.document_ref is too long")
    retrieved = _parse_time(evidence.retrieved_at, "evidence.retrieved_at")
    if (retrieved - datetime.now(UTC)).total_seconds() > 86400:
        raise UserError("invalid_evidence", "retrieved_at cannot be in the future")
    return {
        "url": url,
        "document_ref": document_ref,
        "title": title,
        "publisher": publisher,
        "evidence_span": span,
        "retrieved_at": retrieved.date().isoformat(),
    }


def _hash_payload(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _data_dir() -> Path:
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "data"


def _allow_review() -> bool:
    return os.environ.get(REVIEW_ENV, "").strip() == "1"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    url TEXT,
    document_ref TEXT,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    evidence_span TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (url IS NOT NULL OR document_ref IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_identity ON sources (
    COALESCE(url, ''), COALESCE(document_ref, ''), title, evidence_span
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('node', 'relation')),
    school_id TEXT NOT NULL,
    college_id TEXT NOT NULL,
    program_code TEXT NOT NULL,
    exam_year INTEGER NOT NULL,
    subject_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(id),
    evidence_span TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    proposed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewer TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    result_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_proposal ON proposals (
    kind, school_id, college_id, program_code, exam_year, subject_code, payload_hash
) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_proposals_course ON proposals (
    school_id, college_id, program_code, exam_year, subject_code, status
);

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL,
    college_id TEXT NOT NULL,
    program_code TEXT NOT NULL,
    exam_year INTEGER NOT NULL,
    subject_code TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK (node_type IN ('chapter', 'knowledge_point')),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    parent_id TEXT REFERENCES knowledge_nodes(id),
    order_index INTEGER NOT NULL DEFAULT 0,
    syllabus_version TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL REFERENCES sources(id),
    evidence_span TEXT NOT NULL,
    proposal_id TEXT UNIQUE REFERENCES proposals(id),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_identity ON knowledge_nodes (
    school_id, college_id, program_code, exam_year, subject_code,
    COALESCE(parent_id, ''), name COLLATE NOCASE
);
CREATE INDEX IF NOT EXISTS idx_nodes_course ON knowledge_nodes (
    school_id, college_id, program_code, exam_year, subject_code, parent_id
);

CREATE TABLE IF NOT EXISTS knowledge_relations (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL,
    college_id TEXT NOT NULL,
    program_code TEXT NOT NULL,
    exam_year INTEGER NOT NULL,
    subject_code TEXT NOT NULL,
    src_node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    dst_node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'CONTAINS', 'PREREQUISITE_OF', 'RELATED_TO',
        'ASSESSED_BY', 'SUPPORTED_BY', 'APPLIES_TO'
    )),
    source_id TEXT NOT NULL REFERENCES sources(id),
    evidence_span TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    proposal_id TEXT UNIQUE REFERENCES proposals(id),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_relation_identity ON knowledge_relations (
    school_id, college_id, program_code, exam_year, subject_code,
    src_node_id, dst_node_id, relation_type
);
CREATE INDEX IF NOT EXISTS idx_relations_course ON knowledge_relations (
    school_id, college_id, program_code, exam_year, subject_code,
    relation_type, dst_node_id
);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL,
    college_id TEXT NOT NULL,
    program_code TEXT NOT NULL,
    exam_year INTEGER NOT NULL,
    subject_code TEXT NOT NULL,
    question_type TEXT NOT NULL,
    stem TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    answer TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL CHECK (source_type IN ('original', 'adapted', 'past_paper')),
    paper_year INTEGER,
    difficulty TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
    source_id TEXT REFERENCES sources(id),
    copyright_note TEXT NOT NULL DEFAULT '',
    verification_basis TEXT NOT NULL DEFAULT '',
    imported_by_admin INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (
        school_id, college_id, program_code, exam_year, subject_code, content_hash
    )
);
CREATE INDEX IF NOT EXISTS idx_questions_course ON questions (
    school_id, college_id, program_code, exam_year, subject_code,
    source_type, paper_year
);

CREATE TABLE IF NOT EXISTS question_nodes (
    question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES knowledge_nodes(id),
    PRIMARY KEY (question_id, node_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(id),
    course_key TEXT NOT NULL,
    user_ref TEXT NOT NULL,
    answer TEXT NOT NULL,
    is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    time_spent_seconds INTEGER,
    attempt_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_key ON attempts (
    question_id, user_ref, attempt_key
) WHERE attempt_key <> '';
CREATE INDEX IF NOT EXISTS idx_attempts_question_user ON attempts (question_id, user_ref);
"""


class KnowledgeStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._ensure_question_columns(conn)
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _ensure_question_columns(conn: sqlite3.Connection) -> None:
        """Additive migration for databases created before source gating."""

        columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        if "verification_basis" not in columns:
            conn.execute(
                "ALTER TABLE questions ADD COLUMN verification_basis TEXT NOT NULL DEFAULT ''"
            )
        if "imported_by_admin" not in columns:
            conn.execute(
                "ALTER TABLE questions ADD COLUMN imported_by_admin INTEGER NOT NULL DEFAULT 0"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _source_id(self, conn: sqlite3.Connection, evidence: dict) -> str:
        row = conn.execute(
            """SELECT id FROM sources
               WHERE COALESCE(url, '') = ? AND COALESCE(document_ref, '') = ?
                 AND title = ? AND evidence_span = ?""",
            (
                evidence["url"] or "",
                evidence["document_ref"] or "",
                evidence["title"],
                evidence["evidence_span"],
            ),
        ).fetchone()
        if row is not None:
            return row["id"]
        source_id = "src_" + uuid.uuid4().hex
        conn.execute(
            """INSERT INTO sources
               (id, url, document_ref, title, publisher, evidence_span, retrieved_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                evidence["url"],
                evidence["document_ref"],
                evidence["title"],
                evidence["publisher"],
                evidence["evidence_span"],
                evidence["retrieved_at"],
                _now(),
            ),
        )
        return source_id

    @staticmethod
    def _source_dict(row: sqlite3.Row) -> dict:
        return {
            "source_id": row["source_id"],
            "title": row["source_title"],
            "publisher": row["source_publisher"],
            "url": row["source_url"],
            "document_ref": row["source_document_ref"],
            "evidence_span": row["source_span"],
            "retrieved_at": row["source_retrieved_at"],
        }

    @staticmethod
    def _question_verification(source_type: str, imported_by_admin: object) -> dict:
        """Return explicit trust fields; only admin imports can be verified."""

        admin_imported = bool(imported_by_admin)
        if source_type == "past_paper" and admin_imported:
            return {
                "verified": True,
                "verified_as_past_paper": True,
                "verification_status": "admin_imported_past_paper",
                "verification_basis": "admin_supplied_offline_evidence",
                "authenticity_check": "not_online_authenticated; admin evidence import only",
            }
        if source_type == "adapted" and admin_imported:
            return {
                "verified": True,
                "verified_as_past_paper": False,
                "verification_status": "admin_imported_adapted",
                "verification_basis": "admin_supplied_offline_evidence",
                "authenticity_check": "not_online_authenticated; admin evidence import only",
            }
        if source_type in {"past_paper", "adapted"}:
            return {
                "verified": False,
                "verified_as_past_paper": False,
                "verification_status": "legacy_unverified_" + source_type,
                "verification_basis": "legacy_record_not_admin_verified",
                "authenticity_check": (
                    "not_online_authenticated; legacy record was not admin evidence imported"
                ),
            }
        return {
            "verified": False,
            "verified_as_past_paper": False,
            "verification_status": "original_unverified",
            "verification_basis": "original_not_a_past_paper",
            "authenticity_check": "not_applicable_original",
        }

    @staticmethod
    def _node_dict(row: sqlite3.Row) -> dict:
        return {
            "node_id": row["id"],
            "node_type": row["node_type"],
            "name": row["name"],
            "description": row["description"],
            "parent_id": row["parent_id"],
            "order_index": row["order_index"],
            "syllabus_version": row["syllabus_version"],
            "verified": True,
            "evidence_span": row["evidence_span"],
            "evidence": KnowledgeStore._source_dict(row),
        }

    def _node_row(self, conn: sqlite3.Connection, course: dict, node_id: str) -> sqlite3.Row | None:
        where, params = _course_where(course)
        return conn.execute(
            f"""SELECT n.*, s.id AS source_id, s.title AS source_title,
                       s.publisher AS source_publisher, s.url AS source_url,
                       s.document_ref AS source_document_ref,
                       s.evidence_span AS source_span, s.retrieved_at AS source_retrieved_at
                FROM knowledge_nodes n JOIN sources s ON s.id = n.source_id
                WHERE n.id = ? AND {where}""",
            [node_id, *params],
        ).fetchone()

    def _verified_node_ids(
        self, conn: sqlite3.Connection, course: dict, node_ids: list[str]
    ) -> list[str]:
        if not node_ids:
            raise UserError("node_required", "at least one verified knowledge node is required")
        if len(node_ids) > 20:
            raise UserError("invalid_input", "at most 20 knowledge nodes are allowed")
        unique = list(dict.fromkeys(node_ids))
        where, params = _course_where(course)
        placeholders = ",".join("?" for _ in unique)
        rows = conn.execute(
            f"SELECT id FROM knowledge_nodes WHERE {where} AND id IN ({placeholders})",
            [*params, *unique],
        ).fetchall()
        found = {row["id"] for row in rows}
        missing = [node_id for node_id in unique if node_id not in found]
        if missing:
            raise UserError(
                "unknown_node",
                f"nodes not verified in this course: {', '.join(missing[:5])}",
            )
        return unique

    def _would_cycle(
        self,
        conn: sqlite3.Connection,
        course: dict,
        src_node_id: str,
        dst_node_id: str,
    ) -> bool:
        if src_node_id == dst_node_id:
            return True
        where, params = _course_where(course)
        # Adding src -> dst creates a cycle iff dst already reaches src by
        # following existing prerequisite edges in the forward direction.
        frontier = {dst_node_id}
        visited: set[str] = set()
        while frontier and len(visited) < 5000:
            placeholders = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"""SELECT dst_node_id FROM knowledge_relations
                    WHERE {where} AND relation_type = 'PREREQUISITE_OF'
                      AND src_node_id IN ({placeholders})""",
                [*params, *sorted(frontier)],
            ).fetchall()
            next_frontier: set[str] = set()
            for row in rows:
                node_id = row["dst_node_id"]
                if node_id == src_node_id:
                    return True
                if node_id not in visited:
                    visited.add(node_id)
                    next_frontier.add(node_id)
            frontier = next_frontier
        return False

    # ------------------------------------------------------------------ tools
    def ping(self) -> dict:
        """Return server health and whether the admin review tool is exposed."""

        with self.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return {
            "ok": True,
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "status": "ready",
            "storage": "sqlite",
            "review_tool_enabled": self.allow_review,
            "tool_count": 10 if self.allow_review else 9,
        }

    def get_syllabus(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        syllabus_version: str = "",
        include_points: bool = True,
    ) -> dict:
        """Read the verified chapter/knowledge-point tree for one exact course.

        The five course fields are mandatory; unverified proposals are never
        returned. Empty output means no verified syllabus is stored yet.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            if syllabus_version:
                syllabus_version = _clean_text(
                    syllabus_version, "syllabus_version", 1, 100
                )
            where, params = _course_where(course)
            sql = f"""SELECT n.*, s.id AS source_id, s.title AS source_title,
                             s.publisher AS source_publisher, s.url AS source_url,
                             s.document_ref AS source_document_ref,
                             s.evidence_span AS source_span,
                             s.retrieved_at AS source_retrieved_at
                      FROM knowledge_nodes n JOIN sources s ON s.id = n.source_id
                      WHERE {where}"""
            if syllabus_version:
                sql += " AND n.syllabus_version = ?"
                params = [*params, syllabus_version]
            sql += " ORDER BY n.order_index, n.name"
            with self.read() as conn:
                rows = conn.execute(sql, params).fetchall()
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")

        nodes = [self._node_dict(row) for row in rows]
        by_id = {node["node_id"]: node for node in nodes}
        children: dict[str | None, list[dict]] = {}
        for node in nodes:
            parent = node["parent_id"] if node["parent_id"] in by_id else None
            children.setdefault(parent, []).append(node)

        def build(node: dict, seen: set[str]) -> dict:
            if node["node_id"] in seen:
                return {**node, "children": []}
            seen = {*seen, node["node_id"]}
            return {
                **node,
                "children": [build(child, seen) for child in children.get(node["node_id"], [])],
            }

        tree = [build(node, set()) for node in children.get(None, [])]
        if not include_points:
            def strip(node: dict) -> dict:
                return {
                    **{key: value for key, value in node.items() if key != "children"},
                    "child_count": len(node["children"]),
                }
            tree = [strip(node) for node in tree]
        return {
            "ok": True,
            "course": course,
            "available": bool(nodes),
            "verified_only": True,
            "syllabus_version": syllabus_version or None,
            "chapter_count": sum(1 for node in nodes if node["node_type"] == "chapter"),
            "point_count": sum(
                1 for node in nodes if node["node_type"] == "knowledge_point"
            ),
            "tree": tree,
        }

    def _subtree_ids(
        self, conn: sqlite3.Connection, course: dict, chapter_id: str
    ) -> list[str]:
        where, params = _course_where(course)
        root = conn.execute(
            f"SELECT id FROM knowledge_nodes WHERE {where} AND id = ?",
            [*params, chapter_id],
        ).fetchone()
        if root is None:
            raise UserError("unknown_node", "chapter is not verified in this course")
        found = [chapter_id]
        frontier = [chapter_id]
        while frontier and len(found) < 2000:
            placeholders = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"""SELECT id FROM knowledge_nodes
                    WHERE {where} AND parent_id IN ({placeholders})""",
                [*params, *frontier],
            ).fetchall()
            next_frontier = [row["id"] for row in rows if row["id"] not in found]
            found.extend(next_frontier)
            frontier = next_frontier
        return found

    @staticmethod
    def _like_tokens(query: str) -> list[str]:
        if len(query) > MAX_QUERY_CHARS:
            raise UserError("invalid_query", f"query exceeds {MAX_QUERY_CHARS} characters")
        tokens = [token for token in re.split(r"[\s,，、;；]+", query.strip()) if token]
        if len(tokens) > MAX_TOKENS:
            raise UserError("invalid_query", f"query has more than {MAX_TOKENS} terms")
        return tokens

    @staticmethod
    def _like(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def search_knowledge(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        query: str = "",
        chapter_id: str = "",
        limit: int = 10,
    ) -> dict:
        """Search verified knowledge nodes for one exact course.

        Optional ``chapter_id`` restricts results to that chapter subtree.
        Unverified proposals and rejected items are never visible.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            if not isinstance(query, str):
                raise UserError("invalid_query", "query must be a string")
            tokens = self._like_tokens(query)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise UserError("invalid_input", "limit must be an integer")
            limit = max(1, min(MAX_LIMIT, limit))
            chapter_id = (chapter_id or "").strip()
            clauses = [
                "n.school_id = ? AND n.college_id = ? AND n.program_code = ? "
                "AND n.exam_year = ? AND n.subject_code = ?"
            ]
            query_params: list = [
                course["school_id"],
                course["college_id"],
                course["program_code"],
                course["exam_year"],
                course["subject_code"],
            ]
            with self.read() as conn:
                if chapter_id:
                    ids = self._subtree_ids(conn, course, chapter_id)
                    placeholders = ",".join("?" for _ in ids)
                    clauses.append(f"n.id IN ({placeholders})")
                    query_params.extend(ids)
                for token in tokens:
                    like = self._like(token)
                    clauses.append(
                        "(n.name LIKE ? ESCAPE '\\' OR n.description LIKE ? ESCAPE '\\' "
                        "OR s.title LIKE ? ESCAPE '\\')"
                    )
                    query_params.extend([like, like, like])
                sql = f"""SELECT n.*, s.id AS source_id, s.title AS source_title,
                                 s.publisher AS source_publisher, s.url AS source_url,
                                 s.document_ref AS source_document_ref,
                                 s.evidence_span AS source_span,
                                 s.retrieved_at AS source_retrieved_at
                          FROM knowledge_nodes n JOIN sources s ON s.id = n.source_id
                          WHERE {' AND '.join(clauses)}
                          ORDER BY n.order_index, n.name
                          LIMIT ?"""
                rows = conn.execute(sql, [*query_params, limit]).fetchall()
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")

        return {
            "ok": True,
            "course": course,
            "query": query,
            "verified_only": True,
            "count": len(rows),
            "results": [self._node_dict(row) for row in rows],
        }

    def propose_node(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        node_type: str,
        name: str,
        evidence: Evidence | None = None,
        description: str = "",
        parent_id: str = "",
        order_index: int = 0,
        syllabus_version: str = "",
        proposed_by: str = "",
    ) -> dict:
        """Create a pending knowledge-node proposal. Only an admin can approve it.

        ``evidence`` must identify a real document/page. Proposals are never
        returned by retrieval until approved.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            node_type = (node_type or "").strip()
            if node_type not in NODE_TYPES:
                raise UserError("invalid_node_type", "node_type must be chapter or knowledge_point")
            name = _clean_text(name, "name", 1, 300)
            if "\r" in name or "\n" in name:
                raise UserError("invalid_input", "name must be one line")
            description = (description or "").strip()
            if len(description) > 5000:
                raise UserError("invalid_input", "description is too long")
            parent_id = (parent_id or "").strip()
            syllabus_version = (syllabus_version or "").strip()
            if len(syllabus_version) > 100:
                raise UserError("invalid_input", "syllabus_version is too long")
            proposed_by = (proposed_by or "").strip()
            if len(proposed_by) > 200:
                raise UserError("invalid_input", "proposed_by is too long")
            if isinstance(order_index, bool) or not isinstance(order_index, int):
                raise UserError("invalid_input", "order_index must be an integer")
            order_index = max(-10000, min(10000, order_index))
            clean_evidence = _validated_evidence(evidence)
            payload = {
                "node_type": node_type,
                "name": name,
                "description": description,
                "parent_id": parent_id or None,
                "order_index": order_index,
                "syllabus_version": syllabus_version,
            }
            payload_hash = _hash_payload(payload)
            where, params = _course_where(course)
            with self.tx() as conn:
                if parent_id:
                    parent = conn.execute(
                        f"SELECT id FROM knowledge_nodes WHERE {where} AND id = ?",
                        [*params, parent_id],
                    ).fetchone()
                    if parent is None:
                        raise UserError(
                            "unknown_parent",
                            "parent must be a verified node in the same course",
                        )
                duplicate = conn.execute(
                    f"""SELECT id FROM knowledge_nodes
                        WHERE {where} AND COALESCE(parent_id, '') = COALESCE(?, '')
                          AND name = ? COLLATE NOCASE""",
                    [*params, parent_id or None, name],
                ).fetchone()
                if duplicate is not None:
                    raise UserError(
                        "already_verified",
                        f"a verified node already exists: {duplicate['id']}",
                    )
                pending = conn.execute(
                    f"""SELECT id FROM proposals
                        WHERE {where} AND kind = 'node' AND status = 'pending'
                          AND payload_hash = ?""",
                    [*params, payload_hash],
                ).fetchone()
                if pending is not None:
                    return {
                        "ok": True,
                        "duplicate": True,
                        "proposal_id": pending["id"],
                        "status": "pending",
                        "review_required": True,
                    }
                source_id = self._source_id(conn, clean_evidence)
                proposal_id = "prop_" + uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO proposals
                       (id, kind, school_id, college_id, program_code, exam_year,
                        subject_code, payload_json, payload_hash, source_id,
                        evidence_span, status, proposed_by, created_at)
                       VALUES (?, 'node', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    [
                        proposal_id,
                        course["school_id"],
                        course["college_id"],
                        course["program_code"],
                        course["exam_year"],
                        course["subject_code"],
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        payload_hash,
                        source_id,
                        clean_evidence["evidence_span"],
                        proposed_by,
                        _now(),
                    ],
                )
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")

        return {
            "ok": True,
            "duplicate": False,
            "proposal_id": proposal_id,
            "status": "pending",
            "review_required": True,
            "message": "Proposal stored; it is invisible to retrieval until approved.",
        }

    def propose_relation(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        src_node_id: str,
        dst_node_id: str,
        relation_type: str,
        evidence: Evidence | None = None,
        confidence: float = 0.8,
        proposed_by: str = "",
    ) -> dict:
        """Create a pending relation proposal between two verified nodes.

        ``PREREQUISITE_OF`` means ``src`` is a prerequisite of ``dst``.
        Cycles and self-relations are rejected. Approval requires admin review.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            relation_type = (relation_type or "").strip().upper()
            if relation_type not in RELATION_TYPES:
                raise UserError("invalid_relation_type", "unknown relation_type")
            src_node_id = _clean_text(src_node_id, "src_node_id", 1, 200)
            dst_node_id = _clean_text(dst_node_id, "dst_node_id", 1, 200)
            if src_node_id == dst_node_id:
                raise UserError("invalid_relation", "self-relations are not allowed")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise UserError("invalid_input", "confidence must be a number")
            confidence = float(confidence)
            if not 0.0 <= confidence <= 1.0:
                raise UserError("invalid_input", "confidence must be between 0 and 1")
            proposed_by = (proposed_by or "").strip()
            if len(proposed_by) > 200:
                raise UserError("invalid_input", "proposed_by is too long")
            clean_evidence = _validated_evidence(evidence)
            payload = {
                "src_node_id": src_node_id,
                "dst_node_id": dst_node_id,
                "relation_type": relation_type,
                "confidence": round(confidence, 4),
            }
            payload_hash = _hash_payload(payload)
            where, params = _course_where(course)
            with self.tx() as conn:
                endpoints = conn.execute(
                    f"SELECT id FROM knowledge_nodes WHERE {where} AND id IN (?, ?)",
                    [*params, src_node_id, dst_node_id],
                ).fetchall()
                if len(endpoints) != 2:
                    raise UserError(
                        "unknown_node",
                        "both relation endpoints must be verified nodes in this course",
                    )
                if relation_type == "PREREQUISITE_OF" and self._would_cycle(
                    conn, course, src_node_id, dst_node_id
                ):
                    raise UserError(
                        "prerequisite_cycle",
                        "relation would create a prerequisite cycle",
                    )
                duplicate = conn.execute(
                    f"""SELECT id FROM knowledge_relations
                        WHERE {where} AND src_node_id = ? AND dst_node_id = ?
                          AND relation_type = ?""",
                    [*params, src_node_id, dst_node_id, relation_type],
                ).fetchone()
                if duplicate is not None:
                    raise UserError(
                        "already_verified",
                        f"a verified relation already exists: {duplicate['id']}",
                    )
                pending = conn.execute(
                    f"""SELECT id FROM proposals
                        WHERE {where} AND kind = 'relation' AND status = 'pending'
                          AND payload_hash = ?""",
                    [*params, payload_hash],
                ).fetchone()
                if pending is not None:
                    return {
                        "ok": True,
                        "duplicate": True,
                        "proposal_id": pending["id"],
                        "status": "pending",
                        "review_required": True,
                    }
                source_id = self._source_id(conn, clean_evidence)
                proposal_id = "prop_" + uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO proposals
                       (id, kind, school_id, college_id, program_code, exam_year,
                        subject_code, payload_json, payload_hash, source_id,
                        evidence_span, status, proposed_by, created_at)
                       VALUES (?, 'relation', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    [
                        proposal_id,
                        course["school_id"],
                        course["college_id"],
                        course["program_code"],
                        course["exam_year"],
                        course["subject_code"],
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        payload_hash,
                        source_id,
                        clean_evidence["evidence_span"],
                        proposed_by,
                        _now(),
                    ],
                )
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")

        return {
            "ok": True,
            "duplicate": False,
            "proposal_id": proposal_id,
            "status": "pending",
            "review_required": True,
            "message": "Proposal stored; it is invisible to retrieval until approved.",
        }

    def review_proposals(self, reviewer: str, decisions: list[ProposalDecision]) -> dict:
        """Admin-only, all-or-nothing approval/rejection of pending proposals.

        This tool exists only when ``POSTGRAD_ALLOW_REVIEW=1``. Any invalid
        decision rolls back the entire batch. Approved nodes/relations become
        visible to retrieval; rejected proposals remain invisible.
        """

        if not self.allow_review:
            return _error("forbidden", "review is disabled on this server")
        try:
            reviewer = _clean_text(reviewer, "reviewer", 2, 200)
            if not isinstance(decisions, list) or not 1 <= len(decisions) <= 50:
                raise UserError("invalid_input", "decisions must contain 1-50 items")
            decisions = [
                ProposalDecision(**item) if isinstance(item, dict) else item
                for item in decisions
            ]
            ids = [str(item.proposal_id).strip() for item in decisions]
            if any(not proposal_id for proposal_id in ids):
                raise UserError("invalid_input", "proposal_id is required")
            if len(set(ids)) != len(ids):
                raise UserError("invalid_input", "duplicate proposal_id in decisions")
            results: list[dict] = []
            with self.tx() as conn:
                for item in decisions:
                    proposal_id = str(item.proposal_id).strip()
                    decision = str(item.decision).strip()
                    note = (item.note or "").strip()[:1000]
                    row = conn.execute(
                        "SELECT * FROM proposals WHERE id = ?", [proposal_id]
                    ).fetchone()
                    if row is None:
                        raise UserError("not_found", f"proposal not found: {proposal_id}")
                    if row["status"] != "pending":
                        raise UserError(
                            "not_pending", f"proposal is already {row['status']}: {proposal_id}"
                        )
                    if decision == "reject":
                        conn.execute(
                            """UPDATE proposals
                               SET status = 'rejected', reviewed_at = ?, reviewer = ?,
                                   review_note = ?
                               WHERE id = ?""",
                            [_now(), reviewer, note, proposal_id],
                        )
                        results.append(
                            {"proposal_id": proposal_id, "status": "rejected"}
                        )
                        continue
                    if decision != "approve":
                        raise UserError("invalid_decision", "decision must be approve or reject")
                    payload = json.loads(row["payload_json"])
                    course = {
                        "school_id": row["school_id"],
                        "college_id": row["college_id"],
                        "program_code": row["program_code"],
                        "exam_year": row["exam_year"],
                        "subject_code": row["subject_code"],
                    }
                    if row["kind"] == "node":
                        parent_id = payload.get("parent_id")
                        if parent_id:
                            where, params = _course_where(course)
                            parent = conn.execute(
                                f"SELECT id FROM knowledge_nodes WHERE {where} AND id = ?",
                                [*params, parent_id],
                            ).fetchone()
                            if parent is None:
                                raise UserError(
                                    "unknown_parent",
                                    f"parent is not verified: {parent_id}",
                                )
                        where, params = _course_where(course)
                        duplicate = conn.execute(
                            f"""SELECT id FROM knowledge_nodes
                                WHERE {where}
                                  AND COALESCE(parent_id, '') = COALESCE(?, '')
                                  AND name = ? COLLATE NOCASE""",
                            [*params, parent_id, payload["name"]],
                        ).fetchone()
                        if duplicate is not None:
                            raise UserError(
                                "already_verified",
                                f"node already verified: {duplicate['id']}",
                            )
                        result_id = "node_" + uuid.uuid4().hex
                        conn.execute(
                            """INSERT INTO knowledge_nodes
                               (id, school_id, college_id, program_code, exam_year,
                                subject_code, node_type, name, description, parent_id,
                                order_index, syllabus_version, source_id, evidence_span,
                                proposal_id, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            [
                                result_id,
                                course["school_id"],
                                course["college_id"],
                                course["program_code"],
                                course["exam_year"],
                                course["subject_code"],
                                payload["node_type"],
                                payload["name"],
                                payload.get("description", ""),
                                parent_id,
                                payload.get("order_index", 0),
                                payload.get("syllabus_version", ""),
                                row["source_id"],
                                row["evidence_span"],
                                proposal_id,
                                _now(),
                            ],
                        )
                    elif row["kind"] == "relation":
                        src = payload["src_node_id"]
                        dst = payload["dst_node_id"]
                        relation_type = payload["relation_type"]
                        if relation_type not in RELATION_TYPES:
                            raise UserError("invalid_relation_type", "unknown relation_type")
                        where, params = _course_where(course)
                        endpoints = conn.execute(
                            f"SELECT id FROM knowledge_nodes WHERE {where} AND id IN (?, ?)",
                            [*params, src, dst],
                        ).fetchall()
                        if len(endpoints) != 2:
                            raise UserError(
                                "unknown_node",
                                "both relation endpoints must be verified",
                            )
                        if relation_type == "PREREQUISITE_OF" and self._would_cycle(
                            conn, course, src, dst
                        ):
                            raise UserError(
                                "prerequisite_cycle",
                                "relation would create a prerequisite cycle",
                            )
                        duplicate = conn.execute(
                            f"""SELECT id FROM knowledge_relations
                                WHERE {where} AND src_node_id = ? AND dst_node_id = ?
                                  AND relation_type = ?""",
                            [*params, src, dst, relation_type],
                        ).fetchone()
                        if duplicate is not None:
                            raise UserError(
                                "already_verified",
                                f"relation already verified: {duplicate['id']}",
                            )
                        result_id = "rel_" + uuid.uuid4().hex
                        conn.execute(
                            """INSERT INTO knowledge_relations
                               (id, school_id, college_id, program_code, exam_year,
                                subject_code, src_node_id, dst_node_id, relation_type,
                                source_id, evidence_span, confidence, proposal_id,
                                created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            [
                                result_id,
                                course["school_id"],
                                course["college_id"],
                                course["program_code"],
                                course["exam_year"],
                                course["subject_code"],
                                src,
                                dst,
                                relation_type,
                                row["source_id"],
                                row["evidence_span"],
                                float(payload.get("confidence", 0.8)),
                                proposal_id,
                                _now(),
                            ],
                        )
                    else:
                        raise UserError("invalid_proposal", "unknown proposal kind")
                    conn.execute(
                        """UPDATE proposals
                           SET status = 'approved', reviewed_at = ?, reviewer = ?,
                               review_note = ?, result_id = ?
                           WHERE id = ?""",
                        [_now(), reviewer, note, result_id, proposal_id],
                    )
                    results.append(
                        {
                            "proposal_id": proposal_id,
                            "status": "approved",
                            "result_id": result_id,
                        }
                    )
        except UserError as exc:
            return _error(exc.code, exc.message)
        except (sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
            return _error(
                "review_failed",
                "review batch failed validation and was rolled back",
            )
        return {"ok": True, "reviewer": reviewer, "results": results}

    def get_prerequisites(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        node_id: str,
        transitive: bool = True,
        max_depth: int = 3,
    ) -> dict:
        """Return verified prerequisite nodes for a knowledge point.

        ``src --PREREQUISITE_OF--> dst`` means src is learned before dst.
        Cycles are ignored during traversal as a second line of defense.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            node_id = _clean_text(node_id, "node_id", 1, 200)
            if isinstance(max_depth, bool) or not isinstance(max_depth, int):
                raise UserError("invalid_input", "max_depth must be an integer")
            max_depth = max(1, min(10, max_depth))
            where, params = _course_where(course)
            with self.read() as conn:
                if self._node_row(conn, course, node_id) is None:
                    raise UserError("unknown_node", "node is not verified in this course")
                depth_of: dict[str, int] = {}
                via: dict[str, dict] = {}
                frontier = {node_id}
                depth = 0
                while frontier and depth < (max_depth if transitive else 1):
                    depth += 1
                    placeholders = ",".join("?" for _ in frontier)
                    rows = conn.execute(
                        f"""SELECT r.*, s.title AS source_title, s.publisher AS source_publisher,
                                   s.url AS source_url, s.document_ref AS source_document_ref,
                                   s.evidence_span AS source_span,
                                   s.retrieved_at AS source_retrieved_at
                            FROM knowledge_relations r
                            JOIN sources s ON s.id = r.source_id
                            WHERE r.school_id = ? AND r.college_id = ?
                              AND r.program_code = ? AND r.exam_year = ?
                              AND r.subject_code = ?
                              AND r.relation_type = 'PREREQUISITE_OF'
                              AND r.dst_node_id IN ({placeholders})""",
                        [*params, *sorted(frontier)],
                    ).fetchall()
                    next_frontier: set[str] = set()
                    for row in rows:
                        src = row["src_node_id"]
                        if src == node_id:
                            continue
                        if src not in depth_of:
                            depth_of[src] = depth
                            via[src] = {
                                "relation_id": row["id"],
                                "relation_type": row["relation_type"],
                                "confidence": row["confidence"],
                                "evidence_span": row["evidence_span"],
                                "evidence": self._source_dict(row),
                            }
                        next_frontier.add(src)
                    frontier = next_frontier
                prerequisites: list[dict] = []
                for prereq_id, prereq_depth in sorted(
                    depth_of.items(), key=lambda item: (item[1], item[0])
                ):
                    row = self._node_row(conn, course, prereq_id)
                    if row is not None:
                        prerequisites.append(
                            {
                                "node": self._node_dict(row),
                                "depth": prereq_depth,
                                "via": via.get(prereq_id),
                            }
                        )
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")
        return {
            "ok": True,
            "course": course,
            "node_id": node_id,
            "verified_only": True,
            "count": len(prerequisites),
            "prerequisites": prerequisites,
        }

    def save_verified_question(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        question_type: str,
        stem: str,
        answer: str,
        node_ids: list[str],
        options: list[str] | None = None,
        explanation: str = "",
        source_type: str = "original",
        source: Evidence | None = None,
        paper_year: int | None = None,
        difficulty: str = "medium",
        copyright_note: str = "",
        created_by: str = "",
    ) -> dict:
        """Store a question with explicit originality/copyright/trust labels.

        Ordinary servers may store only ``original`` practice questions. The
        source types ``past_paper`` and ``adapted`` are reserved for an
        administrator evidence-import process and require
        ``POSTGRAD_ALLOW_REVIEW=1`` (``self.allow_review``), non-empty copyright
        notes and source evidence. Even then, "verified" means admin-supplied
        evidence was recorded, not that authenticity was checked online.
        Questions must link to at least one verified node in this course.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            question_type = (question_type or "").strip()
            if question_type not in QUESTION_TYPES:
                raise UserError("invalid_question_type", "unknown question_type")
            stem = _clean_text(stem, "stem", 5, 5000)
            answer = _clean_text(answer, "answer", 1, 5000)
            explanation = (explanation or "").strip()
            if len(explanation) > 10000:
                raise UserError("invalid_input", "explanation is too long")
            source_type = (source_type or "").strip()
            if source_type not in SOURCE_TYPES:
                raise UserError(
                    "invalid_source_type",
                    "source_type must be original, adapted or past_paper",
                )
            admin_import = source_type in {"past_paper", "adapted"}
            if admin_import and not self.allow_review:
                raise UserError(
                    "admin_required",
                    "past_paper/adapted questions require administrator evidence "
                    "import (POSTGRAD_ALLOW_REVIEW=1); ordinary agents may store "
                    "original practice questions only",
                )
            difficulty = (difficulty or "").strip()
            if difficulty not in DIFFICULTIES:
                raise UserError("invalid_difficulty", "difficulty must be easy, medium or hard")
            copyright_note = (copyright_note or "").strip()
            if len(copyright_note) > 1000:
                raise UserError("invalid_input", "copyright_note is too long")

            created_by = (created_by or "").strip()
            if len(created_by) > 200:
                raise UserError("invalid_input", "created_by is too long")
            if options is None:
                options = []
            if not isinstance(options, list) or len(options) > 10:
                raise UserError("invalid_input", "options must be a list of at most 10 items")
            cleaned_options = [
                _clean_text(option, "option", 1, 2000) for option in options
            ]
            if len(set(cleaned_options)) != len(cleaned_options):
                raise UserError("invalid_input", "options contain duplicates")
            if question_type in {"single_choice", "multiple_choice"} and len(
                cleaned_options
            ) < 2:
                raise UserError("invalid_input", "choice questions need at least two options")
            if paper_year is not None:
                if isinstance(paper_year, bool) or not isinstance(paper_year, int):
                    raise UserError("invalid_input", "paper_year must be an integer")
                current_year = datetime.now(UTC).year
                if not 1980 <= paper_year <= current_year + 1:
                    raise UserError("invalid_input", "paper_year is outside the allowed range")
            clean_evidence: dict | None = None
            if isinstance(source, dict):
                try:
                    source = Evidence(**source)
                except Exception as exc:
                    raise UserError("invalid_evidence", "source evidence is invalid") from exc
            if source is not None:
                clean_evidence = _validated_evidence(source)
            if source_type == "past_paper":
                if paper_year is None:
                    raise UserError(
                        "paper_year_required", "past_paper requires the real paper_year"
                    )
                if clean_evidence is None:
                    raise UserError(
                        "evidence_required",
                        "past_paper requires source evidence; fabricated papers are refused",
                    )
                if not copyright_note:
                    raise UserError(
                        "copyright_note_required",
                        "past_paper admin imports require a non-empty copyright note",
                    )
                label_note = copyright_note
                verification_basis = "admin_imported_past_paper"
            elif source_type == "adapted":
                if clean_evidence is None:
                    raise UserError(
                        "evidence_required", "adapted questions require source evidence"
                    )
                if not copyright_note:
                    raise UserError(
                        "copyright_note_required",
                        "adapted admin imports require a non-empty copyright note",
                    )
                label_note = copyright_note
                verification_basis = "admin_imported_adapted"
            else:
                label_note = copyright_note or "原创/非真题；未冒充历年真题"
                verification_basis = "original_not_a_past_paper"
            content_hash = _hash_payload(
                {
                    "course": course,
                    "question_type": question_type,
                    "stem": " ".join(stem.split()).casefold(),
                    "options": cleaned_options,
                    "answer": " ".join(answer.split()).casefold(),
                    "source_type": source_type,
                    "paper_year": paper_year,
                }
            )
            where, params = _course_where(course)
            with self.tx() as conn:
                verified_nodes = self._verified_node_ids(conn, course, node_ids)
                duplicate = conn.execute(
                    f"""SELECT id, source_type, imported_by_admin FROM questions
                        WHERE {where} AND content_hash = ?""",
                    [*params, content_hash],
                ).fetchone()
                if duplicate is not None:
                    duplicate_verification = self._question_verification(
                        duplicate["source_type"], duplicate["imported_by_admin"]
                    )
                    return {
                        "ok": True,
                        "duplicate": True,
                        "saved": True,
                        "question_id": duplicate["id"],
                        "originality": duplicate["source_type"],
                        "source_type": duplicate["source_type"],
                        **duplicate_verification,
                    }
                source_id = (
                    self._source_id(conn, clean_evidence) if clean_evidence else None
                )
                question_id = "q_" + uuid.uuid4().hex
                imported_by_admin = 1 if admin_import else 0
                conn.execute(
                    """INSERT INTO questions
                       (id, school_id, college_id, program_code, exam_year,
                        subject_code, question_type, stem, options_json, answer,
                        explanation, source_type, paper_year, difficulty, source_id,
                        copyright_note, verification_basis, imported_by_admin,
                        content_hash, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        question_id,
                        course["school_id"],
                        course["college_id"],
                        course["program_code"],
                        course["exam_year"],
                        course["subject_code"],
                        question_type,
                        stem,
                        json.dumps(cleaned_options, ensure_ascii=False),
                        answer,
                        explanation,
                        source_type,
                        paper_year,
                        difficulty,
                        source_id,
                        label_note,
                        verification_basis,
                        imported_by_admin,
                        content_hash,
                        created_by,
                        _now(),
                    ],
                )
                conn.executemany(
                    "INSERT INTO question_nodes (question_id, node_id) VALUES (?, ?)",
                    [(question_id, node_id) for node_id in verified_nodes],
                )
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.IntegrityError:
            return _error("conflict", "question conflicted with an existing record")
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")

        verification = self._question_verification(source_type, imported_by_admin)
        if source_type == "past_paper":
            disclaimer = (
                "Admin offline evidence import only: source evidence is recorded as "
                "supplied, but authenticity was not independently checked online. "
                "Do not present it as an officially confirmed past paper."
            )
        elif source_type == "adapted":
            disclaimer = (
                "Admin offline evidence import only: this is an adapted question, "
                "not a past paper. Source evidence was not independently checked online."
            )
        else:
            disclaimer = (
                "Original practice question, not a past paper and not source-verified."
            )
        return {
            "ok": True,
            "duplicate": False,
            "saved": True,
            "question_id": question_id,
            "originality": source_type,
            "source_type": source_type,
            "copyright_note": label_note,
            "copyright_status": "declared" if copyright_note else "unknown",
            "paper_year": paper_year,
            "node_ids": verified_nodes,
            "source": clean_evidence,
            **verification,
            "disclaimer": disclaimer,
        }

    def find_questions(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        query: str = "",
        source_type: str = "",
        paper_year: int | None = None,
        node_id: str = "",
        difficulty: str = "",
        limit: int = 10,
        include_unverified: bool = False,
    ) -> dict:
        """Find stored questions for one exact course with explicit trust labels.

        Original practice questions are returned with ``verified=False``.
        Legacy past-paper/adapted rows that were not imported by an admin are
        excluded by default; set ``include_unverified=True`` to inspect them
        with ``verified=False`` and ``verification_status=legacy_unverified_*``.
        """

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            if not isinstance(query, str):
                raise UserError("invalid_query", "query must be a string")
            tokens = self._like_tokens(query)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise UserError("invalid_input", "limit must be an integer")
            limit = max(1, min(MAX_LIMIT, limit))
            if not isinstance(include_unverified, bool):
                raise UserError("invalid_input", "include_unverified must be boolean")
            source_type = (source_type or "").strip()
            if source_type and source_type not in SOURCE_TYPES:
                raise UserError("invalid_source_type", "unknown source_type")
            difficulty = (difficulty or "").strip()
            if difficulty and difficulty not in DIFFICULTIES:
                raise UserError("invalid_difficulty", "unknown difficulty")
            if paper_year is not None and (
                isinstance(paper_year, bool) or not isinstance(paper_year, int)
            ):
                raise UserError("invalid_input", "paper_year must be an integer")
            node_id = (node_id or "").strip()
            clauses = [
                "q.school_id = ? AND q.college_id = ? AND q.program_code = ? "
                "AND q.exam_year = ? AND q.subject_code = ?"
            ]
            query_params: list = [
                course["school_id"],
                course["college_id"],
                course["program_code"],
                course["exam_year"],
                course["subject_code"],
            ]
            with self.read() as conn:
                if node_id:
                    node = conn.execute(
                        f"SELECT id FROM knowledge_nodes WHERE {_course_where(course)[0]} AND id = ?",
                        [*_course_where(course)[1], node_id],
                    ).fetchone()
                    if node is None:
                        raise UserError("unknown_node", "node is not verified in this course")
                    clauses.append(
                        "q.id IN (SELECT question_id FROM question_nodes WHERE node_id = ?)"
                    )
                    query_params.append(node_id)
                if source_type:
                    clauses.append("q.source_type = ?")
                    query_params.append(source_type)
                if not include_unverified:
                    clauses.append(
                        "(q.source_type = 'original' OR q.imported_by_admin = 1)"
                    )
                if paper_year is not None:
                    clauses.append("q.paper_year = ?")
                    query_params.append(paper_year)
                if difficulty:
                    clauses.append("q.difficulty = ?")
                    query_params.append(difficulty)
                for token in tokens:
                    like = self._like(token)
                    clauses.append(
                        "(q.stem LIKE ? ESCAPE '\\' OR q.explanation LIKE ? ESCAPE '\\')"
                    )
                    query_params.extend([like, like])
                rows = conn.execute(
                    f"""SELECT q.*, s.id AS source_id, s.title AS source_title,
                               s.publisher AS source_publisher, s.url AS source_url,
                               s.document_ref AS source_document_ref,
                               s.evidence_span AS source_span,
                               s.retrieved_at AS source_retrieved_at
                        FROM questions q LEFT JOIN sources s ON s.id = q.source_id
                        WHERE {' AND '.join(clauses)}
                        ORDER BY q.created_at DESC
                        LIMIT ?""",
                    [*query_params, limit],
                ).fetchall()
                question_ids = [row["id"] for row in rows]
                links: dict[str, list[str]] = {question_id: [] for question_id in question_ids}
                if question_ids:
                    placeholders = ",".join("?" for _ in question_ids)
                    link_rows = conn.execute(
                        f"SELECT question_id, node_id FROM question_nodes WHERE question_id IN ({placeholders})",
                        question_ids,
                    ).fetchall()
                    for link in link_rows:
                        links[link["question_id"]].append(link["node_id"])
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")

        results = []
        for row in rows:
            item = {
                "question_id": row["id"],
                "question_type": row["question_type"],
                "stem": row["stem"],
                "options": json.loads(row["options_json"] or "[]"),
                "answer": row["answer"],
                "explanation": row["explanation"],
                "originality": row["source_type"],
                "source_type": row["source_type"],
                "paper_year": row["paper_year"],
                "difficulty": row["difficulty"],
                "node_ids": links.get(row["id"], []),
                "copyright_note": row["copyright_note"],
                "source": None,
            }
            item.update(
                self._question_verification(
                    row["source_type"], row["imported_by_admin"]
                )
            )
            if row["source_id"]:
                item["source"] = self._source_dict(row)
            results.append(item)
        return {
            "ok": True,
            "course": course,
            "verified_only": False,
            "include_unverified": include_unverified,
            "verification_policy": (
                "past_paper/adapted require admin evidence import; original is "
                "unverified; authenticity checks are not online authenticated"
            ),
            "count": len(results),
            "results": results,
        }

    def record_attempt(
        self,
        school_id: str,
        college_id: str,
        program_code: str,
        exam_year: int,
        subject_code: str,
        question_id: str,
        user_ref: str,
        answer: str,
        is_correct: bool,
        time_spent_seconds: int | None = None,
        attempt_key: str = "",
    ) -> dict:
        """Record one practice attempt and return per-question/user statistics."""

        try:
            course = _course(school_id, college_id, program_code, exam_year, subject_code)
            question_id = _clean_text(question_id, "question_id", 1, 200)
            user_ref = _clean_text(user_ref, "user_ref", 1, 128)
            answer = _clean_text(answer, "answer", 1, 10000)
            if not isinstance(is_correct, bool):
                raise UserError("invalid_input", "is_correct must be boolean")
            if time_spent_seconds is not None:
                if isinstance(time_spent_seconds, bool) or not isinstance(
                    time_spent_seconds, int
                ):
                    raise UserError("invalid_input", "time_spent_seconds must be an integer")
                if not 0 <= time_spent_seconds <= 86400:
                    raise UserError(
                        "invalid_input", "time_spent_seconds must be 0..86400"
                    )
            attempt_key = (attempt_key or "").strip()
            if len(attempt_key) > 128 or any(
                char not in "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                for char in attempt_key
            ):
                raise UserError("invalid_attempt_key", "attempt_key has invalid characters")
            where, params = _course_where(course)
            with self.tx() as conn:
                question = conn.execute(
                    f"SELECT id FROM questions WHERE {where} AND id = ?",
                    [*params, question_id],
                ).fetchone()
                if question is None:
                    raise UserError("unknown_question", "question is not saved in this course")
                if attempt_key:
                    duplicate = conn.execute(
                        """SELECT id, created_at FROM attempts
                           WHERE question_id = ? AND user_ref = ? AND attempt_key = ?""",
                        [question_id, user_ref, attempt_key],
                    ).fetchone()
                    if duplicate is not None:
                        return {
                            "ok": True,
                            "duplicate": True,
                            "attempt_id": duplicate["id"],
                            "created_at": duplicate["created_at"],
                        }
                attempt_id = "att_" + uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO attempts
                       (id, question_id, course_key, user_ref, answer, is_correct,
                        time_spent_seconds, attempt_key, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        attempt_id,
                        question_id,
                        "|".join(str(value) for value in course.values()),
                        user_ref,
                        answer,
                        1 if is_correct else 0,
                        time_spent_seconds,
                        attempt_key,
                        _now(),
                    ],
                )
                stats = conn.execute(
                    """SELECT COUNT(*) AS attempts,
                              COALESCE(SUM(is_correct), 0) AS correct
                       FROM attempts WHERE question_id = ? AND user_ref = ?""",
                    [question_id, user_ref],
                ).fetchone()
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "knowledge database is unavailable")
        attempts = int(stats["attempts"])
        correct = int(stats["correct"])
        return {
            "ok": True,
            "duplicate": False,
            "attempt_id": attempt_id,
            "question_id": question_id,
            "is_correct": is_correct,
            "stats": {
                "attempts": attempts,
                "correct": correct,
                "accuracy": round(correct / attempts, 4) if attempts else 0.0,
            },
        }

    # ------------------------------------------------------------------ setup
    allow_review: bool = False


def build_server(
    data_dir: str | Path | None = None,
    allow_review: bool | None = None,
) -> MCPServer:
    """Build the stdio MCP server.

    ``allow_review`` is an explicit test/dev override; production startup uses
    only ``POSTGRAD_ALLOW_REVIEW=1``. The review tool is not registered at all
    when disabled.
    """

    directory = Path(data_dir).expanduser() if data_dir is not None else _data_dir()
    review = _allow_review() if allow_review is None else bool(allow_review)
    store = KnowledgeStore(directory / "exam_knowledge.sqlite3")
    store.allow_review = review

    server = MCPServer(SERVER_NAME, version=SERVER_VERSION)
    for tool in (
        store.ping,
        store.get_syllabus,
        store.search_knowledge,
        store.propose_node,
        store.propose_relation,
        store.get_prerequisites,
        store.find_questions,
        store.save_verified_question,
        store.record_attempt,
    ):
        server.add_tool(tool)
    if review:
        server.add_tool(store.review_proposals)
    return server


if __name__ == "__main__":
    build_server().run("stdio")
