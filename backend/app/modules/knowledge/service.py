from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.contracts import KnowledgeGateway, RetrievalChunk, UserContext
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.ids import new_id
from app.core.logging import get_logger
from app.infrastructure.database.session import get_session_factory
from app.infrastructure.storage.base import StorageBackend
from app.infrastructure.storage.local import get_storage
from app.modules.knowledge.chunking import chunk_sections
from app.modules.knowledge.embeddings import EmbeddingProvider, create_embedding_provider
from app.modules.knowledge.exceptions import (
    DocumentParseError,
    DocumentTooLargeError,
    KnowledgeError,
)
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    EmbeddingRecord,
    KnowledgeBase,
)
from app.modules.knowledge.parsers import detect_kind, parse_document
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.schemas import (
    ChunkOut,
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from app.modules.knowledge.vector_store import LocalVectorStore, Reranker, RetrievalCandidate
from app.modules.permission.service import PermissionService

logger = get_logger(__name__)

_ACCESS_SCOPES = {"public", "department", "private"}
_TERMINAL_STATUSES = {"ready", "failed", "deleted"}
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_DOCUMENT_LOCKS: dict[str, asyncio.Lock] = {}


def _document_lock(document_id: str) -> asyncio.Lock:
    return _DOCUMENT_LOCKS.setdefault(document_id, asyncio.Lock())


def _parse_metadata(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("metadata must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("metadata must be a JSON object")
    return {str(key): value for key, value in payload.items()}


class KnowledgeService(KnowledgeGateway):
    def __init__(
        self,
        db: AsyncSession,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
        storage: StorageBackend | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.repo = KnowledgeRepository(db)
        self.permissions = PermissionService(db)
        self.storage = storage or get_storage()
        self._embedding_provider = embedding_provider
        self.store = LocalVectorStore(
            mode=self.settings.knowledge_search_mode,
            reranker=reranker,
        )

    @property
    def embeddings(self) -> EmbeddingProvider:
        if self._embedding_provider is None:
            self._embedding_provider = create_embedding_provider(self.settings)
        return self._embedding_provider

    # --------------------------------------------------------------- permissions

    async def _base_readable(self, user: UserContext, base: KnowledgeBase) -> bool:
        if user.is_superuser:
            return True
        if base.tenant_id != user.tenant_id:
            return False
        if base.owner_id == user.user_id:
            return True
        if base.access_scope == "public":
            return True
        if (
            base.access_scope == "department"
            and base.department_id
            and base.department_id == user.department_id
        ):
            return True
        return await self.permissions.can_access_resource(
            user,
            resource_type="knowledge_base",
            resource_id=base.id,
            owner_id=base.owner_id,
        )

    async def _base_writable(self, user: UserContext, base: KnowledgeBase) -> bool:
        if user.is_superuser:
            return True
        return base.owner_id == user.user_id

    async def _document_readable(
        self,
        user: UserContext,
        document: Document,
        base: KnowledgeBase,
        *,
        check_base: bool = True,
    ) -> bool:
        if user.is_superuser:
            return True
        if check_base and not await self._base_readable(user, base):
            return False
        if document.owner_id == user.user_id:
            return True
        if document.access_scope == "public":
            return True
        if (
            document.access_scope == "department"
            and document.department_id
            and document.department_id == user.department_id
        ):
            return True
        return await self.permissions.can_access_resource(
            user,
            resource_type="document",
            resource_id=document.id,
            owner_id=document.owner_id,
        )

    async def _document_writable(
        self, user: UserContext, document: Document, base: KnowledgeBase
    ) -> bool:
        if user.is_superuser:
            return True
        return document.owner_id == user.user_id or base.owner_id == user.user_id

    async def _require_base_read(self, actor: UserContext, base_id: str) -> KnowledgeBase:
        base = await self.repo.get_base(base_id)
        if base is None:
            raise NotFoundError("Knowledge base not found")
        if not await self._base_readable(actor, base):
            raise PermissionDeniedError("You cannot access this knowledge base")
        return base

    async def _require_base_write(self, actor: UserContext, base_id: str) -> KnowledgeBase:
        base = await self.repo.get_base(base_id)
        if base is None:
            raise NotFoundError("Knowledge base not found")
        if not await self._base_writable(actor, base):
            raise PermissionDeniedError("You cannot modify this knowledge base")
        return base

    async def _require_document_read(
        self, actor: UserContext, document_id: str
    ) -> tuple[Document, KnowledgeBase]:
        document = await self.repo.get_document(document_id)
        if document is None or document.status == "deleted":
            raise NotFoundError("Document not found")
        base = await self.repo.get_base(document.knowledge_base_id)
        if base is None:
            raise NotFoundError("Knowledge base not found")
        if not await self._document_readable(actor, document, base, check_base=False):
            raise PermissionDeniedError("You cannot access this document")
        return document, base

    async def _require_document_write(
        self, actor: UserContext, document_id: str
    ) -> tuple[Document, KnowledgeBase]:
        document = await self.repo.get_document(document_id)
        if document is None or document.status == "deleted":
            raise NotFoundError("Document not found")
        base = await self.repo.get_base(document.knowledge_base_id)
        if base is None:
            raise NotFoundError("Knowledge base not found")
        if not await self._document_writable(actor, document, base):
            raise PermissionDeniedError("You cannot modify this document")
        return document, base

    # ------------------------------------------------------------------ converters

    async def _base_out(self, base: KnowledgeBase) -> KnowledgeBaseOut:
        return KnowledgeBaseOut(
            id=base.id,
            name=base.name,
            description=base.description,
            owner_id=base.owner_id,
            tenant_id=base.tenant_id,
            department_id=base.department_id,
            access_scope=base.access_scope,  # type: ignore[arg-type]
            status=base.status,  # type: ignore[arg-type]
            embedding_model=base.embedding_model,
            chunk_size=base.chunk_size,
            chunk_overlap=base.chunk_overlap,
            document_count=await self.repo.count_documents(base.id),
            created_at=base.created_at,
            updated_at=base.updated_at,
        )

    def _document_out(self, document: Document, *, chunk_count: int = 0) -> DocumentOut:
        return DocumentOut(
            id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            owner_id=document.owner_id,
            department_id=document.department_id,
            tenant_id=document.tenant_id,
            filename=document.filename,
            content_type=document.content_type,
            size=document.size,
            status=document.status,  # type: ignore[arg-type]
            version=document.version,
            error=document.error,
            metadata=document.doc_metadata or {},
            access_scope=document.access_scope,  # type: ignore[arg-type]
            chunk_count=chunk_count,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )

    def _chunk_out(self, chunk: DocumentChunk) -> ChunkOut:
        return ChunkOut(
            id=chunk.id,
            document_id=chunk.document_id,
            knowledge_base_id=chunk.knowledge_base_id,
            version=chunk.version,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            char_count=chunk.char_count,
            metadata=chunk.chunk_metadata or {},
            created_at=chunk.created_at,
        )

    # ------------------------------------------------------------- knowledge bases

    async def create_base(
        self, actor: UserContext, payload: KnowledgeBaseCreate
    ) -> KnowledgeBaseOut:
        if await self.repo.get_base_by_name(payload.name):
            raise ConflictError("Knowledge base name already exists")
        if payload.access_scope == "department":
            if not (payload.department_id or actor.department_id):
                raise ValidationError("department_id is required for department-scoped bases")
        chunk_size = (
            payload.chunk_size
            if payload.chunk_size is not None
            else self.settings.rag_chunk_size
        )
        chunk_overlap = (
            payload.chunk_overlap
            if payload.chunk_overlap is not None
            else self.settings.rag_chunk_overlap
        )
        if chunk_overlap >= chunk_size:
            raise ValidationError("chunk_overlap must be smaller than chunk_size")
        base = KnowledgeBase(
            name=payload.name,
            description=payload.description,
            owner_id=actor.user_id,
            tenant_id=actor.tenant_id or "default",
            department_id=payload.department_id or actor.department_id,
            access_scope=payload.access_scope,
            status=payload.status,
            embedding_model=payload.embedding_model or self.embeddings.name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self.db.add(base)
        await self.db.commit()
        await self.db.refresh(base)
        return await self._base_out(base)

    async def list_bases(self, actor: UserContext) -> list[KnowledgeBaseOut]:
        bases = await self.repo.list_bases()
        visible = [base for base in bases if await self._base_readable(actor, base)]
        return [await self._base_out(base) for base in visible]

    async def get_base(self, actor: UserContext, base_id: str) -> KnowledgeBaseOut:
        base = await self._require_base_read(actor, base_id)
        return await self._base_out(base)

    async def update_base(
        self, actor: UserContext, base_id: str, payload: KnowledgeBaseUpdate
    ) -> KnowledgeBaseOut:
        base = await self._require_base_write(actor, base_id)
        data = payload.model_dump(exclude_unset=True)
        if "name" in data and data["name"] and data["name"] != base.name:
            if await self.repo.get_base_by_name(data["name"]):
                raise ConflictError("Knowledge base name already exists")
            base.name = data["name"]
        for field_name in ("description", "access_scope", "status", "embedding_model"):
            if field_name in data and data[field_name] is not None:
                setattr(base, field_name, data[field_name])
        if "department_id" in data:
            base.department_id = data["department_id"]
        if "chunk_size" in data and data["chunk_size"] is not None:
            base.chunk_size = data["chunk_size"]
        if "chunk_overlap" in data and data["chunk_overlap"] is not None:
            base.chunk_overlap = data["chunk_overlap"]
        if base.chunk_overlap >= base.chunk_size:
            raise ValidationError("chunk_overlap must be smaller than chunk_size")
        if base.access_scope == "department" and not base.department_id:
            raise ValidationError("department_id is required for department-scoped bases")
        await self.db.commit()
        await self.db.refresh(base)
        return await self._base_out(base)

    async def delete_base(self, actor: UserContext, base_id: str) -> None:
        base = await self._require_base_write(actor, base_id)
        documents = await self.repo.list_base_documents(base_id)
        storage_keys: set[str] = set()
        for document in documents:
            versions = await self.repo.list_versions(document.id)
            storage_keys.update(version.storage_key for version in versions)
            storage_keys.add(document.storage_key)
            await self.repo.delete_chunks(document.id)
            await self.db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document.id)
            )
            await self.db.delete(document)
        await self.db.delete(base)
        await self.db.commit()
        for key in storage_keys:
            await self._delete_storage(key)

    async def _delete_storage(self, key: str) -> None:
        try:
            await self.storage.delete(key)
        except Exception as exc:  # noqa: BLE001 - storage cleanup is best effort
            logger.warning("knowledge storage cleanup failed", extra={"key": key, "error": str(exc)})

    # ------------------------------------------------------------------- documents

    async def upload_document(
        self,
        actor: UserContext,
        base_id: str,
        upload: UploadFile,
        *,
        metadata: str | dict[str, Any] | None = None,
        access_scope: str | None = None,
    ) -> DocumentOut:
        base = await self._require_base_write(actor, base_id)
        if base.status != "active":
            raise ConflictError("Cannot upload documents to a disabled knowledge base")
        filename = Path(upload.filename or "").name
        if not filename:
            raise ValidationError("Uploaded file must have a filename")
        content_type = upload.content_type or mimetypes.guess_type(filename)[0] or ""
        kind = detect_kind(filename, content_type)
        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        if upload.size is not None and upload.size > max_bytes:
            raise DocumentTooLargeError(
                f"File exceeds the {self.settings.max_upload_mb} MB upload limit"
            )
        data = await upload.read()
        if not data:
            raise ValidationError("Uploaded file is empty")
        if len(data) > max_bytes:
            raise DocumentTooLargeError(
                f"File exceeds the {self.settings.max_upload_mb} MB upload limit"
            )
        scope = access_scope or base.access_scope
        if scope not in _ACCESS_SCOPES:
            raise ValidationError("access_scope must be public, department or private")
        document_metadata = _parse_metadata(metadata)
        document_id = new_id()
        suffix = PurePosixPath(filename).suffix.lower() or f".{kind}"
        storage_key = f"knowledge/{document_id}/1{suffix}"
        await self.storage.put(storage_key, data, content_type)
        document = Document(
            id=document_id,
            knowledge_base_id=base.id,
            owner_id=actor.user_id,
            department_id=base.department_id or actor.department_id,
            tenant_id=actor.tenant_id or base.tenant_id,
            filename=filename,
            content_type=content_type,
            size=len(data),
            storage_key=storage_key,
            status="pending",
            version=1,
            error="",
            doc_metadata=document_metadata,
            access_scope=scope,
        )
        version = DocumentVersion(
            id=new_id(),
            document_id=document_id,
            version=1,
            storage_key=storage_key,
            size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
            created_by=actor.user_id,
        )
        self.db.add_all([document, version])
        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            await self._delete_storage(storage_key)
            raise
        self._schedule_processing(document_id, 1)
        return self._document_out(document)

    async def list_documents(
        self, actor: UserContext, base_id: str
    ) -> list[DocumentOut]:
        base = await self._require_base_read(actor, base_id)
        documents = await self.repo.list_documents(base.id)
        result: list[DocumentOut] = []
        for document in documents:
            if not await self._document_readable(actor, document, base, check_base=False):
                continue
            chunk_count = await self.repo.count_chunks(document.id)
            result.append(self._document_out(document, chunk_count=chunk_count))
        return result

    async def get_document(self, actor: UserContext, document_id: str) -> DocumentOut:
        document, _ = await self._require_document_read(actor, document_id)
        chunk_count = await self.repo.count_chunks(document.id)
        return self._document_out(document, chunk_count=chunk_count)

    async def delete_document(self, actor: UserContext, document_id: str) -> None:
        document, _ = await self._require_document_write(actor, document_id)
        versions = await self.repo.list_versions(document.id)
        storage_keys = {document.storage_key}
        storage_keys.update(version.storage_key for version in versions)
        await self.repo.delete_chunks(document.id)
        await self.db.execute(
            delete(DocumentVersion).where(DocumentVersion.document_id == document.id)
        )
        await self.db.delete(document)
        await self.db.commit()
        for key in storage_keys:
            await self._delete_storage(key)

    async def reprocess_document(self, actor: UserContext, document_id: str) -> DocumentOut:
        document, _ = await self._require_document_write(actor, document_id)
        async with _document_lock(document.id):
            try:
                data = await self.storage.get(document.storage_key)
            except FileNotFoundError as exc:
                raise KnowledgeError("Stored document file is missing") from exc
            version_number = document.version + 1
            suffix = PurePosixPath(document.filename).suffix.lower() or ".txt"
            storage_key = f"knowledge/{document.id}/{version_number}{suffix}"
            await self.storage.put(storage_key, data, document.content_type)
            await self.repo.delete_chunks(document.id)
            version = DocumentVersion(
                id=new_id(),
                document_id=document.id,
                version=version_number,
                storage_key=storage_key,
                size=len(data),
                checksum=hashlib.sha256(data).hexdigest(),
                created_by=actor.user_id,
            )
            document.version = version_number
            document.storage_key = storage_key
            document.status = "pending"
            document.error = ""
            self.db.add(version)
            try:
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                await self._delete_storage(storage_key)
                raise
            self._schedule_processing(document.id, version_number)
            return self._document_out(document)

    async def list_chunks(self, actor: UserContext, document_id: str) -> list[ChunkOut]:
        document, _ = await self._require_document_read(actor, document_id)
        chunks = await self.repo.list_chunks(document.id)
        return [self._chunk_out(chunk) for chunk in chunks]

    # ------------------------------------------------------------------ processing

    def _schedule_processing(self, document_id: str, version: int) -> None:
        task = asyncio.create_task(_run_processing(document_id, version))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def process_document(
        self, document_id: str, expected_version: int | None = None
    ) -> str:
        """Parse, chunk and embed a document, returning the final status."""
        async with _document_lock(document_id):
            document = await self.repo.get_document(document_id)
            if document is None or document.status == "deleted":
                return "missing"
            if expected_version is not None and document.version != expected_version:
                return "stale"
            document.status = "processing"
            document.error = ""
            await self.db.commit()
            return await self._process_locked(document_id)

    async def _process_locked(self, document_id: str) -> str:
        document = await self.repo.get_document(document_id)
        if document is None:
            return "missing"
        try:
            data = await self.storage.get(document.storage_key)
            base = await self.repo.get_base(document.knowledge_base_id)
            parsed = parse_document(document.filename, data, document.content_type)
            drafts = chunk_sections(
                [(section.text, section.metadata) for section in parsed.sections],
                chunk_size=base.chunk_size if base else self.settings.rag_chunk_size,
                chunk_overlap=base.chunk_overlap if base else self.settings.rag_chunk_overlap,
                max_chunks=self.settings.knowledge_max_chunks,
            )
            if not drafts:
                raise DocumentParseError("Document contains no extractable text")
            vectors = await self.embeddings.embed([draft.content for draft in drafts])
            if len(vectors) != len(drafts):
                raise DocumentParseError("Embedding provider returned an unexpected vector count")
            await self.repo.delete_chunks(document.id)
            for draft, vector in zip(drafts, vectors, strict=True):
                chunk = DocumentChunk(
                    id=new_id(),
                    document_id=document.id,
                    knowledge_base_id=document.knowledge_base_id,
                    version=document.version,
                    chunk_index=draft.chunk_index,
                    content=draft.content,
                    char_count=draft.char_count,
                    chunk_metadata=draft.metadata,
                )
                self.db.add(chunk)
                self.db.add(
                    EmbeddingRecord(
                        id=new_id(),
                        chunk_id=chunk.id,
                        model=self.embeddings.name,
                        dim=len(vector),
                        vector=[float(value) for value in vector],
                    )
                )
            document.doc_metadata = {
                **(document.doc_metadata or {}),
                **parsed.metadata,
                "chunk_count": len(drafts),
            }
            if len(drafts) >= self.settings.knowledge_max_chunks:
                document.doc_metadata["truncated"] = True
            document.status = "ready"
            document.error = ""
            await self.db.commit()
            return "ready"
        except Exception as exc:  # noqa: BLE001 - failures are recorded on the document
            await self.db.rollback()
            fresh = await self.repo.get_document(document_id)
            if fresh is not None:
                fresh.status = "failed"
                fresh.error = str(exc)[:2000]
                await self.db.commit()
            logger.warning(
                "knowledge document processing failed",
                extra={"document_id": document_id, "error": str(exc)},
            )
            return "failed"

    async def wait_for_document(self, document_id: str, timeout: float = 30) -> DocumentOut:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            async with get_session_factory()() as session:
                repo = KnowledgeRepository(session)
                document = await repo.get_document(document_id)
                if document is None or document.status == "deleted":
                    raise NotFoundError("Document not found")
                status = document.status
                chunk_count = await repo.count_chunks(document.id)
                result = self._document_out(document, chunk_count=chunk_count)
            if status in _TERMINAL_STATUSES:
                return result
            if loop.time() >= deadline:
                raise KnowledgeError(
                    f"Document {document_id} did not finish processing within {timeout}s"
                )
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------- retrieval

    async def search(
        self,
        *,
        knowledge_base_ids: Sequence[str],
        query: str,
        user: UserContext,
        top_k: int = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[RetrievalChunk]:
        text = (query or "").strip()
        base_ids = list(dict.fromkeys(str(item) for item in knowledge_base_ids if item))
        if not text or not base_ids:
            return []
        bases = await self.repo.list_bases_by_ids(base_ids)
        allowed_bases: list[KnowledgeBase] = []
        for base in bases:
            if base.status != "active":
                continue
            if await self._base_readable(user, base):
                allowed_bases.append(base)
        if not allowed_bases:
            return []
        allowed_document_ids: list[str] = []
        for base in allowed_bases:
            for document in await self.repo.list_documents(base.id):
                if document.status != "ready":
                    continue
                if await self._document_readable(user, document, base, check_base=False):
                    allowed_document_ids.append(document.id)
        if not allowed_document_ids:
            return []
        query_vector = (await self.embeddings.embed([text]))[0]
        rows = await self.repo.list_retrieval_rows(
            knowledge_base_ids=[base.id for base in allowed_bases],
            document_ids=allowed_document_ids,
            embedding_model=self.embeddings.name,
            embedding_dim=len(query_vector),
        )
        candidates = [
            RetrievalCandidate(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_title=document.filename,
                knowledge_base_id=chunk.knowledge_base_id,
                content=chunk.content,
                vector=embedding.vector or [],
                metadata=chunk.chunk_metadata or {},
                document_metadata=document.doc_metadata or {},
                document_version=document.version,
            )
            for chunk, embedding, document in rows
        ]
        return await self.store.search(
            query=text,
            query_vector=query_vector,
            candidates=candidates,
            top_k=top_k,
            metadata_filter=metadata_filter,
        )

    async def validate_bindings(
        self, knowledge_base_ids: Sequence[str], actor: UserContext
    ) -> list[KnowledgeBaseOut]:
        base_ids = list(dict.fromkeys(str(item) for item in knowledge_base_ids if item))
        if not base_ids:
            return []
        bases = await self.repo.list_bases_by_ids(base_ids)
        by_id = {base.id: base for base in bases}
        missing = [base_id for base_id in base_ids if base_id not in by_id]
        disabled = [base.id for base in bases if base.status != "active"]
        denied: list[str] = []
        for base in bases:
            if base.status != "active":
                continue
            if not await self._base_readable(actor, base):
                denied.append(base.id)
        if missing or disabled or denied:
            details: dict[str, Any] = {
                "missing": missing,
                "disabled": disabled,
                "denied": denied,
            }
            parts = []
            if missing:
                parts.append(f"unknown: {', '.join(missing)}")
            if disabled:
                parts.append(f"disabled: {', '.join(disabled)}")
            if denied:
                parts.append(f"access denied: {', '.join(denied)}")
            raise ValidationError(
                "Invalid knowledge base bindings (" + "; ".join(parts) + ")",
                details=details,
            )
        return [await self._base_out(by_id[base_id]) for base_id in base_ids]


async def _run_processing(document_id: str, version: int) -> None:
    async with get_session_factory()() as session:
        await KnowledgeService(session).process_document(document_id, version)
