"""Audit analysis domain pack.

The pack stays outside platform core: it exposes pure analysis helpers, two
native tools and a seed routine. ``register`` imports the tool registry lazily
so the pack can be imported in isolation.
"""

from __future__ import annotations

from typing import Any

PLUGIN_NAME = "audit"
PLUGIN_VERSION = "1.0.0"

__all__ = ["PLUGIN_NAME", "PLUGIN_VERSION", "register", "seed_audit_demo"]


def register(registry: Any = None) -> list[Any]:
    """Register the audit native tools, returning their descriptors.

    Idempotent: re-registering replaces the existing entries.
    """

    if registry is None:
        from app.modules.tool.registry import get_native_registry

        registry = get_native_registry()
    from app.domain_plugins.audit.tools import register_audit_tools

    return register_audit_tools(registry)


def __getattr__(name: str) -> Any:
    if name == "seed_audit_demo":
        from app.domain_plugins.audit.seed import seed_audit_demo

        return seed_audit_demo
    raise AttributeError(name)
