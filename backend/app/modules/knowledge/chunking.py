from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    char_count: int
    chunk_index: int
    metadata: dict[str, Any] = field(default_factory=dict)


def _preferred_cut(text: str, start: int, end: int) -> int:
    """Find the last paragraph/newline boundary near ``end``; falls back to ``end``."""
    window_start = max(start + 1, end - max(80, (end - start) // 4))
    for separator in ("\n\n", "\n", ". ", "。", "! ", "? "):
        position = text.rfind(separator, window_start, end)
        if position > start:
            return position
    return end


def chunk_text(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    metadata: dict[str, Any] | None = None,
    start_index: int = 0,
    max_chunks: int | None = None,
) -> list[ChunkDraft]:
    """Split text into overlapping character chunks, preferring paragraph boundaries."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    size = max(1, int(chunk_size))
    overlap = max(0, min(int(chunk_overlap), size - 1))
    base_metadata = dict(metadata or {})
    drafts: list[ChunkDraft] = []
    start = 0
    index = start_index
    length = len(cleaned)
    while start < length:
        if max_chunks is not None and len(drafts) >= max_chunks:
            break
        end = min(start + size, length)
        if end < length:
            end = _preferred_cut(cleaned, start, end)
        piece = cleaned[start:end].strip()
        if piece:
            chunk_metadata = {**base_metadata, "chunk_index": index}
            drafts.append(
                ChunkDraft(
                    content=piece,
                    char_count=len(piece),
                    chunk_index=index,
                    metadata=chunk_metadata,
                )
            )
            index += 1
        if end >= length:
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    return drafts


def chunk_sections(
    sections: list[tuple[str, dict[str, Any]]],
    *,
    chunk_size: int,
    chunk_overlap: int,
    max_chunks: int | None = None,
) -> list[ChunkDraft]:
    """Chunk parsed sections, preserving page/section metadata and a global chunk index."""
    drafts: list[ChunkDraft] = []
    for text, metadata in sections:
        if max_chunks is not None and len(drafts) >= max_chunks:
            break
        remaining = None if max_chunks is None else max_chunks - len(drafts)
        drafts.extend(
            chunk_text(
                text,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                metadata=metadata,
                start_index=len(drafts),
                max_chunks=remaining,
            )
        )
    return drafts
