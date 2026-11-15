from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

AccessScope = Literal["public", "department", "private"]
KnowledgeBaseStatus = Literal["active", "disabled"]
DocumentStatus = Literal["pending", "processing", "ready", "failed", "deleted"]


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    department_id: str | None = None
    access_scope: AccessScope = "private"
    status: KnowledgeBaseStatus = "active"
    embedding_model: str | None = Field(default=None, max_length=120)
    chunk_size: int | None = Field(default=None, ge=100, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)

    @model_validator(mode="after")
    def _check_overlap(self) -> KnowledgeBaseCreate:
        if (
            self.chunk_size is not None
            and self.chunk_overlap is not None
            and self.chunk_overlap >= self.chunk_size
        ):
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = None
    department_id: str | None = None
    access_scope: AccessScope | None = None
    status: KnowledgeBaseStatus | None = None
    embedding_model: str | None = Field(default=None, max_length=120)
    chunk_size: int | None = Field(default=None, ge=100, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    description: str
    owner_id: str
    tenant_id: str
    department_id: str | None
    access_scope: AccessScope
    status: KnowledgeBaseStatus
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    document_count: int = 0
    created_at: datetime
    updated_at: datetime


class DocumentOut(BaseModel):
    id: str
    knowledge_base_id: str
    owner_id: str
    department_id: str | None
    tenant_id: str
    filename: str
    content_type: str
    size: int
    status: DocumentStatus
    version: int
    error: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    access_scope: AccessScope
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class DocumentVersionOut(BaseModel):
    id: str
    document_id: str
    version: int
    size: int
    checksum: str
    created_by: str | None
    created_at: datetime


class ChunkOut(BaseModel):
    id: str
    document_id: str
    knowledge_base_id: str
    version: int
    chunk_index: int
    content: str
    char_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    metadata_filter: dict[str, Any] | None = None


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    knowledge_base_id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    knowledge_base_ids: list[str]
    results: list[SearchHit] = Field(default_factory=list)


class BindingValidationOut(BaseModel):
    knowledge_bases: list[KnowledgeBaseOut] = Field(default_factory=list)
