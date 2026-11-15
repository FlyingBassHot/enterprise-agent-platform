from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AuditResult = Literal["success", "failure", "denied"]


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    occurred_at: datetime
    actor_id: str | None = None
    actor_username: str = ""
    action: str
    resource_type: str = ""
    resource_id: str | None = None
    result: AuditResult = "success"
    status_code: int | None = None
    ip_address: str | None = None
    request_id: str | None = None
    tenant_id: str = "default"
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("occurred_at", "created_at", mode="before")
    @classmethod
    def _assume_utc(cls, value: Any) -> Any:
        # SQLite drops tzinfo on read; treat stored wall-clock values as UTC so
        # every API response carries an explicit offset.
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
