import hashlib
import hmac
import json

import pytest

from mysql_mcp_server.audit import (
    AuditWriteError,
    JsonlAuditSink,
    build_audit_context,
    reset_audit_context,
    set_audit_context,
    validate_required_audit_context,
)
from mysql_mcp_server.config import ConnectionProfile


def audit_profile(tmp_path, **overrides):
    values = {
        "name": "prod",
        "host": "db.internal",
        "port": 3306,
        "user": "reader",
        "password": "secret",
        "database": "app",
        "audit_log_file": str(tmp_path / "audit.jsonl"),
        "audit_log_max_bytes": 10_000,
        "audit_log_backup_count": 2,
    }
    values.update(overrides)
    return ConnectionProfile(**values)


def test_jsonl_audit_is_attributed_signed_and_contains_no_query_or_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "signing-secret")
    profile = audit_profile(tmp_path, audit_hmac_key_env="AUDIT_SIGNING_KEY")
    context = build_audit_context(
        {
            "actor": "svc-reporting",
            "purpose": "monthly close",
            "ticket_id": "FIN-42",
        },
        request_id="request-1",
        operation="execute_sql",
        client_name="test-client",
        client_version="1.2.3",
    )
    token = set_audit_context(context)
    try:
        _, serialized = JsonlAuditSink().write(
            profile,
            {
                "event": "mysql_read_query",
                "connection": "prod",
                "database": "app",
                "query_id": "abc123",
                "status": "success",
            },
        )
    finally:
        reset_audit_context(token)

    event = json.loads(serialized)
    assert event["schema_version"] == 1
    assert event["request_id"] == "request-1"
    assert event["actor"] == "svc-reporting"
    assert event["ticket_id"] == "FIN-42"
    assert event["timestamp"].endswith("Z")
    assert len(event["event_id"]) == 32
    signature = event.pop("signature")
    assert event["signature_algorithm"] == "hmac-sha256"
    canonical = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert (
        signature
        == hmac.new(b"signing-secret", canonical.encode(), hashlib.sha256).hexdigest()
    )
    contents = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "SELECT " not in contents
    assert "secret" not in contents


def test_audit_sink_rotates_jsonl(tmp_path):
    profile = audit_profile(tmp_path, audit_log_max_bytes=10_000)
    sink = JsonlAuditSink()
    for index in range(80):
        sink.write(
            profile,
            {
                "event": "mysql_read_query",
                "connection": "prod",
                "query_id": f"id-{index}",
                "status": "success",
                "padding": "x" * 200,
            },
        )

    assert (tmp_path / "audit.jsonl").is_file()
    assert (tmp_path / "audit.jsonl.1").is_file()
    for path in tmp_path.glob("audit.jsonl*"):
        for line in path.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def test_missing_signing_key_is_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_AUDIT_KEY", raising=False)
    profile = audit_profile(tmp_path, audit_hmac_key_env="MISSING_AUDIT_KEY")

    with pytest.raises(AuditWriteError, match="MISSING_AUDIT_KEY"):
        JsonlAuditSink().write(profile, {"event": "mysql_read_query"})


def test_required_context_and_control_characters_are_enforced(tmp_path):
    profile = audit_profile(tmp_path, audit_required_context=("actor", "ticket_id"))
    incomplete = build_audit_context(
        {"actor": "alice"},
        request_id="request-1",
        operation="execute_sql",
    )
    with pytest.raises(ValueError, match="ticket_id"):
        validate_required_audit_context(profile, incomplete)

    with pytest.raises(ValueError, match="control characters"):
        build_audit_context(
            {"actor": "alice\nadmin"},
            request_id="request-1",
            operation="execute_sql",
        )
