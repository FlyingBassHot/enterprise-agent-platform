"""Postgraduate notification MCP (stdio).

Safety model:
- dry-run is the default; no external network call happens unless
  ``POSTGRAD_NOTIFICATION_DRY_RUN`` is explicitly one of 0/false/no/off;
- every recipient/group must be allowlisted;
- every send needs an explicit, channel-scoped authorization record;
- every send needs a caller-supplied idempotency key;
- sends are rate limited and persisted before the external call;
- uncertain outcomes are never retried automatically by this server;
- SMTP connection details and WeCom webhooks come only from environment
  variables; tool arguments can never choose a host or URL;
- credentials and webhook URLs are never included in tool responses.

Run with:
    python extensions/postgraduate/mcp/notification.py

Environment:
    POSTGRAD_DATA_DIR                       default: <repo>/extensions/postgraduate/data
    POSTGRAD_NOTIFICATION_DRY_RUN           default 1 (1/true/yes/on, 0/false/no/off)
    POSTGRAD_EMAIL_ALLOWLIST                comma/semicolon separated addresses
    POSTGRAD_WECOM_ALLOWLIST                comma/semicolon separated group names
    POSTGRAD_WECOM_WEBHOOKS                 JSON object: {"group": "https://qyapi.../send?key=..."}
    POSTGRAD_SMTP_HOST / PORT / USER / PASSWORD / FROM
    POSTGRAD_SMTP_SSL / STARTTLS / TIMEOUT
    POSTGRAD_NOTIFICATION_RATE_LIMIT_PER_HOUR  default 20
    POSTGRAD_NOTIFICATION_STALE_SECONDS        default 300
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sqlite3
import ssl
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

SERVER_NAME = "postgrad-notification"
SERVER_VERSION = "0.1.0"
DATA_DIR_ENV = "POSTGRAD_DATA_DIR"
MAX_EMAIL_RECIPIENTS = 10
MAX_SUBJECT_CHARS = 200
MAX_EMAIL_BODY_CHARS = 20_000
MAX_WECOM_BODY_BYTES = 1_800
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL = frozenset({"sent", "dry_run", "failed", "uncertain"})
_PREFLIGHT = frozenset({"unavailable", "blocked", "rate_limited"})


class UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str, **extra: object) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}, **extra}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _data_dir() -> Path:
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent / "data"


def _parse_flag(raw: str | None, default: bool) -> tuple[bool, str | None]:
    if raw is None or not raw.strip():
        return default, None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True, None
    if value in {"0", "false", "no", "off"}:
        return False, None
    return default, "unrecognised boolean environment value; using the safe default"


def _parse_int(raw: str | None, default: int, minimum: int, maximum: int) -> tuple[int, str | None]:
    if raw is None or not raw.strip():
        return default, None
    try:
        value = int(raw.strip())
    except ValueError:
        return default, "invalid integer environment value; using the safe default"
    if not minimum <= value <= maximum:
        return default, "integer environment value out of range; using the safe default"
    return value, None


def _split_list(raw: str) -> list[str]:
    return [part for part in re.split(r"[,;\s]+", raw.strip()) if part]


def _valid_email(value: str) -> bool:
    if len(value) > 254 or value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    return bool(local and len(local) <= 64 and _EMAIL_RE.match(value) and ".." not in domain)


def _valid_webhook(url: str) -> bool:
    """Only the configured WeCom robot endpoint is a valid destination."""

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.hostname != "qyapi.weixin.qq.com":
        return False
    if parts.username or parts.password or parts.fragment:
        return False
    if port not in (None, 443):
        return False
    if parts.path != "/cgi-bin/webhook/send":
        return False
    query = parse_qs(parts.query, keep_blank_values=True)
    return bool(query.get("key") and query["key"][0].strip())


class NotificationConfig:
    """Environment-derived configuration. Never serialize this object."""

    def __init__(
        self,
        *,
        dry_run: bool,
        email_allowlist: set[str],
        wecom_allowlist: set[str],
        wecom_webhooks: dict[str, str],
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        smtp_from: str,
        smtp_ssl: bool,
        smtp_starttls: bool,
        smtp_timeout: int,
        rate_limit_per_hour: int,
        stale_seconds: int,
        warnings: list[str],
    ) -> None:
        self.dry_run = dry_run
        self.email_allowlist = frozenset(email_allowlist)
        self.wecom_allowlist = frozenset(wecom_allowlist)
        self.wecom_webhooks = dict(wecom_webhooks)
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_from = smtp_from
        self.smtp_ssl = smtp_ssl
        self.smtp_starttls = smtp_starttls
        self.smtp_timeout = smtp_timeout
        self.rate_limit_per_hour = rate_limit_per_hour
        self.stale_seconds = stale_seconds
        self.warnings = list(warnings)

    def __repr__(self) -> str:
        return (
            f"NotificationConfig(dry_run={self.dry_run}, "
            f"email_recipients={len(self.email_allowlist)}, "
            f"wecom_groups={len(self.wecom_allowlist)})"
        )

    @classmethod
    def from_env(cls) -> NotificationConfig:
        warnings: list[str] = []
        dry_run, warning = _parse_flag(
            os.environ.get("POSTGRAD_NOTIFICATION_DRY_RUN"), True
        )
        if warning:
            warnings.append(warning)

        email_allowlist = {
            value.strip().lower()
            for value in _split_list(os.environ.get("POSTGRAD_EMAIL_ALLOWLIST", ""))
            if value.strip()
        }
        invalid_emails = sorted(value for value in email_allowlist if not _valid_email(value))
        if invalid_emails:
            warnings.append("some allowlist email addresses are invalid and were ignored")
            email_allowlist.difference_update(invalid_emails)

        webhooks: dict[str, str] = {}
        raw_webhooks = os.environ.get("POSTGRAD_WECOM_WEBHOOKS", "").strip()
        if raw_webhooks:
            try:
                parsed = json.loads(raw_webhooks)
            except ValueError:
                parsed = None
                warnings.append("POSTGRAD_WECOM_WEBHOOKS is not valid JSON; no groups configured")
            if isinstance(parsed, dict):
                for group, url in parsed.items():
                    name = str(group).strip()
                    if not name or not isinstance(url, str) or not _valid_webhook(url.strip()):
                        warnings.append(
                            "an invalid WeCom webhook entry was ignored; "
                            "only the official https qyapi.weixin.qq.com robot endpoint is accepted"
                        )
                        continue
                    webhooks[name] = url.strip()
            elif parsed is not None:
                warnings.append("POSTGRAD_WECOM_WEBHOOKS must be a JSON object")

        raw_wecom_allowlist = os.environ.get("POSTGRAD_WECOM_ALLOWLIST")
        # An absent or explicitly empty allowlist is empty, never "all webhooks".
        # This is deliberately fail-closed even when webhooks are configured.
        wecom_allowlist = (
            {
                value.strip()
                for value in _split_list(raw_wecom_allowlist or "")
                if value.strip()
            }
            if raw_wecom_allowlist is not None
            else set()
        )

        smtp_host = os.environ.get("POSTGRAD_SMTP_HOST", "").strip()
        smtp_from = os.environ.get("POSTGRAD_SMTP_FROM", "").strip()
        if smtp_from and not _valid_email(smtp_from):
            warnings.append("POSTGRAD_SMTP_FROM is not a valid email address")
            smtp_from = ""
        smtp_port, warning = _parse_int(
            os.environ.get("POSTGRAD_SMTP_PORT"), 587, 1, 65535
        )
        if warning:
            warnings.append("invalid POSTGRAD_SMTP_PORT; using 587")
        smtp_timeout, warning = _parse_int(
            os.environ.get("POSTGRAD_SMTP_TIMEOUT"), 15, 1, 120
        )
        if warning:
            warnings.append("invalid POSTGRAD_SMTP_TIMEOUT; using 15")
        smtp_ssl, warning = _parse_flag(
            os.environ.get("POSTGRAD_SMTP_SSL"), smtp_port == 465
        )
        if warning:
            warnings.append("invalid POSTGRAD_SMTP_SSL; using the safe default")
        smtp_starttls, warning = _parse_flag(
            os.environ.get("POSTGRAD_SMTP_STARTTLS"), not smtp_ssl
        )
        if warning:
            warnings.append("invalid POSTGRAD_SMTP_STARTTLS; using the safe default")
        rate_limit, warning = _parse_int(
            os.environ.get("POSTGRAD_NOTIFICATION_RATE_LIMIT_PER_HOUR"), 20, 1, 1000
        )
        if warning:
            warnings.append("invalid rate limit; using 20 per hour")
        stale_seconds, warning = _parse_int(
            os.environ.get("POSTGRAD_NOTIFICATION_STALE_SECONDS"), 300, 1, 86400
        )
        if warning:
            warnings.append("invalid stale timeout; using 300 seconds")

        return cls(
            dry_run=dry_run,
            email_allowlist=email_allowlist,
            wecom_allowlist=wecom_allowlist,
            wecom_webhooks=webhooks,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=os.environ.get("POSTGRAD_SMTP_USER", "").strip(),
            smtp_password=os.environ.get("POSTGRAD_SMTP_PASSWORD", ""),
            smtp_from=smtp_from,
            smtp_ssl=smtp_ssl,
            smtp_starttls=smtp_starttls,
            smtp_timeout=smtp_timeout,
            rate_limit_per_hour=rate_limit,
            stale_seconds=stale_seconds,
            warnings=warnings,
        )

    def email_missing(self) -> list[str]:
        missing = []
        if not self.smtp_host:
            missing.append("POSTGRAD_SMTP_HOST")
        if not self.smtp_from:
            missing.append("POSTGRAD_SMTP_FROM")
        return missing

    def email_ready(self) -> bool:
        return not self.email_missing()

    def wecom_ready(self, group: str) -> bool:
        return group in self.wecom_webhooks


class Authorization(BaseModel):
    """Explicit, channel-scoped user authorization supplied with a send.

    ``channel`` is optional because each tool already fixes its own channel;
    when supplied it must match. A plain string is accepted as the
    ``authorization_ref`` by the service layer for lightweight admin CLI use.
    """

    channel: str = ""
    authorized_by: str
    authorization_ref: str
    expires_at: str | None = None


class DeliveryFailed(Exception):
    """The provider definitely rejected the message."""


class DeliveryUncertain(Exception):
    """The message may have been delivered; never retry automatically."""


class DeliveryUnavailable(Exception):
    """Configuration is missing; no external call was attempted."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _smtp_send(
    config: NotificationConfig,
    recipients: list[str],
    subject: str,
    body: str,
) -> str:
    """SMTP adapter. Raises classified exceptions; never logs credentials."""

    message = EmailMessage()
    message["From"] = config.smtp_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    phase = "connect"
    try:
        smtp_cls = smtplib.SMTP_SSL if config.smtp_ssl else smtplib.SMTP
        with smtp_cls(
            config.smtp_host, config.smtp_port, timeout=config.smtp_timeout
        ) as client:
            if not config.smtp_ssl and config.smtp_starttls:
                client.starttls(context=ssl.create_default_context())
            if config.smtp_user:
                client.login(config.smtp_user, config.smtp_password)
            phase = "send"
            refused = client.send_message(message)
            if refused:
                # Some recipients were accepted, some were not: never retry.
                raise DeliveryUncertain("partial_recipient_refusal")
        return ""
    except (DeliveryFailed, DeliveryUncertain, DeliveryUnavailable):
        raise
    except (
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPSenderRefused,
        smtplib.SMTPAuthenticationError,
        smtplib.SMTPConnectError,
        smtplib.SMTPHeloError,
        smtplib.SMTPNotSupportedError,
        smtplib.SMTPDataError,
    ) as exc:
        raise DeliveryFailed(type(exc).__name__) from exc
    except (
        smtplib.SMTPServerDisconnected,
        TimeoutError,
        ConnectionError,
        OSError,
        ssl.SSLError,
    ) as exc:
        if phase == "send":
            raise DeliveryUncertain(type(exc).__name__) from exc
        raise DeliveryFailed(type(exc).__name__) from exc
    except smtplib.SMTPException as exc:
        raise DeliveryFailed(type(exc).__name__) from exc
    except Exception as exc:  # noqa: BLE001 - classified as uncertain, never retried
        raise DeliveryUncertain(type(exc).__name__) from exc


def _wecom_send(config: NotificationConfig, group: str, content: str) -> str:
    """WeCom robot adapter using only the URL fixed in configuration."""

    url = config.wecom_webhooks.get(group)
    if not url:
        raise DeliveryUnavailable("webhook is not configured for this group")
    payload = json.dumps(
        {"msgtype": "text", "text": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            status = getattr(response, "status", 200)
            raw = response.read(65_536)
    except urllib.error.HTTPError as exc:
        if 500 <= exc.code:
            raise DeliveryUncertain(f"http_{exc.code}") from exc
        raise DeliveryFailed(f"http_{exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise DeliveryUncertain(type(exc).__name__) from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeliveryUncertain("invalid_provider_response") from exc
    if status != 200:
        if status >= 500:
            raise DeliveryUncertain(f"http_{status}")
        raise DeliveryFailed(f"http_{status}")
    try:
        errcode = int(decoded.get("errcode", -1))
    except (TypeError, ValueError) as exc:
        raise DeliveryUncertain("invalid_provider_response") from exc
    if errcode != 0:
        raise DeliveryFailed(f"wecom_errcode_{errcode}")
    return ""


def _hash_delivery(channel: str, destination: str, subject: str, body: str) -> str:
    blob = json.dumps(
        {
            "channel": channel,
            "destination": destination,
            "subject": subject,
            "body": body,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'wecom')),
    destination TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    authorization_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deliveries_rate ON deliveries (
    channel, last_attempt_at
);
CREATE INDEX IF NOT EXISTS idx_deliveries_content ON deliveries (
    channel, destination, body_hash, status
);
"""


class NotificationStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _by_key(conn: sqlite3.Connection, key: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE idempotency_key = ?", [key]
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _by_id(conn: sqlite3.Connection, delivery_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM deliveries WHERE id = ?", [delivery_id]).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _insert(conn: sqlite3.Connection, record: dict) -> dict:
        conn.execute(
            """INSERT INTO deliveries
               (id, idempotency_key, channel, destination, subject, body_hash,
                status, error_code, detail, provider_message_id, attempts,
                authorization_json, created_at, updated_at, last_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                record["id"],
                record["idempotency_key"],
                record["channel"],
                record["destination"],
                record["subject"],
                record["body_hash"],
                record["status"],
                record.get("error_code", ""),
                record.get("detail", ""),
                record.get("provider_message_id", ""),
                record.get("attempts", 0),
                record["authorization_json"],
                record["created_at"],
                record["updated_at"],
                record.get("last_attempt_at"),
            ],
        )
        return record


class NotificationService:
    def __init__(self, store: NotificationStore, config: NotificationConfig) -> None:
        self.store = store
        self.config = config

    # ------------------------------------------------------------- validation
    @staticmethod
    def _validate_key(value: object) -> str:
        if not isinstance(value, str) or not _KEY_RE.match(value.strip()):
            raise UserError(
                "invalid_idempotency_key",
                "idempotency_key must be 1-128 chars of letters, digits, . _ : -",
            )
        return value.strip()

    @staticmethod
    def _validate_authorization(
        value: Authorization | dict | str | None, channel: str
    ) -> dict:
        if value is None:
            raise UserError(
                "authorization_required",
                "explicit authorization is required for every send",
            )
        if isinstance(value, str):
            value = Authorization(
                channel=channel,
                authorized_by="caller",
                authorization_ref=value,
            )
        if isinstance(value, dict):
            try:
                value = Authorization(**value)
            except Exception as exc:
                raise UserError(
                    "authorization_required", "authorization is invalid"
                ) from exc
        if not isinstance(value, Authorization):
            raise UserError("authorization_required", "authorization is invalid")
        authorized_by = value.authorized_by.strip()
        reference = value.authorization_ref.strip()
        if not authorized_by or len(authorized_by) > 200:
            raise UserError("authorization_required", "authorized_by is required")
        if not reference or len(reference) > 500:
            raise UserError("authorization_required", "authorization_ref is required")
        declared_channel = value.channel.strip().lower()
        if declared_channel and declared_channel != channel:
            raise UserError(
                "authorization_channel_mismatch",
                f"authorization must be scoped to the {channel} channel",
            )
        if value.expires_at:
            try:
                expires = _parse_time(value.expires_at)
            except ValueError as exc:
                raise UserError(
                    "authorization_invalid", "expires_at must be ISO-8601"
                ) from exc
            if expires <= datetime.now(UTC):
                raise UserError("authorization_expired", "authorization has expired")
        return {
            "channel": channel,
            "authorized_by": authorized_by,
            "authorization_ref": reference,
            "expires_at": value.expires_at,
        }

    @staticmethod
    def _validate_subject(value: object) -> str:
        if not isinstance(value, str):
            raise UserError("invalid_subject", "subject must be a string")
        subject = value.strip()
        if not 1 <= len(subject) <= MAX_SUBJECT_CHARS:
            raise UserError(
                "invalid_subject", f"subject must be 1-{MAX_SUBJECT_CHARS} characters"
            )
        if any(char in subject for char in "\r\n\x00"):
            raise UserError("invalid_subject", "subject must not contain line breaks")
        return subject

    @staticmethod
    def _validate_body(value: object) -> str:
        if not isinstance(value, str):
            raise UserError("invalid_body", "body must be a string")
        body = value.strip()
        if not 1 <= len(body) <= MAX_EMAIL_BODY_CHARS:
            raise UserError(
                "invalid_body", f"body must be 1-{MAX_EMAIL_BODY_CHARS} characters"
            )
        if "\x00" in body:
            raise UserError("invalid_body", "body must not contain NUL")
        return body

    @staticmethod
    def _validate_recipients(value: object) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_EMAIL_RECIPIENTS:
            raise UserError(
                "invalid_recipients",
                f"to must contain 1-{MAX_EMAIL_RECIPIENTS} email addresses",
            )
        recipients: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise UserError("invalid_recipients", "every recipient must be a string")
            address = item.strip().lower()
            if any(char in address for char in "\r\n\x00") or not _valid_email(address):
                raise UserError("invalid_recipients", f"invalid email address: {address[:80]}")
            if address not in recipients:
                recipients.append(address)
        return recipients

    @staticmethod
    def _validate_wecom_group(value: object) -> str:
        if not isinstance(value, str):
            raise UserError("invalid_group", "group must be a string")
        group = value.strip()
        if not 1 <= len(group) <= 100 or any(char in group for char in "\r\n\x00"):
            raise UserError("invalid_group", "group is invalid")
        return group

    @staticmethod
    def _validate_wecom_content(value: object) -> str:
        if not isinstance(value, str):
            raise UserError("invalid_body", "content must be a string")
        content = value.strip()
        if not content:
            raise UserError("invalid_body", "content is required")
        if len(content.encode("utf-8")) > MAX_WECOM_BODY_BYTES:
            raise UserError(
                "invalid_body", f"content exceeds {MAX_WECOM_BODY_BYTES} UTF-8 bytes"
            )
        if "\x00" in content:
            raise UserError("invalid_body", "content must not contain NUL")
        if "@all" in content.casefold():
            raise UserError("mention_all_blocked", "group-wide @all mentions are blocked")
        return content

    # --------------------------------------------------------------- responses
    @staticmethod
    def _public(row: dict) -> dict:
        retry_allowed = row["status"] in {"failed", "unavailable", "blocked", "rate_limited"}
        return {
            "delivery_id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "channel": row["channel"],
            "destination": row["destination"],
            "subject": row["subject"],
            "status": row["status"],
            "dry_run": row["status"] == "dry_run",
            "attempts": row["attempts"],
            "provider_message_id": row["provider_message_id"] or None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "retry_allowed": retry_allowed,
            "error_code": row["error_code"] or None,
            "detail": row["detail"] or None,
        }

    def _response(
        self, row: dict, *, duplicate: bool = False, warnings: list[str] | None = None
    ) -> dict:
        body = {
            "ok": row["status"] in {"sent", "dry_run"},
            "duplicate": duplicate,
            **self._public(row),
        }
        if not body["ok"]:
            body["error"] = {
                "code": row["error_code"] or row["status"],
                "message": row["detail"] or row["status"],
            }
        if warnings:
            body["warnings"] = warnings
        return body

    # --------------------------------------------------------------- persistence
    def _record_preflight(
        self,
        *,
        key: str,
        channel: str,
        destination: str,
        subject: str,
        content_hash: str,
        authorization: dict,
        status: str,
        error_code: str,
        detail: str,
    ) -> dict:
        now = _now()
        with self.store.tx() as conn:
            row = self.store._by_key(conn, key)
            if row is not None:
                if row["body_hash"] != content_hash:
                    raise UserError(
                        "idempotency_conflict",
                        "idempotency_key was already used for a different message",
                    )
                if row["status"] in _TERMINAL or row["status"] == "sending":
                    return row
                conn.execute(
                    """UPDATE deliveries
                       SET status = ?, error_code = ?, detail = ?, updated_at = ?
                       WHERE id = ?""",
                    [status, error_code, detail, now, row["id"]],
                )
                row.update(status=status, error_code=error_code, detail=detail, updated_at=now)
                return row
            record = {
                "id": "dlv_" + uuid.uuid4().hex,
                "idempotency_key": key,
                "channel": channel,
                "destination": destination,
                "subject": subject,
                "body_hash": content_hash,
                "status": status,
                "error_code": error_code,
                "detail": detail,
                "provider_message_id": "",
                "attempts": 0,
                "authorization_json": json.dumps(authorization, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
                "last_attempt_at": None,
            }
            return self.store._insert(conn, record)

    def _record_dry_run(
        self,
        *,
        key: str,
        channel: str,
        destination: str,
        subject: str,
        content_hash: str,
        authorization: dict,
    ) -> tuple[dict, bool]:
        now = _now()
        with self.store.tx() as conn:
            row = self.store._by_key(conn, key)
            if row is not None:
                if row["body_hash"] != content_hash:
                    raise UserError(
                        "idempotency_conflict",
                        "idempotency_key was already used for a different message",
                    )
                if row["status"] in _TERMINAL or row["status"] == "sending":
                    return row, True
                conn.execute(
                    """UPDATE deliveries
                       SET status = 'dry_run', error_code = '', detail = '',
                           updated_at = ?
                       WHERE id = ?""",
                    [now, row["id"]],
                )
                row.update(
                    status="dry_run", error_code="", detail="", updated_at=now
                )
                return row, False
            record = {
                "id": "dlv_" + uuid.uuid4().hex,
                "idempotency_key": key,
                "channel": channel,
                "destination": destination,
                "subject": subject,
                "body_hash": content_hash,
                "status": "dry_run",
                "error_code": "",
                "detail": "",
                "provider_message_id": "",
                "attempts": 0,
                "authorization_json": json.dumps(authorization, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
                "last_attempt_at": None,
            }
            return self.store._insert(conn, record), False

    def _begin_send(
        self,
        *,
        key: str,
        channel: str,
        destination: str,
        subject: str,
        content_hash: str,
        authorization: dict,
    ) -> tuple[dict, bool]:
        """Atomically reserve a send. Returns (row, should_send)."""

        now = _now()
        now_dt = datetime.now(UTC)
        rate_cutoff = (now_dt - timedelta(hours=1)).isoformat(timespec="seconds")
        uncertain_cutoff = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")
        with self.store.tx() as conn:
            row = self.store._by_key(conn, key)
            if row is not None:
                if row["body_hash"] != content_hash:
                    raise UserError(
                        "idempotency_conflict",
                        "idempotency_key was already used for a different message",
                    )
                if row["status"] == "sending":
                    if row["updated_at"] < (
                        now_dt - timedelta(seconds=self.config.stale_seconds)
                    ).isoformat(timespec="seconds"):
                        conn.execute(
                            """UPDATE deliveries
                               SET status = 'uncertain', error_code = 'delivery_state_unknown',
                                   detail = 'send was interrupted; reconcile before retrying',
                                   updated_at = ?
                               WHERE id = ?""",
                            [now, row["id"]],
                        )
                        row.update(
                            status="uncertain",
                            error_code="delivery_state_unknown",
                            detail="send was interrupted; reconcile before retrying",
                            updated_at=now,
                        )
                    return row, False
                if row["status"] in _TERMINAL:
                    return row, False

            prior = conn.execute(
                """SELECT id FROM deliveries
                   WHERE channel = ? AND destination = ? AND body_hash = ?
                     AND status = 'uncertain' AND last_attempt_at >= ?
                     AND idempotency_key <> ?
                   LIMIT 1""",
                [
                    channel,
                    destination,
                    content_hash,
                    uncertain_cutoff,
                    key,
                ],
            ).fetchone()
            if prior is not None:
                detail = "an earlier identical send has an uncertain outcome; reconcile first"
                if row is None:
                    record = {
                        "id": "dlv_" + uuid.uuid4().hex,
                        "idempotency_key": key,
                        "channel": channel,
                        "destination": destination,
                        "subject": subject,
                        "body_hash": content_hash,
                        "status": "blocked",
                        "error_code": "prior_uncertain",
                        "detail": detail,
                        "provider_message_id": "",
                        "attempts": 0,
                        "authorization_json": json.dumps(authorization, ensure_ascii=False),
                        "created_at": now,
                        "updated_at": now,
                        "last_attempt_at": None,
                    }
                    return self.store._insert(conn, record), False
                conn.execute(
                    """UPDATE deliveries
                       SET status = 'blocked', error_code = 'prior_uncertain',
                           detail = ?, updated_at = ?
                       WHERE id = ?""",
                    [detail, now, row["id"]],
                )
                row.update(
                    status="blocked",
                    error_code="prior_uncertain",
                    detail=detail,
                    updated_at=now,
                )
                return row, False

            sent_recently = conn.execute(
                """SELECT COUNT(*) AS total FROM deliveries
                   WHERE channel = ? AND attempts > 0 AND last_attempt_at >= ?""",
                [channel, rate_cutoff],
            ).fetchone()["total"]
            if int(sent_recently) >= self.config.rate_limit_per_hour:
                detail = (
                    f"rate limit reached for {channel}: "
                    f"{self.config.rate_limit_per_hour} attempts per hour"
                )
                if row is None:
                    record = {
                        "id": "dlv_" + uuid.uuid4().hex,
                        "idempotency_key": key,
                        "channel": channel,
                        "destination": destination,
                        "subject": subject,
                        "body_hash": content_hash,
                        "status": "rate_limited",
                        "error_code": "rate_limited",
                        "detail": detail,
                        "provider_message_id": "",
                        "attempts": 0,
                        "authorization_json": json.dumps(authorization, ensure_ascii=False),
                        "created_at": now,
                        "updated_at": now,
                        "last_attempt_at": None,
                    }
                    return self.store._insert(conn, record), False
                conn.execute(
                    """UPDATE deliveries
                       SET status = 'rate_limited', error_code = 'rate_limited',
                           detail = ?, updated_at = ?
                       WHERE id = ?""",
                    [detail, now, row["id"]],
                )
                row.update(
                    status="rate_limited",
                    error_code="rate_limited",
                    detail=detail,
                    updated_at=now,
                )
                return row, False

            if row is None:
                record = {
                    "id": "dlv_" + uuid.uuid4().hex,
                    "idempotency_key": key,
                    "channel": channel,
                    "destination": destination,
                    "subject": subject,
                    "body_hash": content_hash,
                    "status": "sending",
                    "error_code": "",
                    "detail": "",
                    "provider_message_id": "",
                    "attempts": 1,
                    "authorization_json": json.dumps(authorization, ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                    "last_attempt_at": now,
                }
                return self.store._insert(conn, record), True
            conn.execute(
                """UPDATE deliveries
                   SET status = 'sending', error_code = '', detail = '',
                       provider_message_id = '', attempts = attempts + 1,
                       last_attempt_at = ?, updated_at = ?
                   WHERE id = ?""",
                [now, now, row["id"]],
            )
            row.update(
                status="sending",
                error_code="",
                detail="",
                provider_message_id="",
                attempts=int(row["attempts"]) + 1,
                last_attempt_at=now,
                updated_at=now,
            )
            return row, True

    def _finish(
        self,
        row: dict,
        status: str,
        error_code: str = "",
        detail: str = "",
        provider_message_id: str = "",
    ) -> dict:
        now = _now()
        try:
            with self.store.tx() as conn:
                conn.execute(
                    """UPDATE deliveries
                       SET status = ?, error_code = ?, detail = ?,
                           provider_message_id = ?, updated_at = ?
                       WHERE id = ?""",
                    [status, error_code, detail, provider_message_id, now, row["id"]],
                )
        except sqlite3.Error:
            # The external call may have succeeded; never invite a retry.
            row.update(
                status="uncertain",
                error_code="delivery_state_unknown",
                detail="delivery outcome could not be persisted; reconcile before retrying",
                provider_message_id="",
                updated_at=now,
            )
            return row
        row.update(
            status=status,
            error_code=error_code,
            detail=detail,
            provider_message_id=provider_message_id,
            updated_at=now,
        )
        return row

    # ----------------------------------------------------------------- tools
    def send_email(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        idempotency_key: str,
        authorization: Authorization | str | None = None,
        dry_run: bool | None = None,
    ) -> dict:
        """Send (or safely dry-run) one plain-text email to allowlisted users.

        Dry-run is the default. Per-call ``dry_run=True`` can only tighten
        safety; it can never disable environment dry-run. Real sending requires
        explicit environment configuration. All recipients must be allowlisted
        and every call needs explicit authorization plus an idempotency key.
        """

        try:
            recipients = self._validate_recipients(to)
            subject = self._validate_subject(subject)
            body = self._validate_body(body)
            key = self._validate_key(idempotency_key)
            auth = self._validate_authorization(authorization, "email")
            effective_dry_run = self.config.dry_run or bool(dry_run)
            destination = ",".join(recipients)
            content_hash = _hash_delivery("email", destination, subject, body)
            denied = [
                address
                for address in recipients
                if address not in self.config.email_allowlist
            ]
            if denied:
                row = self._record_preflight(
                    key=key,
                    channel="email",
                    destination=destination,
                    subject=subject,
                    content_hash=content_hash,
                    authorization=auth,
                    status="blocked",
                    error_code="recipient_not_allowlisted",
                    detail="all recipients must be explicitly allowlisted",
                )
                return self._response(row, duplicate=False)
            warnings = list(self.config.warnings)
            if effective_dry_run:
                if not self.config.email_ready():
                    warnings.append(
                        "dry-run only; real email is unavailable until "
                        + ", ".join(self.config.email_missing())
                        + " are set"
                    )
                row, duplicate = self._record_dry_run(
                    key=key,
                    channel="email",
                    destination=destination,
                    subject=subject,
                    content_hash=content_hash,
                    authorization=auth,
                )
                return self._response(
                    row, duplicate=duplicate, warnings=warnings or None
                )
            if not self.config.email_ready():
                detail = (
                    "SMTP is unavailable; configure "
                    + ", ".join(self.config.email_missing())
                )
                row = self._record_preflight(
                    key=key,
                    channel="email",
                    destination=destination,
                    subject=subject,
                    content_hash=content_hash,
                    authorization=auth,
                    status="unavailable",
                    error_code="provider_unavailable",
                    detail=detail,
                )
                return self._response(row)
            row, should_send = self._begin_send(
                key=key,
                channel="email",
                destination=destination,
                subject=subject,
                content_hash=content_hash,
                authorization=auth,
            )
            if not should_send:
                return self._response(row, duplicate=True)
            try:
                provider_id = _smtp_send(self.config, recipients, subject, body)
            except DeliveryUnavailable as exc:
                row = self._finish(
                    row,
                    "unavailable",
                    "provider_unavailable",
                    f"SMTP unavailable: {exc}",
                )
            except DeliveryFailed as exc:
                row = self._finish(
                    row,
                    "failed",
                    "provider_rejected",
                    f"SMTP rejected the message ({exc}); no automatic retry",
                )
            except DeliveryUncertain as exc:
                row = self._finish(
                    row,
                    "uncertain",
                    "delivery_uncertain",
                    f"SMTP outcome is uncertain ({exc}); reconcile before any retry",
                )
            except Exception as exc:  # noqa: BLE001 - conservative outcome
                row = self._finish(
                    row,
                    "uncertain",
                    "delivery_uncertain",
                    f"unexpected SMTP outcome ({type(exc).__name__}); reconcile",
                )
            else:
                row = self._finish(
                    row, "sent", "", "", provider_message_id=provider_id
                )
            return self._response(row)
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "notification database is unavailable")

    def send_wecom_group(
        self,
        group: str,
        content: str,
        idempotency_key: str,
        authorization: Authorization | str | None = None,
        dry_run: bool | None = None,
    ) -> dict:
        """Send (or safely dry-run) a text message to one allowlisted WeCom group.

        The webhook URL cannot be supplied by the caller; it must come from
        ``POSTGRAD_WECOM_WEBHOOKS`` and point at the official WeCom robot
        endpoint. Per-call ``dry_run=True`` can only tighten safety and never
        disables environment dry-run. Uncertain outcomes are never retried.
        """

        try:
            group = self._validate_wecom_group(group)
            content = self._validate_wecom_content(content)
            key = self._validate_key(idempotency_key)
            auth = self._validate_authorization(authorization, "wecom")
            effective_dry_run = self.config.dry_run or bool(dry_run)
            subject = ""
            content_hash = _hash_delivery("wecom", group, subject, content)
            if group not in self.config.wecom_allowlist:
                row = self._record_preflight(
                    key=key,
                    channel="wecom",
                    destination=group,
                    subject=subject,
                    content_hash=content_hash,
                    authorization=auth,
                    status="blocked",
                    error_code="group_not_allowlisted",
                    detail="group must be explicitly allowlisted",
                )
                return self._response(row)
            warnings = list(self.config.warnings)
            if effective_dry_run:
                if not self.config.wecom_ready(group):
                    warnings.append(
                        "dry-run only; this group has no configured webhook "
                        "(POSTGRAD_WECOM_WEBHOOKS), so real delivery is unavailable"
                    )
                row, duplicate = self._record_dry_run(
                    key=key,
                    channel="wecom",
                    destination=group,
                    subject=subject,
                    content_hash=content_hash,
                    authorization=auth,
                )
                return self._response(
                    row, duplicate=duplicate, warnings=warnings or None
                )
            if not self.config.wecom_ready(group):
                row = self._record_preflight(
                    key=key,
                    channel="wecom",
                    destination=group,
                    subject=subject,
                    content_hash=content_hash,
                    authorization=auth,
                    status="unavailable",
                    error_code="provider_unavailable",
                    detail=(
                        "WeCom is unavailable; configure a webhook for this group "
                        "via POSTGRAD_WECOM_WEBHOOKS"
                    ),
                )
                return self._response(row)
            row, should_send = self._begin_send(
                key=key,
                channel="wecom",
                destination=group,
                subject=subject,
                content_hash=content_hash,
                authorization=auth,
            )
            if not should_send:
                return self._response(row, duplicate=True)
            try:
                provider_id = _wecom_send(self.config, group, content)
            except DeliveryUnavailable as exc:
                row = self._finish(
                    row,
                    "unavailable",
                    "provider_unavailable",
                    f"WeCom unavailable: {exc}",
                )
            except DeliveryFailed as exc:
                row = self._finish(
                    row,
                    "failed",
                    "provider_rejected",
                    f"WeCom rejected the message ({exc}); no automatic retry",
                )
            except DeliveryUncertain as exc:
                row = self._finish(
                    row,
                    "uncertain",
                    "delivery_uncertain",
                    f"WeCom outcome is uncertain ({exc}); reconcile before any retry",
                )
            except Exception as exc:  # noqa: BLE001 - conservative outcome
                row = self._finish(
                    row,
                    "uncertain",
                    "delivery_uncertain",
                    f"unexpected WeCom outcome ({type(exc).__name__}); reconcile",
                )
            else:
                row = self._finish(
                    row, "sent", "", "", provider_message_id=provider_id
                )
            return self._response(row)
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "notification database is unavailable")

    def get_delivery_status(
        self,
        idempotency_key: str = "",
        delivery_id: str = "",
    ) -> dict:
        """Read a persisted delivery record by idempotency key or delivery id.

        A stale ``sending`` record is reported as ``uncertain`` and is retry
        forbidden. Responses never include SMTP credentials or webhook URLs.
        """

        try:
            key = (idempotency_key or "").strip()
            identifier = (delivery_id or "").strip()
            if bool(key) == bool(identifier):
                raise UserError(
                    "invalid_lookup",
                    "provide exactly one of idempotency_key or delivery_id",
                )
            with self.store.read() as conn:
                row = (
                    self.store._by_key(conn, key)
                    if key
                    else self.store._by_id(conn, identifier)
                )
            if row is None:
                raise UserError("not_found", "delivery record was not found")
            if row["status"] == "sending" and row["updated_at"] < (
                datetime.now(UTC)
                - timedelta(seconds=self.config.stale_seconds)
            ).isoformat(timespec="seconds"):
                row = self._finish(
                    row,
                    "uncertain",
                    "delivery_state_unknown",
                    "send was interrupted; reconcile before retrying",
                )
        except UserError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error:
            return _error("storage_unavailable", "notification database is unavailable")
        return {"ok": True, "found": True, **self._public(row)}


def build_server(
    data_dir: str | Path | None = None,
    config: NotificationConfig | None = None,
) -> MCPServer:
    """Build the notification stdio server. Dry-run is on unless env disables it."""

    directory = Path(data_dir).expanduser() if data_dir is not None else _data_dir()
    store = NotificationStore(directory / "notification.sqlite3")
    service = NotificationService(store, config or NotificationConfig.from_env())
    server = MCPServer(SERVER_NAME, version=SERVER_VERSION)
    for tool in (
        service.send_email,
        service.send_wecom_group,
        service.get_delivery_status,
    ):
        server.add_tool(tool)
    return server


if __name__ == "__main__":
    build_server().run("stdio")
