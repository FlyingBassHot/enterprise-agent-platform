from __future__ import annotations

from app.core.exceptions import AppError


class WorkflowError(AppError):
    """Base class for workflow management failures."""

    status_code = 400
    code = "workflow_error"


class WorkflowNotPublishedError(WorkflowError):
    """A draft workflow cannot be bound to an agent or executed."""

    status_code = 409
    code = "workflow_not_published"


class WorkflowDisabledError(WorkflowError):
    """A disabled workflow cannot be bound to an agent or executed."""

    status_code = 409
    code = "workflow_disabled"


class WorkflowBindingError(WorkflowError):
    """A workflow node references a resource that is not usable."""

    status_code = 422
    code = "workflow_binding_error"


__all__ = [
    "WorkflowBindingError",
    "WorkflowDisabledError",
    "WorkflowError",
    "WorkflowNotPublishedError",
]
