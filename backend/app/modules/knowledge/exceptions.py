from __future__ import annotations

from app.core.exceptions import AppError


class KnowledgeError(AppError):
    """Base class for knowledge base failures."""

    status_code = 400
    code = "knowledge_error"


class UnsupportedDocumentTypeError(KnowledgeError):
    """The uploaded document type is not supported by any parser."""

    status_code = 415
    code = "unsupported_document_type"


class DocumentTooLargeError(KnowledgeError):
    """The uploaded document exceeds the configured size limit."""

    status_code = 413
    code = "document_too_large"


class DocumentParseError(KnowledgeError):
    """A document could not be parsed into text."""

    status_code = 422
    code = "document_parse_error"


class KnowledgeDisabledError(KnowledgeError):
    """A knowledge base is disabled and cannot be used for retrieval."""

    status_code = 409
    code = "knowledge_base_disabled"
