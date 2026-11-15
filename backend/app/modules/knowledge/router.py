from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.knowledge.schemas import (
    ChunkOut,
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
    SearchRequest,
    SearchResponse,
)
from app.modules.knowledge.service import KnowledgeService

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(
    actor: UserContext = Depends(require_permission("knowledge:read")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).list_bases(actor)


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    actor: UserContext = Depends(require_permission("knowledge:write")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).create_base(actor, payload)


@router.get("/knowledge-bases/{base_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    base_id: str,
    actor: UserContext = Depends(require_permission("knowledge:read")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).get_base(actor, base_id)


@router.patch("/knowledge-bases/{base_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    base_id: str,
    payload: KnowledgeBaseUpdate,
    actor: UserContext = Depends(require_permission("knowledge:write")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).update_base(actor, base_id, payload)


@router.delete("/knowledge-bases/{base_id}", status_code=204)
async def delete_knowledge_base(
    base_id: str,
    actor: UserContext = Depends(require_permission("knowledge:write")),
    db: AsyncSession = Depends(get_db),
):
    await KnowledgeService(db).delete_base(actor, base_id)


@router.post(
    "/knowledge-bases/{base_id}/documents",
    response_model=DocumentOut,
    status_code=201,
)
async def upload_document(
    base_id: str,
    file: UploadFile = File(...),
    metadata: str | None = Form(default=None),
    access_scope: str | None = Form(default=None),
    actor: UserContext = Depends(require_permission("knowledge:write")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).upload_document(
        actor, base_id, file, metadata=metadata, access_scope=access_scope
    )


@router.get("/knowledge-bases/{base_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    base_id: str,
    actor: UserContext = Depends(require_permission("knowledge:read")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).list_documents(actor, base_id)


@router.post("/knowledge-bases/{base_id}/search", response_model=SearchResponse)
async def search_knowledge_base(
    base_id: str,
    payload: SearchRequest,
    actor: UserContext = Depends(require_permission("knowledge:read")),
    db: AsyncSession = Depends(get_db),
):
    service = KnowledgeService(db)
    await service.get_base(actor, base_id)
    results = await service.search(
        knowledge_base_ids=[base_id],
        query=payload.query,
        user=actor,
        top_k=payload.top_k,
        metadata_filter=payload.metadata_filter,
    )
    return SearchResponse(
        query=payload.query,
        knowledge_base_ids=[base_id],
        results=[hit.model_dump() for hit in results],
    )


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: str,
    actor: UserContext = Depends(require_permission("knowledge:read")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).get_document(actor, document_id)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    actor: UserContext = Depends(require_permission("knowledge:write")),
    db: AsyncSession = Depends(get_db),
):
    await KnowledgeService(db).delete_document(actor, document_id)


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_document_chunks(
    document_id: str,
    actor: UserContext = Depends(require_permission("knowledge:read")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).list_chunks(actor, document_id)


@router.post("/documents/{document_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(
    document_id: str,
    actor: UserContext = Depends(require_permission("knowledge:write")),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db).reprocess_document(actor, document_id)
