from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.contracts import (
    ChatMessage,
    ChatResult,
    ModelStreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.core.exceptions import ModelError
from app.infrastructure.model_gateway.base import BaseModelProvider


class OpenAICompatibleProvider(BaseModelProvider):
    """vLLM / LiteLLM / OpenAI-compatible `/chat/completions` client."""

    def _url(self, suffix: str) -> str:
        base = (self.resolved.base_url or "").rstrip("/")
        if not base:
            raise ModelError("Model provider base_url is not configured")
        return f"{base}/{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.resolved.model,
            "messages": [message.to_provider() for message in messages],
            "temperature": (
                self.resolved.temperature if temperature is None else temperature
            ),
            "max_tokens": self.resolved.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = [tool.to_provider() for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        payload = self._payload(messages, tools, temperature, max_tokens, stream=False)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                response = await client.post(
                    self._url("chat/completions"), json=payload, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ModelError(
                f"Model provider returned {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        try:
            choice = data["choices"][0]
            raw_message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("Model provider returned an unexpected payload") from exc
        tool_calls: list[ToolCall] = []
        for index, call in enumerate(raw_message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=call.get("id") or f"call_{index}",
                    name=function.get("name") or "",
                    arguments=_parse_arguments(function.get("arguments")),
                )
            )
        message = ChatMessage(
            role="assistant",
            content=raw_message.get("content") or "",
            tool_calls=[call for call in tool_calls if call.name],
        )
        usage = data.get("usage") or {}
        return ChatResult(
            message=message,
            finish_reason=choice.get("finish_reason") or "stop",
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ),
        )

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        payload = self._payload(messages, tools, temperature, max_tokens, stream=True)
        tool_calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason = "stop"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                async with client.stream(
                    "POST", self._url("chat/completions"), json=payload, headers=self._headers()
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")
                        raise ModelError(
                            f"Model provider returned {response.status_code}: {body[:500]}"
                        )
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("usage"):
                            raw_usage = chunk["usage"]
                            usage = Usage(
                                prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                                completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                                total_tokens=int(raw_usage.get("total_tokens") or 0),
                            )
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield ModelStreamEvent(kind="delta", content=content)
                            usage.completion_tokens += max(1, len(content) // 4)
                        for raw_call in delta.get("tool_calls") or []:
                            index = int(raw_call.get("index", 0))
                            slot = tool_calls.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            if raw_call.get("id"):
                                slot["id"] = raw_call["id"]
                            function = raw_call.get("function") or {}
                            if function.get("name"):
                                slot["name"] += function["name"]
                            if function.get("arguments"):
                                slot["arguments"] += function["arguments"]
        except httpx.HTTPError as exc:
            raise ModelError(f"Model stream failed: {exc}") from exc

        for index in sorted(tool_calls):
            slot = tool_calls[index]
            if not slot["name"]:
                continue
            yield ModelStreamEvent(
                kind="tool_call",
                tool_call=ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=_parse_arguments(slot["arguments"]),
                ),
            )
        if usage.total_tokens == 0:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        yield ModelStreamEvent(kind="done", finish_reason=finish_reason, usage=usage)

    async def health(self) -> tuple[bool, str]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self._url("models"), headers=self._headers())
            latency = round((time.perf_counter() - started) * 1000)
            if response.status_code < 400:
                return True, f"reachable in {latency}ms"
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
        except (httpx.HTTPError, ModelError) as exc:
            return False, str(exc)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {"_raw": str(raw)}
