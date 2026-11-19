"""Focused safety tests for the postgrad notification MCP.

Run from the repository root:
    backend/.venv/bin/python -m pytest extensions/postgraduate/tests/test_notification.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1] / "mcp"
sys.path.insert(0, str(MCP_DIR))

import notification as nt  # noqa: E402

ENV_KEYS = [
    "POSTGRAD_DATA_DIR",
    "POSTGRAD_NOTIFICATION_DRY_RUN",
    "POSTGRAD_EMAIL_ALLOWLIST",
    "POSTGRAD_WECOM_ALLOWLIST",
    "POSTGRAD_WECOM_WEBHOOKS",
    "POSTGRAD_SMTP_HOST",
    "POSTGRAD_SMTP_PORT",
    "POSTGRAD_SMTP_USER",
    "POSTGRAD_SMTP_PASSWORD",
    "POSTGRAD_SMTP_FROM",
    "POSTGRAD_SMTP_SSL",
    "POSTGRAD_SMTP_STARTTLS",
    "POSTGRAD_SMTP_TIMEOUT",
    "POSTGRAD_NOTIFICATION_RATE_LIMIT_PER_HOUR",
    "POSTGRAD_NOTIFICATION_STALE_SECONDS",
]
AUTH = {
    "channel": "email",
    "authorized_by": "main-codex",
    "authorization_ref": "user-approved-2026-11-18",
    "expires_at": "2099-01-01T00:00:00Z",
}


def clear_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def call(server, name: str, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    assert not result.is_error, result
    text = "".join(block.text for block in result.content if getattr(block, "type", "") == "text")
    return json.loads(text)


def send_email(
    server, *, to=None, subject="练习提醒", body="今天完成 20 题。", key="email-key-0001", auth=None
):
    return call(
        server,
        "send_email",
        to=to or ["allowed@example.com"],
        subject=subject,
        body=body,
        idempotency_key=key,
        authorization=AUTH if auth is None else auth,
    )


def test_default_dry_run_records_without_any_network(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    server = nt.build_server(data_dir=tmp_path)

    calls = []
    monkeypatch.setattr(nt, "_smtp_send", lambda *args, **kwargs: calls.append(args) or "")

    result = send_email(server)
    assert result["ok"], result
    assert result["status"] == "dry_run"
    assert result["duplicate"] is False
    assert calls == []
    assert any("dry-run only" in warning for warning in result["warnings"])

    duplicate = send_email(server)
    assert duplicate["duplicate"] is True
    assert duplicate["status"] == "dry_run"
    assert calls == []

    status = call(server, "get_delivery_status", idempotency_key="email-key-0001")
    assert status["ok"] is True
    assert status["found"] is True
    assert status["status"] == "dry_run"


def test_real_email_without_smtp_is_clearly_unavailable(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    server = nt.build_server(data_dir=tmp_path)

    calls = []
    monkeypatch.setattr(nt, "_smtp_send", lambda *args, **kwargs: calls.append(args) or "")
    result = send_email(server, key="email-key-0002")
    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "provider_unavailable"
    assert "POSTGRAD_SMTP_HOST" in result["error"]["message"]
    assert calls == []

    status = call(server, "get_delivery_status", idempotency_key="email-key-0002")
    assert status["status"] == "unavailable"
    assert status["retry_allowed"] is True


def test_allowlist_authorization_and_idempotency_conflicts(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    server = nt.build_server(data_dir=tmp_path)

    denied = send_email(server, to=["other@example.com"], key="email-key-0003")
    assert denied["ok"] is False
    assert denied["error"]["code"] == "recipient_not_allowlisted"

    first = send_email(server, key="email-key-0004")
    assert first["status"] == "dry_run"
    conflict = send_email(server, key="email-key-0004", body="不同内容")
    assert conflict["error"]["code"] == "idempotency_conflict"

    wrong_channel = send_email(
        server,
        key="email-key-0005",
        auth={**AUTH, "channel": "wecom"},
    )
    assert wrong_channel["error"]["code"] == "authorization_channel_mismatch"

    expired = send_email(
        server,
        key="email-key-0006",
        auth={**AUTH, "expires_at": "2000-01-01T00:00:00Z"},
    )
    assert expired["error"]["code"] == "authorization_expired"

    config = nt.NotificationConfig.from_env()
    store = nt.NotificationStore(tmp_path / "notification.sqlite3")
    service = nt.NotificationService(store, config)
    missing = service.send_email(["allowed@example.com"], "s", "b", "email-key-0007", None)
    assert missing["error"]["code"] == "authorization_required"


def test_real_email_sent_idempotent_and_password_redacted(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_PASSWORD", "super-secret-password")
    server = nt.build_server(data_dir=tmp_path)

    calls = []

    def fake_send(config, recipients, subject, body):
        calls.append((recipients, subject, body))
        return "provider-message-1"

    monkeypatch.setattr(nt, "_smtp_send", fake_send)
    result = send_email(server, key="email-key-0008")
    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result["provider_message_id"] == "provider-message-1"
    assert calls and calls[0][0] == ["allowed@example.com"]
    assert "super-secret-password" not in json.dumps(result)

    duplicate = send_email(server, key="email-key-0008")
    assert duplicate["duplicate"] is True
    assert duplicate["status"] == "sent"
    assert len(calls) == 1

    status = call(server, "get_delivery_status", idempotency_key="email-key-0008")
    assert status["status"] == "sent"
    assert "super-secret-password" not in json.dumps(status)


def test_uncertain_is_never_retried_and_blocks_identical_content(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_FROM", "bot@example.com")
    server = nt.build_server(data_dir=tmp_path)

    calls = []

    def uncertain(config, recipients, subject, body):
        calls.append((recipients, subject, body))
        raise nt.DeliveryUncertain("timeout after DATA")

    monkeypatch.setattr(nt, "_smtp_send", uncertain)
    first = send_email(server, key="email-key-0009")
    assert first["ok"] is False
    assert first["status"] == "uncertain"
    assert first["retry_allowed"] is False

    duplicate = send_email(server, key="email-key-0009")
    assert duplicate["duplicate"] is True
    assert duplicate["status"] == "uncertain"
    assert len(calls) == 1

    same_content_new_key = send_email(server, key="email-key-0010")
    assert same_content_new_key["status"] == "blocked"
    assert same_content_new_key["error"]["code"] == "prior_uncertain"
    assert len(calls) == 1

    status = call(server, "get_delivery_status", idempotency_key="email-key-0009")
    assert status["status"] == "uncertain"
    assert status["retry_allowed"] is False


def test_rate_limit_is_persisted_before_sending(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_RATE_LIMIT_PER_HOUR", "2")
    server = nt.build_server(data_dir=tmp_path)

    calls = []
    monkeypatch.setattr(
        nt,
        "_smtp_send",
        lambda *args, **kwargs: calls.append(args) or "",
    )
    assert send_email(server, key="email-key-0011")["status"] == "sent"
    assert send_email(server, key="email-key-0012")["status"] == "sent"
    limited = send_email(server, key="email-key-0013")
    assert limited["status"] == "rate_limited"
    assert limited["error"]["code"] == "rate_limited"
    assert len(calls) == 2


def test_wecom_dry_run_ignores_arbitrary_webhooks_and_blocks_at_all(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_WECOM_ALLOWLIST", "study-group")
    monkeypatch.setenv(
        "POSTGRAD_WECOM_WEBHOOKS",
        json.dumps({"study-group": "https://evil.example/cgi-bin/webhook/send?key=x"}),
    )
    server = nt.build_server(data_dir=tmp_path)
    auth = {**AUTH, "channel": "wecom"}

    result = call(
        server,
        "send_wecom_group",
        group="study-group",
        content="今日练习",
        idempotency_key="wecom-key-0001",
        authorization=auth,
    )
    assert result["status"] == "dry_run"
    assert any("no configured webhook" in warning for warning in result["warnings"])
    assert "evil.example" not in json.dumps(result)

    mention = call(
        server,
        "send_wecom_group",
        group="study-group",
        content="@all 全员刷题",
        idempotency_key="wecom-key-0002",
        authorization=auth,
    )
    assert mention["error"]["code"] == "mention_all_blocked"


def test_wecom_real_send_redacts_webhook_secret(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    monkeypatch.setenv("POSTGRAD_WECOM_ALLOWLIST", "study-group")
    monkeypatch.setenv(
        "POSTGRAD_WECOM_WEBHOOKS",
        json.dumps(
            {
                "study-group": (
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPER-SECRET-KEY"
                )
            }
        ),
    )
    server = nt.build_server(data_dir=tmp_path)
    calls = []

    def fake_send(config, group, content):
        calls.append((group, content))
        return "wecom-message-1"

    monkeypatch.setattr(nt, "_wecom_send", fake_send)
    result = call(
        server,
        "send_wecom_group",
        group="study-group",
        content="今日练习",
        idempotency_key="wecom-key-0003",
        authorization={**AUTH, "channel": "wecom"},
    )
    assert result["status"] == "sent"
    assert result["provider_message_id"] == "wecom-message-1"
    assert calls == [("study-group", "今日练习")]
    assert "SUPER-SECRET-KEY" not in json.dumps(result)

    status = call(server, "get_delivery_status", idempotency_key="wecom-key-0003")
    assert status["status"] == "sent"
    assert "SUPER-SECRET-KEY" not in json.dumps(status)


def test_status_lookup_validation_and_persistence(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    server = nt.build_server(data_dir=tmp_path)
    assert send_email(server, key="email-key-0014")["status"] == "dry_run"

    restarted = nt.build_server(data_dir=tmp_path)
    status = call(restarted, "get_delivery_status", idempotency_key="email-key-0014")
    assert status["found"] is True
    assert status["status"] == "dry_run"

    missing = call(restarted, "get_delivery_status", idempotency_key="email-key-9999")
    assert missing["error"]["code"] == "not_found"

    invalid = call(
        restarted,
        "get_delivery_status",
        idempotency_key="email-key-0014",
        delivery_id="dlv_x",
    )
    assert invalid["error"]["code"] == "invalid_lookup"


def test_explicit_empty_wecom_allowlist_never_falls_back_to_webhooks(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(
        "POSTGRAD_WECOM_WEBHOOKS",
        json.dumps(
            {
                "study-group": (
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
                )
            }
        ),
    )
    monkeypatch.setenv("POSTGRAD_WECOM_ALLOWLIST", "")
    config = nt.NotificationConfig.from_env()
    assert config.wecom_allowlist == frozenset()

    server = nt.build_server(data_dir=tmp_path)
    calls = []
    monkeypatch.setattr(
        nt, "_wecom_send", lambda *args, **kwargs: calls.append(args) or ""
    )
    result = call(
        server,
        "send_wecom_group",
        group="study-group",
        content="不应发送",
        idempotency_key="wecom-empty-allow-1",
        authorization={**AUTH, "channel": "wecom"},
    )
    assert result["status"] == "blocked"
    assert result["error"]["code"] == "group_not_allowlisted"
    assert calls == []


def test_per_call_dry_run_can_only_tighten_environment(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("POSTGRAD_EMAIL_ALLOWLIST", "allowed@example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("POSTGRAD_SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "0")
    calls = []
    monkeypatch.setattr(
        nt, "_smtp_send", lambda *args, **kwargs: calls.append(args) or ""
    )

    server = nt.build_server(data_dir=tmp_path)
    forced = call(
        server,
        "send_email",
        to=["allowed@example.com"],
        subject="per-call dry-run",
        body="must not leave the process",
        idempotency_key="dryrun-tighten-1",
        authorization={**AUTH, "channel": "email"},
        dry_run=True,
    )
    assert forced["status"] == "dry_run"
    assert forced["dry_run"] is True
    assert calls == []

    # dry_run=False cannot disable an environment-level dry-run.
    monkeypatch.setenv("POSTGRAD_NOTIFICATION_DRY_RUN", "1")
    server_default_dry = nt.build_server(data_dir=tmp_path)
    cannot_disable = call(
        server_default_dry,
        "send_email",
        to=["allowed@example.com"],
        subject="cannot disable",
        body="must not leave the process",
        idempotency_key="dryrun-tighten-2",
        authorization={**AUTH, "channel": "email"},
        dry_run=False,
    )
    assert cannot_disable["status"] == "dry_run"
    assert calls == []
