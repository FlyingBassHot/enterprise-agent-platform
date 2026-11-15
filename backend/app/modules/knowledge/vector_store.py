from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.contracts import RetrievalChunk

_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)


class Reranker(Protocol):
    async def rerank(
        self,
        query: str,
        chunks: Sequence[RetrievalChunk],
        *,
        top_k: int,
    ) -> list[RetrievalChunk]: ...


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk_id: str
    document_id: str
    document_title: str
    knowledge_base_id: str
    content: str
    vector: Sequence[float]
    metadata: dict[str, Any] = field(default_factory=dict)
    document_metadata: dict[str, Any] = field(default_factory=dict)
    document_version: int = 1


def tokens(text: str) -> set[str]:
    return {match.lower() for match in _TOKEN.findall(text or "")}


def keyword_overlap(query: str, content: str) -> float:
    query_tokens = tokens(query)
    if not query_tokens:
        return 0.0
    content_tokens = tokens(content)
    if not content_tokens:
        return 0.0
    return len(query_tokens & content_tokens) / len(query_tokens)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _metadata_matches(
    candidate: RetrievalCandidate, metadata_filter: Mapping[str, Any]
) -> bool:
    for key, expected in metadata_filter.items():
        if key == "document_id":
            actual: Any = candidate.document_id
        elif key == "knowledge_base_id":
            actual = candidate.knowledge_base_id
        elif key == "document_version":
            actual = candidate.document_version
        else:
            actual = candidate.document_metadata.get(key)
        if actual != expected:
            return False
    return True


class LocalVectorStore:
    """Brute-force local vector search over preloaded embedding candidates."""

    def __init__(
        self,
        *,
        mode: str = "hybrid",
        vector_weight: float = 0.7,
        keyword_weight: float = 0.3,
        reranker: Reranker | None = None,
    ) -> None:
        self.mode = (mode or "hybrid").lower()
        self.vector_weight = vector_weight
        self.keyword_weight = keyword_weight
        self.reranker = reranker

    def _score(self, query: str, query_vector: Sequence[float], candidate: RetrievalCandidate) -> float:
        cosine = max(0.0, cosine_similarity(query_vector, candidate.vector))
        if self.mode in {"vector", "embedding"}:
            return cosine
        keyword = keyword_overlap(query, candidate.content)
        if self.mode in {"keyword", "fulltext", "text"}:
            return keyword
        return self.vector_weight * cosine + self.keyword_weight * keyword

    async def search(
        self,
        *,
        query: str,
        query_vector: Sequence[float],
        candidates: Sequence[RetrievalCandidate],
        top_k: int = 5,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> list[RetrievalChunk]:
        scored: list[tuple[float, RetrievalCandidate]] = []
        for candidate in candidates:
            if metadata_filter and not _metadata_matches(candidate, metadata_filter):
                continue
            score = self._score(query, query_vector, candidate)
            if score <= 0.0:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
        limit = max(1, int(top_k))
        chunks = [
            RetrievalChunk(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                document_title=candidate.document_title,
                knowledge_base_id=candidate.knowledge_base_id,
                content=candidate.content,
                score=round(score, 6),
                metadata={
                    **candidate.metadata,
                    "document_version": candidate.document_version,
                },
            )
            for score, candidate in scored[:limit]
        ]
        if self.reranker is not None and chunks:
            return await self.reranker.rerank(query, chunks, top_k=limit)
        return chunks
