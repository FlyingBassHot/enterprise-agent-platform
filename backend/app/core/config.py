from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="APP_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    name: str = "Enterprise Agent Platform"
    env: str = "dev"
    debug: bool = True
    api_prefix: str = "/api/v1"

    secret_key: str = "dev-secret-change-me-in-production"
    access_token_ttl_minutes: int = 720
    refresh_token_ttl_days: int = 14

    database_url: str = "sqlite+aiosqlite:///./data/platform.db"
    auto_create_tables: bool = True
    sql_echo: bool = False

    storage_backend: str = "local"
    storage_dir: str = "./data/files"
    max_upload_mb: int = 50

    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
    ]

    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: str | None = None
    bootstrap_token: str | None = None

    default_embedding_dim: int = 384
    rag_top_k: int = 5
    rag_chunk_size: int = 900
    rag_chunk_overlap: int = 120

    execution_max_iterations: int = 12
    execution_timeout_seconds: int = 600
    tool_default_timeout_seconds: int = 60
    tool_secret_env_prefix: str = "TOOL_SECRET_"
    worker_inline: bool = True
    sse_keepalive_seconds: int = 15

    telemetry_sink: str = "database"

    # Optional OpenAI-compatible endpoint used by the demo/seed provider.
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "local-model"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Settings:
        if self.env in {"dev", "test"}:
            return self
        insecure_secrets = {
            "",
            "dev-secret-change-me",
            "dev-secret-change-me-in-production",
            "change-me-in-production",
        }
        if self.secret_key in insecure_secrets or len(self.secret_key) < 32:
            raise ValueError(
                "APP_SECRET_KEY must be a random value of at least 32 characters outside dev"
            )
        if self.bootstrap_admin_password in {"admin-pass-123", "change-me"}:
            raise ValueError("APP_BOOTSTRAP_ADMIN_PASSWORD must not use a default value")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def fernet_key(self) -> bytes:
        import base64
        import hashlib

        digest = hashlib.sha256(self.secret_key.encode()).digest()
        return base64.urlsafe_b64encode(digest)


@lru_cache
def get_settings() -> Settings:
    return Settings()
