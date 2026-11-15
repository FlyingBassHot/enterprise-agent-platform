from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin


class ModelProvider(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "model_providers"

    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extra: Mapped[dict] = mapped_column(default=dict, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ModelConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "model_configs"

    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("model_providers.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    params: Mapped[dict] = mapped_column(default=dict, nullable=False)
    capabilities: Mapped[dict] = mapped_column(default=dict, nullable=False)
    extra: Mapped[dict] = mapped_column(default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
