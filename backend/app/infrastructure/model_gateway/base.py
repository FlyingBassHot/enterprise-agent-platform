from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from app.core.contracts import (
    ChatMessage,
    ChatResult,
    ModelConfigResolver,
    ModelStreamEvent,
    ToolSpec,
)


class BaseModelProvider(ABC):
    def __init__(self, resolved: ModelConfigResolver, api_key: str = "") -> None:
        self.resolved = resolved
        self.api_key = api_key

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult: ...

    @abstractmethod
    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...

    @abstractmethod
    async def health(self) -> tuple[bool, str]: ...
