from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    EmbeddingRecord,
    KnowledgeBase,
)


class KnowledgeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------ knowledge bases

    async def get_base(self, base_id: str) -> KnowledgeBase | None:
        return await self.db.get(KnowledgeBase, base_id)

    async def get_base_by_name(self, name: str) -> KnowledgeBase | None:
        return await self.db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))

    async def list_bases(self) -> list[KnowledgeBase]:
        return list(
            (await self.db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name))).all()
        )

    async def list_bases_by_ids(self, base_ids: Sequence[str]) -> list[KnowledgeBase]:
        if not base_ids:
            return []
        return list(
            (
                await self.db.scalars(
                    select(KnowledgeBase).where(KnowledgeBase.id.in_(list(base_ids)))
                )
            ).all()
        )

    async def count_documents(self, base_id: str) -> int:
        return int(
            await self.db.scalar(
                select(func.count(Document.id)).where(
                    Document.knowledge_base_id == base_id,
                    Document.status != "deleted",
                )
            )
            or 0
        )

    # ------------------------------------------------------------------- documents

    async def get_document(self, document_id: str) -> Document | None:
        return await self.db.get(Document, document_id)

    async def list_documents(self, base_id: str) -> list[Document]:
        return list(
            (
                await self.db.scalars(
                    select(Document)
                    .where(
                        Document.knowledge_base_id == base_id,
                        Document.status != "deleted",
                    )
                    .order_by(Document.created_at.desc(), Document.filename)
                )
            ).all()
        )

    async def list_base_documents(self, base_id: str) -> list[Document]:
        return list(
            (await self.db.scalars(select(Document).where(Document.knowledge_base_id == base_id))).all()
        )

    async def count_chunks(self, document_id: str) -> int:
        return int(
            await self.db.scalar(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_id == document_id
                )
            )
            or 0
        )

    async def count_embeddings(self, document_id: str) -> int:
        chunk_ids = select(DocumentChunk.id).where(DocumentChunk.document_id == document_id)
        return int(
            await self.db.scalar(
                select(func.count(EmbeddingRecord.id)).where(
                    EmbeddingRecord.chunk_id.in_(chunk_ids)
                )
            )
            or 0
        )

    # ---------------------------------------------------------------------- chunks

    async def list_chunks(self, document_id: str) -> list[DocumentChunk]:
        return list(
            (
                await self.db.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).all()
        )

    async def delete_chunks(self, document_id: str) -> None:
        chunk_ids = select(DocumentChunk.id).where(DocumentChunk.document_id == document_id)
        await self.db.execute(
            delete(EmbeddingRecord).where(EmbeddingRecord.chunk_id.in_(chunk_ids))
        )
        await self.db.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        await self.db.flush()

    # -------------------------------------------------------------------- versions

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return list(
            (
                await self.db.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document_id)
                    .order_by(DocumentVersion.version)
                )
            ).all()
        )

    # ------------------------------------------------------------------ retrieval

    async def list_retrieval_rows(
        self,
        *,
        knowledge_base_ids: Sequence[str],
        document_ids: Sequence[str],
        embedding_model: str,
        embedding_dim: int,
    ) -> list[tuple[DocumentChunk, EmbeddingRecord, Document]]:
        if not knowledge_base_ids or not document_ids:
            return []
        statement = (
            select(DocumentChunk, EmbeddingRecord, Document)
            .join(EmbeddingRecord, EmbeddingRecord.chunk_id == DocumentChunk.id)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.knowledge_base_id.in_(list(knowledge_base_ids)),
                DocumentChunk.document_id.in_(list(document_ids)),
                DocumentChunk.version == Document.version,
                Document.status == "ready",
                EmbeddingRecord.model == embedding_model,
                EmbeddingRecord.dim == embedding_dim,
            )
            .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
        )
        result = await self.db.execute(statement)
        return [(row[0], row[1], row[2]) for row in result.all()]
