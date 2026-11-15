from __future__ import annotations

import asyncio
import io
import json
import math
import time
from collections.abc import Iterable
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.contracts import UserContext
from app.core.exceptions import NotFoundError
from app.core.exceptions import ValidationError as AppValidationError
from app.modules.knowledge.chunking import chunk_sections, chunk_text
from app.modules.knowledge.embeddings import HashEmbeddingProvider
from app.modules.knowledge.exceptions import UnsupportedDocumentTypeError
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    EmbeddingRecord,
    KnowledgeBase,
)
from app.modules.knowledge.parsers import parse_document
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.vector_store import LocalVectorStore, RetrievalCandidate
from app.modules.permission.service import PermissionService

KNOWLEDGE = "/api/v1/knowledge-bases"
DOCUMENTS = "/api/v1/documents"
USERS = "/api/v1/users"
GRANTS = "/api/v1/permissions/grant"
PASSWORD = "test-pass-123"


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


async def create_base(
    client: AsyncClient, headers: dict[str, str], **overrides: object
) -> dict:
    payload = {
        "name": unique("kb"),
        "description": "test base",
        "access_scope": "public",
        "chunk_size": 200,
        "chunk_overlap": 20,
    }
    payload.update(overrides)
    response = await client.post(KNOWLEDGE, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def create_user(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    role_names: Iterable[str] = ("viewer",),
    department_id: str | None = None,
) -> tuple[dict, dict[str, str]]:
    username = unique("user")
    response = await client.post(
        USERS,
        headers=admin,
        json={
            "username": username,
            "password": PASSWORD,
            "display_name": username,
            "role_names": list(role_names),
            "department_id": department_id,
        },
    )
    assert response.status_code == 201, response.text
    login = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return response.json(), {"Authorization": f"Bearer {login.json()['access_token']}"}


async def grant(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    resource_type: str,
    resource_id: str,
    subject_id: str,
    actions: list[str] | None = None,
) -> None:
    response = await client.post(
        GRANTS,
        headers=admin,
        json={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "subject_type": "user",
            "subject_id": subject_id,
            "actions": actions or ["read"],
        },
    )
    assert response.status_code == 201, response.text


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    base_id: str,
    filename: str,
    content: bytes,
    *,
    content_type: str = "text/plain",
    metadata: dict | None = None,
    access_scope: str | None = None,
) -> dict:
    data = {}
    if metadata is not None:
        data["metadata"] = json.dumps(metadata)
    if access_scope is not None:
        data["access_scope"] = access_scope
    response = await client.post(
        f"{KNOWLEDGE}/{base_id}/documents",
        headers=headers,
        files={"file": (filename, content, content_type)},
        data=data,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def wait_for_status(
    client: AsyncClient,
    headers: dict[str, str],
    document_id: str,
    *,
    expected: tuple[str, ...] = ("ready",),
    timeout: float = 15.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = await client.get(f"{DOCUMENTS}/{document_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in expected:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"document {document_id} ended in status {last.get('status')}: {last}")


async def search(
    client: AsyncClient,
    headers: dict[str, str],
    base_id: str,
    query: str,
    **payload: object,
) -> list[dict]:
    body = {"query": query, **payload}
    response = await client.post(f"{KNOWLEDGE}/{base_id}/search", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()["results"]


# ------------------------------------------------------------------ unit: embedding


async def test_hash_embedding_is_deterministic_and_normalized() -> None:
    provider = HashEmbeddingProvider(dimension=384)
    first, second, different = await provider.embed(
        ["quarterly revenue audit", "quarterly revenue audit", "human resources leave policy"]
    )
    assert len(first) == 384
    assert first == second
    assert first != different
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0, rel_tol=1e-6)
    assert all(value <= 1.0 for value in first)
    assert provider.name == "hash-384"


async def test_create_embedding_provider_defaults_to_hash() -> None:
    from app.modules.knowledge.embeddings import create_embedding_provider

    provider = create_embedding_provider(get_settings())
    assert isinstance(provider, HashEmbeddingProvider)
    assert provider.dimension == get_settings().default_embedding_dim


# ------------------------------------------------------------------ unit: chunking


def test_chunking_prefers_paragraphs_and_tracks_metadata() -> None:
    text = "\n\n".join(
        [
            "First paragraph about audit planning and controls.",
            "Second paragraph about quarterly revenue reconciliation.",
            "Third paragraph about vendor payments and approvals.",
        ]
    )
    drafts = chunk_text(
        text,
        chunk_size=80,
        chunk_overlap=10,
        metadata={"page": 3},
        start_index=0,
        max_chunks=10,
    )
    assert drafts
    assert [draft.chunk_index for draft in drafts] == list(range(len(drafts)))
    assert all(draft.char_count == len(draft.content) for draft in drafts)
    assert all(draft.metadata["page"] == 3 for draft in drafts)
    assert all(draft.char_count <= 80 for draft in drafts)
    assert any("Second paragraph" in draft.content for draft in drafts)


def test_chunking_caps_and_keeps_global_index() -> None:
    sections = [
        ("alpha " * 100, {"page": 1}),
        ("beta " * 100, {"page": 2}),
    ]
    drafts = chunk_sections(sections, chunk_size=60, chunk_overlap=5, max_chunks=3)
    assert len(drafts) == 3
    assert [draft.chunk_index for draft in drafts] == [0, 1, 2]


# -------------------------------------------------------------------- unit: parsers


def test_parse_text_markdown_and_csv() -> None:
    markdown = b"# Finance Policy\n\nExpense policy applies.\n\n## Travel\n\nFlights are capped."
    parsed = parse_document("policy.md", markdown, "text/markdown")
    assert parsed.metadata["parser"] == "md"
    assert [section.metadata.get("section") for section in parsed.sections] == [
        "Finance Policy",
        "Travel",
    ]

    csv_data = b"name,amount\nalpha,10\nbeta,20\n"
    parsed_csv = parse_document("ledger.csv", csv_data, "text/csv")
    assert parsed_csv.metadata["parser"] == "csv"
    assert "alpha | 10" in parsed_csv.text

    parsed_txt = parse_document("notes.txt", b"plain text findings")
    assert parsed_txt.text == "plain text findings"
    assert parsed_txt.sections[0].metadata == {}


def test_parse_docx_and_xlsx() -> None:
    import docx
    from openpyxl import Workbook

    document = docx.Document()
    document.add_heading("Finance Policy", level=1)
    document.add_paragraph("Expense reimbursement requires receipts.")
    buffer = io.BytesIO()
    document.save(buffer)
    parsed_docx = parse_document("policy.docx", buffer.getvalue())
    assert parsed_docx.metadata["parser"] == "docx"
    assert any(section.metadata.get("section") == "Finance Policy" for section in parsed_docx.sections)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ledger"
    sheet.append(["account", "amount"])
    sheet.append(["cash", 100])
    xlsx_buffer = io.BytesIO()
    workbook.save(xlsx_buffer)
    parsed_xlsx = parse_document("ledger.xlsx", xlsx_buffer.getvalue())
    assert parsed_xlsx.metadata["parser"] == "xlsx"
    assert parsed_xlsx.metadata["sheets"] == ["Ledger"]
    assert "cash | 100" in parsed_xlsx.text
    assert parsed_xlsx.sections[0].metadata == {"sheet": "Ledger"}


def test_parse_pdf_blank_page_and_unsupported_type() -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    parsed = parse_document("blank.pdf", buffer.getvalue(), "application/pdf")
    assert parsed.metadata["parser"] == "pdf"
    assert parsed.metadata["pages"] == 1
    assert parsed.sections[0].metadata == {"page": 1}

    with pytest.raises(UnsupportedDocumentTypeError):
        parse_document("legacy.doc", b"binary")


# --------------------------------------------------------------- unit: vector store


async def test_local_vector_store_hybrid_keyword_and_metadata() -> None:
    candidates = [
        RetrievalCandidate(
            chunk_id="chunk-a",
            document_id="doc-a",
            document_title="revenue.txt",
            knowledge_base_id="kb-1",
            content="quarterly revenue audit report",
            vector=[1.0, 0.0, 0.0],
            metadata={"page": 2, "chunk_index": 0},
            document_metadata={"category": "finance"},
            document_version=3,
        ),
        RetrievalCandidate(
            chunk_id="chunk-b",
            document_id="doc-b",
            document_title="unrelated.txt",
            knowledge_base_id="kb-1",
            content="completely different words",
            vector=[1.0, 0.0, 0.0],
            metadata={},
            document_metadata={"category": "ops"},
            document_version=1,
        ),
    ]
    store = LocalVectorStore()
    results = await store.search(
        query="quarterly revenue", query_vector=[1.0, 0.0, 0.0], candidates=candidates, top_k=5
    )
    assert [hit.chunk_id for hit in results] == ["chunk-a", "chunk-b"]
    assert results[0].score == pytest.approx(1.0)
    assert results[1].score == pytest.approx(0.7)
    assert results[0].metadata["page"] == 2
    assert results[0].metadata["document_version"] == 3
    assert results[0].document_title == "revenue.txt"

    keyword_store = LocalVectorStore(mode="keyword")
    keyword_results = await keyword_store.search(
        query="quarterly revenue",
        query_vector=[1.0, 0.0, 0.0],
        candidates=candidates,
        top_k=5,
    )
    assert [hit.chunk_id for hit in keyword_results] == ["chunk-a"]

    filtered = await store.search(
        query="quarterly revenue",
        query_vector=[1.0, 0.0, 0.0],
        candidates=candidates,
        top_k=5,
        metadata_filter={"category": "ops"},
    )
    assert [hit.chunk_id for hit in filtered] == ["chunk-b"]

    by_document = await store.search(
        query="quarterly revenue",
        query_vector=[1.0, 0.0, 0.0],
        candidates=candidates,
        top_k=5,
        metadata_filter={"document_id": "doc-a"},
    )
    assert [hit.chunk_id for hit in by_document] == ["chunk-a"]


# ------------------------------------------------------------------- API: base CRUD


async def test_knowledge_base_crud_and_ownership(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    owner, owner_headers = await create_user(
        client, admin, role_names=("developer",)
    )
    other, other_headers = await create_user(client, admin, role_names=("developer",))
    _viewer, viewer_headers = await create_user(client, admin, role_names=("viewer",))

    base = await create_base(client, owner_headers, access_scope="private")
    assert base["owner_id"] == owner["id"]
    assert base["status"] == "active"
    assert base["document_count"] == 0

    listed = await client.get(KNOWLEDGE, headers=owner_headers)
    assert listed.status_code == 200
    assert base["id"] in {item["id"] for item in listed.json()}

    viewer_listed = await client.get(KNOWLEDGE, headers=viewer_headers)
    assert viewer_listed.status_code == 200
    assert base["id"] not in {item["id"] for item in viewer_listed.json()}

    denied = await client.get(f"{KNOWLEDGE}/{base['id']}", headers=viewer_headers)
    assert denied.status_code == 403

    other_update = await client.patch(
        f"{KNOWLEDGE}/{base['id']}", headers=other_headers, json={"description": "hijack"}
    )
    assert other_update.status_code == 403

    updated = await client.patch(
        f"{KNOWLEDGE}/{base['id']}",
        headers=owner_headers,
        json={"description": "updated", "access_scope": "public"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "updated"
    assert updated.json()["access_scope"] == "public"

    viewer_get = await client.get(f"{KNOWLEDGE}/{base['id']}", headers=viewer_headers)
    assert viewer_get.status_code == 200

    deleted = await client.delete(f"{KNOWLEDGE}/{base['id']}", headers=owner_headers)
    assert deleted.status_code == 204
    missing = await client.get(f"{KNOWLEDGE}/{base['id']}", headers=owner_headers)
    assert missing.status_code == 404


async def test_duplicate_base_name_conflicts(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    base = await create_base(client, admin)
    response = await client.post(
        KNOWLEDGE,
        headers=admin,
        json={"name": base["name"], "description": "", "access_scope": "public"},
    )
    assert response.status_code == 409


# ------------------------------------------------------- API: ingestion and search


async def test_upload_chunks_embeddings_and_search(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    base = await create_base(client, admin, chunk_size=120, chunk_overlap=20)
    content = (
        b"Quarterly revenue audit\n\n"
        b"The quarterly revenue audit covers enterprise accounts and revenue recognition policy.\n\n"
        b"Internal controls\n\n"
        b"Controls are reviewed quarterly by the audit team before publication."
    )
    document = await upload(
        client, admin, base["id"], "revenue.txt", content, metadata={"category": "finance"}
    )
    assert document["status"] in {"pending", "processing", "ready"}
    ready = await wait_for_status(client, admin, document["id"])
    assert ready["status"] == "ready"
    assert ready["version"] == 1
    assert ready["metadata"]["parser"] == "txt"
    assert ready["metadata"]["category"] == "finance"
    assert ready["chunk_count"] >= 1

    chunks = await client.get(f"{DOCUMENTS}/{document['id']}/chunks", headers=admin)
    assert chunks.status_code == 200, chunks.text
    chunk_rows = chunks.json()
    assert len(chunk_rows) == ready["chunk_count"]
    assert [row["chunk_index"] for row in chunk_rows] == list(range(len(chunk_rows)))
    assert all(row["char_count"] == len(row["content"]) for row in chunk_rows)

    stored_chunks = await db.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_id == document["id"]
        )
    )
    stored_embeddings = await db.scalar(
        select(func.count(EmbeddingRecord.id)).join(
            DocumentChunk, DocumentChunk.id == EmbeddingRecord.chunk_id
        ).where(DocumentChunk.document_id == document["id"])
    )
    assert stored_chunks == len(chunk_rows)
    assert stored_embeddings == len(chunk_rows)

    results = await search(client, admin, base["id"], "quarterly revenue audit", top_k=5)
    assert results
    assert results[0]["document_id"] == document["id"]
    assert results[0]["document_title"] == "revenue.txt"
    assert "quarterly revenue audit" in results[0]["content"].lower()
    assert results[0]["metadata"]["document_version"] == 1
    assert results[0]["score"] > 0

    base_after = await client.get(f"{KNOWLEDGE}/{base['id']}", headers=admin)
    assert base_after.json()["document_count"] == 1


async def test_markdown_section_metadata_and_filters(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    base = await create_base(client, admin)
    finance = await upload(
        client,
        admin,
        base["id"],
        "finance.md",
        b"# Finance Policy\n\nExpense reimbursement policy covers travel costs.",
        content_type="text/markdown",
        metadata={"category": "finance", "team": "audit"},
    )
    hr = await upload(
        client,
        admin,
        base["id"],
        "hr.md",
        b"# HR Policy\n\nLeave policy covers vacation days for employees.",
        content_type="text/markdown",
        metadata={"category": "hr"},
    )
    await wait_for_status(client, admin, finance["id"])
    await wait_for_status(client, admin, hr["id"])

    all_hits = await search(client, admin, base["id"], "policy", top_k=10)
    assert {hit["document_id"] for hit in all_hits} == {finance["id"], hr["id"]}

    finance_hits = await search(
        client, admin, base["id"], "expense policy", metadata_filter={"category": "finance"}
    )
    assert finance_hits
    assert {hit["document_id"] for hit in finance_hits} == {finance["id"]}
    assert finance_hits[0]["metadata"]["section"] == "Finance Policy"

    document_hits = await search(
        client, admin, base["id"], "leave policy", metadata_filter={"document_id": hr["id"]}
    )
    assert {hit["document_id"] for hit in document_hits} == {hr["id"]}

    empty = await search(
        client, admin, base["id"], "policy", metadata_filter={"category": "missing"}
    )
    assert empty == []


async def test_upload_rejects_unsupported_and_empty(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    base = await create_base(client, admin)
    unsupported = await client.post(
        f"{KNOWLEDGE}/{base['id']}/documents",
        headers=admin,
        files={"file": ("legacy.doc", b"binary", "application/msword")},
    )
    assert unsupported.status_code == 415

    empty = await client.post(
        f"{KNOWLEDGE}/{base['id']}/documents",
        headers=admin,
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert empty.status_code == 422


# ------------------------------------------------------------- API: access control


async def test_private_department_and_explicit_grants(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    finance_base = await create_base(
        client,
        admin,
        access_scope="department",
        department_id="finance",
    )
    finance_doc = await upload(
        client,
        admin,
        finance_base["id"],
        "finance.txt",
        b"Budget approval policy for finance department expenses.",
    )
    await wait_for_status(client, admin, finance_doc["id"])

    _, finance_headers = await create_user(client, admin, department_id="finance")
    hr_user, hr_headers = await create_user(client, admin, department_id="hr")

    assert (await client.get(f"{KNOWLEDGE}/{finance_base['id']}", headers=finance_headers)).status_code == 200
    assert (await client.get(f"{KNOWLEDGE}/{finance_base['id']}", headers=hr_headers)).status_code == 403
    assert (await client.get(f"{DOCUMENTS}/{finance_doc['id']}", headers=hr_headers)).status_code == 403
    department_denied = await client.post(
        f"{KNOWLEDGE}/{finance_base['id']}/search",
        headers=hr_headers,
        json={"query": "budget"},
    )
    assert department_denied.status_code == 403
    assert await search(client, finance_headers, finance_base["id"], "budget")

    private_base = await create_base(client, admin, access_scope="private")
    private_doc = await upload(
        client,
        admin,
        private_base["id"],
        "secret.txt",
        b"Secret merger memo and acquisition details.",
    )
    await wait_for_status(client, admin, private_doc["id"])

    assert (await client.get(f"{KNOWLEDGE}/{private_base['id']}", headers=hr_headers)).status_code == 403
    search_denied = await client.post(
        f"{KNOWLEDGE}/{private_base['id']}/search",
        headers=hr_headers,
        json={"query": "merger"},
    )
    assert search_denied.status_code == 403
    assert (await client.get(f"{DOCUMENTS}/{private_doc['id']}", headers=hr_headers)).status_code == 403

    await grant(
        client,
        admin,
        resource_type="knowledge_base",
        resource_id=private_base["id"],
        subject_id=hr_user["id"],
    )
    assert (await client.get(f"{KNOWLEDGE}/{private_base['id']}", headers=hr_headers)).status_code == 200
    documents_after_grant = await client.get(
        f"{KNOWLEDGE}/{private_base['id']}/documents", headers=hr_headers
    )
    assert documents_after_grant.json() == []
    assert await search(client, hr_headers, private_base["id"], "merger") == []

    await grant(
        client,
        admin,
        resource_type="document",
        resource_id=private_doc["id"],
        subject_id=hr_user["id"],
    )
    assert (await client.get(f"{DOCUMENTS}/{private_doc['id']}", headers=hr_headers)).status_code == 200
    grants_hits = await search(client, hr_headers, private_base["id"], "merger memo")
    assert grants_hits
    assert grants_hits[0]["document_id"] == private_doc["id"]

    public_base = await create_base(client, admin, access_scope="public")
    assert (await client.get(f"{KNOWLEDGE}/{public_base['id']}", headers=finance_headers)).status_code == 200


# --------------------------------------------------------- service: binding checks


async def test_validate_bindings(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    owner, owner_headers = await create_user(client, admin, role_names=("developer",))
    _, other_headers = await create_user(client, admin, role_names=("developer",))

    public_base = await create_base(client, owner_headers, access_scope="public")
    private_base = await create_base(client, owner_headers, access_scope="private")
    disabled_base = await create_base(
        client, owner_headers, access_scope="public", status="disabled"
    )

    owner_context = await PermissionService(db).load_context(owner["id"])
    assert isinstance(owner_context, UserContext)
    service = KnowledgeService(db)
    validated = await service.validate_bindings([public_base["id"]], owner_context)
    assert [item.id for item in validated] == [public_base["id"]]

    with pytest.raises(AppValidationError) as missing:
        await service.validate_bindings(["does-not-exist"], owner_context)
    assert "unknown" in missing.value.message

    with pytest.raises(AppValidationError) as disabled:
        await service.validate_bindings([disabled_base["id"]], owner_context)
    assert "disabled" in disabled.value.message

    # Admin owns nothing here; private bases are not readable by a different developer.
    other_user = await client.get("/api/v1/auth/me", headers=other_headers)
    other_context = await PermissionService(db).load_context(other_user.json()["id"])
    with pytest.raises(AppValidationError) as denied:
        await service.validate_bindings([private_base["id"]], other_context)
    assert "access denied" in denied.value.message
    assert private_base["id"] in denied.value.details["denied"]


# -------------------------------------------------------------- reprocess and delete


async def test_reprocess_increments_version_and_replaces_chunks(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    base = await create_base(client, admin)
    document = await upload(
        client, admin, base["id"], "policy.txt", b"First version of the travel policy."
    )
    first = await wait_for_status(client, admin, document["id"])
    assert first["version"] == 1

    reprocessed = await client.post(
        f"{DOCUMENTS}/{document['id']}/reprocess", headers=admin
    )
    assert reprocessed.status_code == 200, reprocessed.text
    assert reprocessed.json()["version"] == 2
    assert reprocessed.json()["status"] in {"pending", "processing", "ready"}

    second = await wait_for_status(client, admin, document["id"])
    assert second["version"] == 2
    assert second["chunk_count"] >= 1

    version_rows = await db.scalar(
        select(func.count(DocumentVersion.id)).where(
            DocumentVersion.document_id == document["id"]
        )
    )
    assert version_rows == 2

    chunk_versions = (
        await db.scalars(
            select(DocumentChunk.version).where(DocumentChunk.document_id == document["id"])
        )
    ).all()
    assert chunk_versions and set(chunk_versions) == {2}

    hits = await search(client, admin, base["id"], "travel policy")
    assert hits
    assert hits[0]["metadata"]["document_version"] == 2


async def test_delete_document_removes_search_results(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    base = await create_base(client, admin)
    document = await upload(
        client, admin, base["id"], "delete.txt", b"Unique deletion sentinel content about penguins."
    )
    await wait_for_status(client, admin, document["id"])
    assert await search(client, admin, base["id"], "penguins")

    deleted = await client.delete(f"{DOCUMENTS}/{document['id']}", headers=admin)
    assert deleted.status_code == 204

    assert (await client.get(f"{DOCUMENTS}/{document['id']}", headers=admin)).status_code == 404
    assert await search(client, admin, base["id"], "penguins") == []

    remaining_chunks = await db.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_id == document["id"]
        )
    )
    remaining_embeddings = await db.scalar(
        select(func.count(EmbeddingRecord.id)).join(
            DocumentChunk, DocumentChunk.id == EmbeddingRecord.chunk_id
        ).where(DocumentChunk.document_id == document["id"])
    )
    assert remaining_chunks == 0
    assert remaining_embeddings == 0

    base_row = await db.get(KnowledgeBase, base["id"])
    assert base_row is not None
    documents = await db.scalar(
        select(func.count(Document.id)).where(Document.knowledge_base_id == base["id"])
    )
    assert documents == 0


async def test_delete_base_cascades_documents_and_chunks(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    base = await create_base(client, admin)
    document = await upload(
        client, admin, base["id"], "cascade.txt", b"Cascade removal content."
    )
    await wait_for_status(client, admin, document["id"])

    assert (await client.delete(f"{KNOWLEDGE}/{base['id']}", headers=admin)).status_code == 204
    assert await db.scalar(
        select(func.count(Document.id)).where(Document.knowledge_base_id == base["id"])
    ) == 0
    assert await db.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.knowledge_base_id == base["id"]
        )
    ) == 0


async def test_wait_for_document_service(db) -> None:
    with pytest.raises(NotFoundError):
        await KnowledgeService(db).wait_for_document("missing", timeout=0.1)


async def test_stale_processing_version_is_ignored(client, admin, db) -> None:
    from app.modules.knowledge.service import KnowledgeService

    base = await client.post(
        "/api/v1/knowledge-bases",
        headers=admin,
        json={"name": "stale-version-kb", "access_scope": "public"},
    )
    assert base.status_code == 201, base.text
    upload = await client.post(
        f"/api/v1/knowledge-bases/{base.json()['id']}/documents",
        headers=admin,
        files={"file": ("stale.txt", b"stale version test", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]
    service = KnowledgeService(db)
    await service.wait_for_document(document_id)
    status = await service.process_document(document_id, expected_version=0)
    assert status == "stale"
