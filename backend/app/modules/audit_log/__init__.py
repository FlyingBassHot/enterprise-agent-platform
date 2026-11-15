"""Append-only audit log module.

Public entry points: ``app.modules.audit_log.service.AuditService`` and
``app.modules.audit_log.router.router``. ORM models live in ``models.py`` and
must be imported (directly or via the database registry) before ``create_all``.
"""
