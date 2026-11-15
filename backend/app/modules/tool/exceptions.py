from __future__ import annotations

from app.core.exceptions import ToolError


class ToolDefinitionError(ToolError):
    """A tool definition or native registration is malformed."""

    status_code = 422
    code = "tool_definition_error"


class ToolExecutionError(ToolError):
    """A tool could not be executed (handler missing, transport failure)."""

    code = "tool_execution_error"


class ToolTimeoutError(ToolExecutionError):
    """A tool exceeded its configured execution timeout."""

    code = "tool_timeout"
