from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ValidationError

_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]", re.IGNORECASE)


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [round(value / norm, 8) for value in vector]


class HashEmbeddingProvider:
    """Deterministic offline embedding using signed sha256 token/trigram hashing."""

    def __init__(self, dimension: int = 384, name: str | None = None) -> None:
        if dimension <= 0:
            raise ValidationError("Embedding dimension must be positive")
        self.dimension = dimension
        self.name = name or f"hash-{dimension}"

    def _hash(self, token: str) -> tuple[int, float]:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % self.dimension
        sign = 1.0 if digest[4] & 1 == 0 else -1.0
        return index, sign

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        lowered = (text or "").lower()
        for word in _TOKEN.findall(lowered):
            index, sign = self._hash(f"w:{word}")
            vector[index] += sign * 1.0
            if len(word) >= 3 and not ("\u3400" <= word <= "\u9fff"):
                padded = f" {word} "
                for offset in range(len(padded) - 2):
                    trigram = padded[offset : offset + 3]
                    index, sign = self._hash(f"t:{trigram}")
                    vector[index] += sign * 0.5
        return _normalize(vector)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAICompatibleEmbeddingProvider:
    """Optional OpenAI-compatible embeddings endpoint (no Chat model config required)."""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str,
        dimension: int = 384,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValidationError(
                "embedding_base_url is required when embedding_provider=openai_compatible"
            )
        if not model:
            raise ValidationError("embedding_model is required for the embeddings provider")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.name = model
        self.dimension = dimension
        self.timeout = timeout

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.name, "input": list(texts)},
            )
        if response.status_code >= 400:
            raise ValidationError(
                f"Embedding provider returned {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        vectors = [item["embedding"] for item in payload.get("data", [])]
        if len(vectors) != len(texts):
            raise ValidationError("Embedding provider returned an unexpected number of vectors")
        self.dimension = len(vectors[0]) if vectors else self.dimension
        return [_normalize([float(value) for value in vector]) for vector in vectors]


def create_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    resolved = settings or get_settings()
    provider = (resolved.embedding_provider or "hash").strip().lower()
    if provider in {"hash", "local", "deterministic"}:
        dimension = resolved.default_embedding_dim
        return HashEmbeddingProvider(dimension=dimension, name=resolved.embedding_model or None)
    if provider in {"openai", "openai_compatible", "http"}:
        return OpenAICompatibleEmbeddingProvider(
            base_url=resolved.embedding_base_url,
            api_key=resolved.embedding_api_key,
            model=resolved.embedding_model,
            dimension=resolved.default_embedding_dim,
        )
    raise ValidationError(f"Unsupported embedding provider: {resolved.embedding_provider}")
