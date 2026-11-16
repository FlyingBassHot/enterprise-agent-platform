"""Deterministic offline provider.

Used by the demo and by tests. Supports a per-model script:
`extra.script` is a list of steps consumed by assistant-message count:
    {"content": "..."} or {"tool_calls": [{"name": "...", "arguments": {...}}]}
When no script is present it can drive the built-in audit demo by calling
`analyze_transactions` when the user message references an uploaded artifact.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence

from app.core.contracts import (
    ChatMessage,
    ChatResult,
    ModelStreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)
from app.infrastructure.model_gateway.base import BaseModelProvider

_ARTIFACT_RE = re.compile(r"artifact_id=([0-9a-fA-F-]{36})")


class MockModelProvider(BaseModelProvider):
    async def health(self) -> tuple[bool, str]:
        return True, "mock provider ready"

    def _script_step(self, messages: Sequence[ChatMessage]) -> dict | None:
        script = self.resolved.extra.get("script")
        if not isinstance(script, list) or not script:
            return None
        index = sum(1 for message in messages if message.role == "assistant")
        if index < len(script) and isinstance(script[index], dict):
            return script[index]
        return None

    def _compose(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ChatMessage:
        turn = sum(1 for message in messages if message.role == "assistant")
        step = self._script_step(messages)
        if step is not None:
            calls = []
            for raw in step.get("tool_calls", []) or []:
                calls.append(
                    ToolCall(
                        id=raw.get("id") or f"call_mock_{turn}_{len(calls)}",
                        name=raw["name"],
                        arguments=raw.get("arguments") or {},
                    )
                )
            return ChatMessage(
                role="assistant", content=step.get("content", "") or "", tool_calls=calls
            )

        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"), ""
        )
        tool_names = {tool.name for tool in tools}
        has_tool_result = any(message.role == "tool" for message in messages)

        artifact = _ARTIFACT_RE.search(last_user)
        if artifact and "analyze_transactions" in tool_names and not has_tool_result:
            return ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call_mock_{abs(hash(last_user)) % 100000}",
                        name="analyze_transactions",
                        arguments={"artifact_id": artifact.group(1)},
                    )
                ],
            )

        if has_tool_result:
            last_tool = next(
                (message.content for message in reversed(messages) if message.role == "tool"), ""
            )
            summary = self._summarize_tool_output(last_tool)
            return ChatMessage(role="assistant", content=summary)

        reply = self.resolved.extra.get("reply")
        if isinstance(reply, str) and reply:
            return ChatMessage(role="assistant", content=reply)

        preview = last_user.strip().replace("\n", " ")[:120]
        return ChatMessage(
            role="assistant",
            content=(
                "【Mock 模型】已收到请求："
                f"{preview or '（空）'}。\n\n"
                "当前使用离线 Mock Provider，用于演示平台链路。"
                "在“模型管理”中切换为 OpenAI 兼容模型即可获得真实推理。"
            ),
        )

    @staticmethod
    def _summarize_tool_output(raw: str) -> str:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return f"工具执行完成：{raw[:800]}"
        if isinstance(data, dict) and isinstance(data.get("output"), (dict, list)):
            data = data["output"]
        if isinstance(data, dict):
            anomalies = data.get("anomalies") or data.get("findings") or []
            total = data.get("summary", {}).get("total_transactions")
            if anomalies:
                lines = [
                    f"分析完成：共识别 {len(anomalies)} 条异常交易。"
                    + (f" 交易总数 {total}。" if total is not None else "")
                ]
                for item in anomalies[:5]:
                    lines.append(
                        f"- [{item.get('rule', 'rule')}] "
                        f"{item.get('description', '')} "
                        f"(row={item.get('row_number')}, 金额={item.get('amount')})"
                    )
                if len(anomalies) > 5:
                    lines.append(f"- 其余 {len(anomalies) - 5} 条见结构化结果。")
                return "\n".join(lines)
            return "分析完成，未发现符合当前规则的异常交易。"
        return f"工具执行完成：{raw[:800]}"

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        message = self._compose(messages, tools)
        prompt_chars = sum(len(message.content) for message in messages)
        completion_chars = len(message.content)
        usage = Usage(
            prompt_tokens=max(1, prompt_chars // 4),
            completion_tokens=max(1, completion_chars // 4),
            total_tokens=max(1, (prompt_chars + completion_chars) // 4),
        )
        return ChatResult(
            message=message,
            finish_reason="tool_calls" if message.tool_calls else "stop",
            usage=usage,
        )

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        message = self._compose(messages, tools)
        content = message.content or ""
        for index in range(0, len(content), 32):
            yield ModelStreamEvent(kind="delta", content=content[index : index + 32])
        for call in message.tool_calls:
            yield ModelStreamEvent(kind="tool_call", tool_call=call)
        prompt_chars = sum(len(item.content) for item in messages)
        completion_chars = len(content)
        yield ModelStreamEvent(
            kind="done",
            finish_reason="tool_calls" if message.tool_calls else "stop",
            usage=Usage(
                prompt_tokens=max(1, prompt_chars // 4),
                completion_tokens=max(1, completion_chars // 4),
                total_tokens=max(1, (prompt_chars + completion_chars) // 4),
            ),
        )
