from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.core.contracts import ToolContext, ToolDescriptor
from app.modules.tool.exceptions import ToolDefinitionError

NativeHandler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]
RISK_LEVELS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True, slots=True)
class NativeToolEntry:
    descriptor: ToolDescriptor
    handler: NativeHandler

    def __getattr__(self, item: str) -> Any:
        """Allow treating an entry as its descriptor (``entry.name`` etc.)."""
        return getattr(self.descriptor, item)


class NativeToolRegistry:
    """Process-wide registry of platform-owned tools.

    Native handlers are code, not data: nothing coming from the API can install
    a handler, which keeps arbitrary code execution out of the tool module.
    """

    def __init__(self) -> None:
        self._entries: dict[str, NativeToolEntry] = {}
        self._lock = threading.RLock()

    def register(self, descriptor: ToolDescriptor, handler: NativeHandler) -> None:
        name = (descriptor.name or "").strip()
        if not name:
            raise ToolDefinitionError("Native tool descriptor requires a non-empty name")
        schema = descriptor.input_schema or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise ToolDefinitionError(f"Tool {name}: input_schema must be an object schema")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ToolDefinitionError(
                f"Tool {name}: invalid input_schema: {exc.message}"
            ) from exc
        if schema.get("type", "object") != "object":
            raise ToolDefinitionError(f"Tool {name}: input_schema must describe an object")
        if not callable(handler):
            raise ToolDefinitionError(f"Tool {name}: handler must be callable")
        if not inspect.iscoroutinefunction(handler):
            raise ToolDefinitionError(f"Tool {name}: handler must be an async callable")
        if not isinstance(descriptor, ToolDescriptor):
            raise ToolDefinitionError(f"Tool {name}: descriptor must be a ToolDescriptor")
        if descriptor.risk_level not in RISK_LEVELS:
            raise ToolDefinitionError(f"Tool {name}: unsupported risk_level {descriptor.risk_level}")
        if descriptor.timeout_seconds < 1:
            raise ToolDefinitionError(f"Tool {name}: timeout_seconds must be positive")
        registered = descriptor.model_copy(
            update={"name": name, "source_type": "native", "input_schema": schema}, deep=True
        )
        with self._lock:
            self._entries[name] = NativeToolEntry(descriptor=registered, handler=handler)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    def get(self, name: str) -> NativeToolEntry | None:
        with self._lock:
            return self._entries.get(name)

    def all(self) -> list[NativeToolEntry]:
        with self._lock:
            return list(self._entries.values())

    def descriptors(self) -> list[ToolDescriptor]:
        return [entry.descriptor for entry in self.all()]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._entries


native_registry = NativeToolRegistry()


def get_native_registry() -> NativeToolRegistry:
    return native_registry
