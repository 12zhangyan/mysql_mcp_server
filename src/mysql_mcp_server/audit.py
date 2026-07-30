"""Structured, credential-safe and optionally durable audit events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ConnectionProfile

AUDIT_SCHEMA_VERSION = 1
AUDIT_CONTEXT_FIELDS = {"actor", "purpose", "ticket_id"}


class AuditWriteError(RuntimeError):
    """Raised when a fail-closed durable audit sink cannot be written."""


@dataclass(frozen=True)
class AuditContext:
    request_id: str
    operation: str
    client_name: str | None = None
    client_version: str | None = None
    actor: str | None = None
    purpose: str | None = None
    ticket_id: str | None = None


_audit_context: ContextVar[AuditContext | None] = ContextVar(
    "mysql_mcp_audit_context", default=None
)


def _clean_context_value(value: Any, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"audit_context.{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise ValueError(f"audit_context.{field} must not exceed {maximum} characters")
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError(f"audit_context.{field} must not contain control characters")
    return cleaned


def build_audit_context(
    value: Any,
    *,
    request_id: str,
    operation: str,
    client_name: str | None = None,
    client_version: str | None = None,
) -> AuditContext:
    if value is None:
        values: dict[str, Any] = {}
    elif isinstance(value, dict):
        values = value
    else:
        raise ValueError("audit_context must be an object")
    unknown = sorted(set(values) - AUDIT_CONTEXT_FIELDS)
    if unknown:
        raise ValueError(
            "audit_context contains unsupported fields: " + ", ".join(unknown)
        )
    return AuditContext(
        request_id=request_id,
        operation=operation,
        client_name=client_name,
        client_version=client_version,
        actor=_clean_context_value(values.get("actor"), field="actor", maximum=128),
        purpose=_clean_context_value(
            values.get("purpose"), field="purpose", maximum=256
        ),
        ticket_id=_clean_context_value(
            values.get("ticket_id"), field="ticket_id", maximum=128
        ),
    )


def set_audit_context(context: AuditContext) -> Token[AuditContext | None]:
    return _audit_context.set(context)


def reset_audit_context(token: Token[AuditContext | None]) -> None:
    _audit_context.reset(token)


def current_audit_context(operation: str = "direct_query") -> AuditContext:
    context = _audit_context.get()
    if context is not None:
        return context
    return AuditContext(request_id=uuid.uuid4().hex, operation=operation)


def validate_required_audit_context(
    profile: ConnectionProfile, context: AuditContext
) -> None:
    missing = [
        field
        for field in profile.audit_required_context
        if not getattr(context, field, None)
    ]
    if missing:
        raise ValueError(
            "Missing required audit_context fields for connection "
            f"'{profile.name}': {', '.join(missing)}"
        )


class JsonlAuditSink:
    """Thread-safe JSONL sink with size rotation and optional HMAC signatures."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @staticmethod
    def _canonical_json(event: dict[str, Any]) -> str:
        return json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def prepare_event(
        self,
        profile: ConnectionProfile,
        fields: dict[str, Any],
        *,
        sign: bool = True,
    ) -> dict[str, Any]:
        context = current_audit_context()
        event: dict[str, Any] = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "event_id": uuid.uuid4().hex,
            "request_id": context.request_id,
            "operation": context.operation,
            **fields,
        }
        optional_context = {
            "client_name": context.client_name,
            "client_version": context.client_version,
            "actor": context.actor,
            "purpose": context.purpose,
            "ticket_id": context.ticket_id,
        }
        event.update(
            {key: value for key, value in optional_context.items() if value is not None}
        )
        if sign and profile.audit_hmac_key_env:
            secret = os.getenv(profile.audit_hmac_key_env)
            if not secret:
                raise AuditWriteError(
                    f"Audit signing environment variable "
                    f"'{profile.audit_hmac_key_env}' is missing"
                )
            event["signature_algorithm"] = "hmac-sha256"
            event["signature"] = hmac.new(
                secret.encode("utf-8"),
                self._canonical_json(event).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return event

    @staticmethod
    def _rotate(path: Path, backup_count: int) -> None:
        oldest = path.with_name(f"{path.name}.{backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(backup_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        if path.exists():
            path.replace(path.with_name(f"{path.name}.1"))

    def preflight(self, profile: ConnectionProfile) -> None:
        """Verify fail-closed audit dependencies before database access."""
        if profile.audit_hmac_key_env and not os.getenv(profile.audit_hmac_key_env):
            raise AuditWriteError(
                f"Audit signing environment variable "
                f"'{profile.audit_hmac_key_env}' is missing"
            )
        if not profile.audit_fail_closed:
            return
        if not profile.audit_log_file:
            raise AuditWriteError("Fail-closed audit requires a durable log file")
        path = Path(profile.audit_log_file).expanduser().resolve()
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8"):
                    pass
        except OSError as exc:
            raise AuditWriteError(
                f"Durable audit log is not writable ({type(exc).__name__})"
            ) from exc

    def write(
        self, profile: ConnectionProfile, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        event = self.prepare_event(profile, fields)
        serialized = self._canonical_json(event)
        if not profile.audit_log_file:
            return event, serialized

        path = Path(profile.audit_log_file).expanduser().resolve()
        encoded_size = len(serialized.encode("utf-8")) + 1
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if (
                    path.exists()
                    and path.stat().st_size + encoded_size > profile.audit_log_max_bytes
                ):
                    self._rotate(path, profile.audit_log_backup_count)
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(serialized + "\n")
                    stream.flush()
                    if profile.audit_fsync:
                        os.fsync(stream.fileno())
        except OSError as exc:
            raise AuditWriteError(
                f"Durable audit log could not be written ({type(exc).__name__})"
            ) from exc
        return event, serialized


audit_sink = JsonlAuditSink()
