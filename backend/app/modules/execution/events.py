"""Execution event persistence and the in-process live event broker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.modules.execution.models import ApprovalRequest, LLMCall
from app.modules.execution.repository import ExecutionRepository
from app.modules.runtime.context import ApprovalHandle, LLMCallRecord

APPROVAL_EVENT_TYPES = {"approval_required", "approval_resolved"}

TERMINAL_EVENT_TYPES = {
    "execution_completed",
    "execution_failed",
    "execution_cancelled",
}


def serialize_event(
    seq: int, execution_id: str, event_type: str, payload: dict[str, Any], created_at: datetime
) -> dict[str, Any]:
    return {
        "seq": seq,
        "execution_id": execution_id,
        "type": event_type,
        "payload": payload or {},
        "created_at": created_at.isoformat(),
    }


class ExecutionEventBroker:
    """Fan-out of persisted events to connected SSE clients."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, execution_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.setdefault(execution_id, set()).add(queue)
        return queue

    def unsubscribe(self, execution_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(execution_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(execution_id, None)

    def publish(self, execution_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(execution_id, ())):
            if queue.full():
                continue
            queue.put_nowait(event)

    def subscriber_count(self, execution_id: str) -> int:
        return len(self._subscribers.get(execution_id, ()))


_broker: ExecutionEventBroker | None = None


def get_broker() -> ExecutionEventBroker:
    global _broker
    if _broker is None:
        _broker = ExecutionEventBroker()
    return _broker


class ExecutionEventSink:
    """Database-backed sink bound to a single execution."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        execution_id: str,
        broker: ExecutionEventBroker | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.execution_id = execution_id
        self.broker = broker or get_broker()

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        payload = payload or {}
        async with self.session_factory() as db:
            repo = ExecutionRepository(db)
            approval_id = payload.get("approval_id")
            if (
                event_type in APPROVAL_EVENT_TYPES
                and isinstance(approval_id, str)
                and approval_id
            ):
                approval = await repo.get_approval(approval_id)
                if approval is not None:
                    emitted = list((approval.payload or {}).get("emitted") or [])
                    if event_type in emitted:
                        return 0
                    approval.payload = {
                        **(approval.payload or {}),
                        "emitted": [*emitted, event_type],
                    }
            row = await repo.append_event(self.execution_id, event_type, payload)
            await db.commit()
            event = serialize_event(row.seq, self.execution_id, event_type, payload, row.created_at)
        self.broker.publish(self.execution_id, event)
        return int(row.seq)

    async def record_llm_call(self, record: LLMCallRecord) -> None:
        async with self.session_factory() as db:
            db.add(
                LLMCall(
                    execution_id=self.execution_id,
                    model_config_id=record.model_config_id,
                    provider=record.provider,
                    model=record.model,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    total_tokens=record.total_tokens,
                    latency_ms=record.latency_ms,
                    status=record.status,
                    error=record.error,
                )
            )
            await db.commit()

    async def request_approval(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        kind: str = "tool_call",
        title: str | None = None,
    ) -> ApprovalHandle:
        async with self.session_factory() as db:
            repo = ExecutionRepository(db)
            existing = await repo.get_approval_by_call(self.execution_id, tool_call_id)
            if existing is not None:
                return ApprovalHandle(id=existing.id, created=False)
            approval = ApprovalRequest(
                id=new_id(),
                execution_id=self.execution_id,
                kind=kind,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                status="pending",
                payload={"risk_level": risk_level, "title": title},
                requested_at=datetime.now(UTC),
            )
            await repo.create_approval(approval)
            await db.commit()
            return ApprovalHandle(id=approval.id, created=True)

    async def attach_interrupt_ids(self, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        async with self.session_factory() as db:
            repo = ExecutionRepository(db)
            changed = False
            for approval_id, interrupt_id in mapping.items():
                approval = await repo.get_approval(approval_id)
                if approval is None:
                    continue
                approval.payload = {
                    **(approval.payload or {}),
                    "interrupt_id": interrupt_id,
                    "interrupt_bound_at": datetime.now(UTC).isoformat(),
                }
                changed = True
            if changed:
                await db.commit()


async def replay_events(
    session_factory: async_sessionmaker[AsyncSession],
    execution_id: str,
    *,
    after_seq: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    async with session_factory() as db:
        rows = await ExecutionRepository(db).list_events(
            execution_id, after_seq=after_seq, limit=limit
        )
        return [
            serialize_event(row.seq, row.execution_id, row.type, row.payload, row.created_at)
            for row in rows
        ]


def format_sse(event: dict[str, Any]) -> str:
    import json

    data = json.dumps(event.get("payload") or {}, ensure_ascii=False, default=str)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


async def live_events(
    broker: ExecutionEventBroker,
    execution_id: str,
    queue: asyncio.Queue[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    try:
        while True:
            yield await queue.get()
    finally:
        broker.unsubscribe(execution_id, queue)
