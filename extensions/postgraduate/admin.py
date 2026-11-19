"""Operator-only offline data import and evidence review; never registered as an Agent tool."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "mcp"))


def execute(command: str, filename: Path, data_dir: Path, reviewer: str) -> list | dict:
    if filename.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Import file exceeds 5 MiB")
    payload = json.loads(filename.read_text(encoding="utf-8"))
    os.environ["POSTGRAD_DATA_DIR"] = str(data_dir.resolve())
    if command == "import-admissions":
        import university_data

        if not isinstance(payload, list) or not 1 <= len(payload) <= 1000:
            raise ValueError("Expect 1..1000 objects containing source, program and record")
        os.environ["POSTGRAD_ALLOW_VERIFIED_WRITE"] = "1"
        return [
            university_data.upsert_verified_record(item["source"], item["program"], item["record"])
            for item in payload
        ]
    import exam_knowledge

    store = exam_knowledge.KnowledgeStore(data_dir / "exam_knowledge.sqlite3")
    store.allow_review = True
    if command == "review-knowledge":
        return store.review_proposals(reviewer=reviewer, decisions=payload)
    if not isinstance(payload, dict):
        raise ValueError("Question import must be a single tool argument object")
    return store.save_verified_question(**payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["import-admissions", "review-knowledge", "import-question"]
    )
    parser.add_argument("filename", type=Path)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--reviewer", default="operator")
    args = parser.parse_args()
    response = execute(args.command, args.filename, args.data_dir, args.reviewer)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    results = response if isinstance(response, list) else [response]
    if any(
        r.get("ok") is False
        or r.get("status") in {"rejected", "storage_error", "disabled", "invalid_request"}
        for r in results
    ):
        sys.exit(1)
