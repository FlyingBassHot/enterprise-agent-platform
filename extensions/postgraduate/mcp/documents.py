"""Bounded document parsing and export within the extension's own workspace."""

from __future__ import annotations

import hashlib
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mcp.server.mcpserver import MCPServer

server = MCPServer("postgrad-documents")
MAX_SIZE = 20 * 1024 * 1024


def data_root() -> Path:
    return Path(
        os.getenv("POSTGRAD_DATA_DIR") or Path(__file__).resolve().parents[1] / "data"
    ).resolve()


def _input(filename: str) -> Path:
    root = data_root() / "documents"
    if root.is_symlink():
        raise ValueError("Documents directory must not be a symbolic link")
    candidate = root / filename
    if (
        not filename
        or candidate.is_symlink()
        or not candidate.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("Document must be inside this extension's documents directory")
    path = candidate.resolve()
    if not path.is_file() or path.stat().st_size > MAX_SIZE:
        raise ValueError("Document missing or exceeds 20 MiB")
    return path


@server.tool()
def parse_document(filename: str, source_url: str = "", max_pages: int = 30) -> dict:
    """Parse an operator-imported PDF, DOCX or text; never promote supplied sources to verified."""
    if not 1 <= max_pages <= 50:
        raise ValueError("max_pages must be 1..50")
    path = _input(filename)
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    pages = []
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return {
                "status": "unavailable",
                "reason": "Install optional pypdf in the documents MCP environment",
            }
        reader = PdfReader(path)
        if reader.is_encrypted:
            return {"status": "unsupported", "reason": "Encrypted PDFs are not accepted"}
        if len(reader.pages) > 200:
            return {"status": "unsupported", "reason": "Document exceeds 200-page parsing limit"}
        # Page content can decompress far beyond the input file size.
        for i, page in enumerate(reader.pages[:max_pages], 1):
            streams = page.get("/Contents")
            objects = streams if isinstance(streams, list) else [streams]
            if any(
                s is not None and len(s.get_object().get_data()) > 2 * 1024 * 1024 for s in objects
            ):
                raise ValueError("PDF page content exceeds parsing limit")
            pages.append({"page": i, "text": (page.extract_text() or "")[:8000]})
        truncated = len(reader.pages) > max_pages
    elif suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            return {
                "status": "unavailable",
                "reason": "Install optional python-docx in the documents MCP environment",
            }
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("DOCX expands beyond 50 MiB")
        doc = Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        pages = [
            {
                "page": None,
                "locator": "paragraph order; Word page numbers unavailable",
                "text": text[:12000],
            }
        ]
        truncated = len(text) > 12000
    elif suffix in {".txt", ".md", ".csv"}:
        text = raw.decode("utf-8", errors="replace")
        pages = [{"page": None, "text": text[:12000]}]
        truncated = len(text) > 12000
    else:
        return {"status": "unsupported", "reason": "Supported formats: PDF, DOCX, UTF-8 TXT/MD/CSV"}
    # Bound the entire MCP result below the platform's 20k result truncation guard.
    remaining = 12000
    bounded = []
    for page in pages:
        if remaining <= 0:
            truncated = True
            break
        text = page["text"]
        bounded.append({**page, "text": text[:remaining]})
        truncated = truncated or len(text) > remaining
        remaining -= len(text)
    return {
        "status": "ok",
        "filename": filename,
        "source_url": source_url,
        "verified": False,
        "trust": "untrusted_document_content",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "parsed_at": datetime.now(UTC).isoformat(),
        "pages": bounded,
        "truncated": truncated,
        "warnings": [
            "Source identity and copyright require separate review; scanned pages may need optional OCR"
        ],
    }


@server.tool()
def export_docx(title: str, content: str) -> dict:
    """Export supplied, already reviewed text into an extension-owned DOCX file."""
    if not title.strip() or len(title) > 200 or not content.strip() or len(content) > 100000:
        raise ValueError("Require title <=200 and content <=100000 characters")
    try:
        from docx import Document
    except ImportError:
        return {
            "status": "unavailable",
            "reason": "python-docx not installed in this MCP environment",
        }
    root = data_root() / "exports"
    if root.is_symlink():
        raise ValueError("Exports directory must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"practice-{uuid4().hex}.docx"
    doc = Document()
    doc.add_heading(title, 0)
    for line in content.splitlines():
        doc.add_paragraph(line)
    doc.save(target)
    return {
        "status": "ok",
        "filename": target.name,
        "relative_path": f"exports/{target.name}",
        "size": target.stat().st_size,
        "delivery": "not_sent",
    }


if __name__ == "__main__":
    # Parsing runs in its own short-lived MCP process. Bound allocations before
    # pypdf can decompress untrusted streams, rather than only checking afterward.
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    server.run("stdio")
