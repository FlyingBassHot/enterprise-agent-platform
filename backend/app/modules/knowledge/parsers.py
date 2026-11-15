from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import get_settings
from app.modules.knowledge.exceptions import DocumentParseError, UnsupportedDocumentTypeError

_MIME_TO_KIND = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "text/csv": "csv",
    "application/csv": "csv",
}

_EXT_TO_KIND = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
    ".csv": "csv",
}

_MAX_SHEET_ROWS = 5000
_MAX_CELL_CHARS = 2000


@dataclass(frozen=True)
class ParsedSection:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    sections: list[ParsedSection]
    metadata: dict[str, Any] = field(default_factory=dict)


def detect_kind(filename: str, content_type: str | None = None) -> str:
    suffix = PurePosixPath(filename or "").suffix.lower()
    kind = _EXT_TO_KIND.get(suffix)
    if kind:
        return kind
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    kind = _MIME_TO_KIND.get(mime)
    if kind:
        return kind
    raise UnsupportedDocumentTypeError(
        f"Unsupported document type: {suffix or content_type or 'unknown'}"
    )


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError("Document text is not valid UTF-8 or Latin-1")


def _section_text(sections: list[ParsedSection]) -> str:
    return "\n\n".join(section.text for section in sections if section.text.strip())


def _parse_pdf(data: bytes) -> ParsedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentParseError("Encrypted PDF documents are not supported")
        page_count = len(reader.pages)
        max_pages = get_settings().knowledge_max_pages
        if page_count > max_pages:
            raise DocumentParseError(
                f"PDF has {page_count} pages which exceeds the {max_pages} page limit"
            )
        sections = []
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            sections.append(ParsedSection(text=text, metadata={"page": number}))
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - third-party parsers raise many types
        raise DocumentParseError(f"Failed to parse PDF: {exc}") from exc
    return ParsedDocument(
        text=_section_text(sections),
        sections=sections,
        metadata={"parser": "pdf", "pages": page_count},
    )


def _flush_docx_section(
    text_parts: list[str], section: str | None, sections: list[ParsedSection]
) -> None:
    text = "\n".join(part for part in text_parts if part.strip()).strip()
    if text:
        sections.append(
            ParsedSection(text=text, metadata={"section": section} if section else {})
        )
    text_parts.clear()


def _parse_docx(data: bytes) -> ParsedDocument:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(f"Failed to parse DOCX: {exc}") from exc
    sections: list[ParsedSection] = []
    parts: list[str] = []
    current: str | None = None
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style_name = (getattr(paragraph.style, "name", "") or "").lower()
        is_heading = style_name.startswith(("heading", "title")) or text.startswith("#")
        if is_heading:
            _flush_docx_section(parts, current, sections)
            current = text.lstrip("#").strip() or None
        parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [(cell.text or "").strip() for cell in row.cells]
            text = " | ".join(cell for cell in cells if cell)
            if text:
                parts.append(text)
    _flush_docx_section(parts, current, sections)
    return ParsedDocument(
        text=_section_text(sections),
        sections=sections,
        metadata={"parser": "docx", "sections": len(sections)},
    )


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    text = str(value).strip()
    return text[:_MAX_CELL_CHARS]


def _parse_xlsx(data: bytes) -> ParsedDocument:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(
            io.BytesIO(data), read_only=True, data_only=True, keep_vba=False
        )
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(f"Failed to parse spreadsheet: {exc}") from exc
    sections: list[ParsedSection] = []
    sheet_names: list[str] = []
    try:
        for worksheet in workbook.worksheets:
            sheet_names.append(worksheet.title)
            lines: list[str] = []
            for row_number, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                if row_number > _MAX_SHEET_ROWS:
                    break
                cells = [_cell_text(value) for value in row]
                line = " | ".join(cell for cell in cells if cell)
                if line:
                    lines.append(line)
            text = "\n".join(lines).strip()
            if text:
                sections.append(
                    ParsedSection(text=text, metadata={"sheet": worksheet.title})
                )
    finally:
        workbook.close()
    return ParsedDocument(
        text=_section_text(sections),
        sections=sections,
        metadata={"parser": "xlsx", "sheets": sheet_names},
    )


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _parse_markdown(text: str) -> ParsedDocument:
    sections: list[ParsedSection] = []
    matches = list(_HEADING.finditer(text))
    if not matches:
        stripped = text.strip()
        sections = [ParsedSection(text=stripped, metadata={})] if stripped else []
    else:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(ParsedSection(text=preamble, metadata={}))
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if body:
                sections.append(
                    ParsedSection(text=body, metadata={"section": match.group(2).strip()})
                )
    return ParsedDocument(
        text=_section_text(sections),
        sections=sections,
        metadata={"parser": "md", "sections": len(sections)},
    )


def _parse_csv(text: str) -> ParsedDocument:
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise DocumentParseError(f"Failed to parse CSV: {exc}") from exc
    lines = []
    for row in rows[: _MAX_SHEET_ROWS + 1]:
        line = " | ".join(cell.strip() for cell in row if cell and cell.strip())
        if line:
            lines.append(line[:_MAX_CELL_CHARS * 4])
    body = "\n".join(lines).strip()
    sections = [ParsedSection(text=body, metadata={})] if body else []
    return ParsedDocument(
        text=body,
        sections=sections,
        metadata={"parser": "csv", "rows": len(rows)},
    )


def _parse_plain(text: str, kind: str) -> ParsedDocument:
    body = text.strip()
    sections = [ParsedSection(text=body, metadata={})] if body else []
    return ParsedDocument(
        text=body,
        sections=sections,
        metadata={"parser": kind},
    )


def parse_document(
    filename: str, data: bytes, content_type: str | None = None
) -> ParsedDocument:
    """Parse a supported document into text sections with page/sheet/section metadata."""
    if not data:
        raise DocumentParseError("Document is empty")
    kind = detect_kind(filename, content_type)
    if kind == "pdf":
        return _parse_pdf(data)
    if kind == "docx":
        return _parse_docx(data)
    if kind == "xlsx":
        return _parse_xlsx(data)
    text = _decode_text(data)
    if kind == "md":
        return _parse_markdown(text)
    if kind == "csv":
        return _parse_csv(text)
    return _parse_plain(text, kind)


def document_extension(filename: str) -> str:
    return Path(filename).suffix.lower()
