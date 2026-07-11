"""Unit tests for audit trail persistence (no live API)."""

from __future__ import annotations

from pathlib import Path

from agent.audit_trail import AuditTrail, _resolve_audit_log_path


def test_absolute_audit_log_path_used_as_is() -> None:
    resolved = _resolve_audit_log_path("/data/audit_log.jsonl")
    assert resolved.is_absolute()
    assert resolved.name == "audit_log.jsonl"
    assert resolved.parent.name == "data"


def test_read_missing_file_returns_empty_list(tmp_path: Path) -> None:
    trail = AuditTrail(tmp_path / "nested" / "audit_log.jsonl")
    assert trail._read_records_sync() == []


def test_append_creates_parent_and_file(tmp_path: Path) -> None:
    log_path = tmp_path / "data" / "audit_log.jsonl"
    trail = AuditTrail(log_path)
    trail.append_sync(
        username="admin",
        event_type="login",
        outcome="success",
        correlation_id="00000000-0000-0000-0000-000000000001",
    )
    assert log_path.is_file()
    records = trail._read_records_sync()
    assert len(records) == 1
    assert records[0].username == "admin"
    assert records[0].event_type == "login"
