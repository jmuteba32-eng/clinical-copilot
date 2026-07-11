"""Unit tests for audit trail persistence (no live API)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent.audit_trail import AuditTrail, _probe_writable, _resolve_audit_log_path, _resolve_writable_path


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


def test_from_env_falls_back_when_preferred_not_writable(tmp_path: Path) -> None:
    preferred = tmp_path / "locked" / "audit_log.jsonl"
    fallback = tmp_path / "writable" / "clinical-copilot-audit.jsonl"

    def fake_probe(path: Path) -> bool:
        return path.resolve() == fallback.resolve()

    with (
        patch.dict("os.environ", {"AUDIT_LOG_PATH": str(preferred)}, clear=False),
        patch("agent.audit_trail._probe_writable", side_effect=fake_probe),
        patch("agent.audit_trail._home_fallback_path", return_value=fallback),
        patch("agent.audit_trail._tmp_fallback_path", return_value=fallback),
    ):
        trail = AuditTrail.from_env()
    assert trail._path.resolve() == fallback.resolve()
    assert trail._writable is True


def test_from_env_never_raises_when_no_path_writable(tmp_path: Path) -> None:
    preferred = tmp_path / "audit_log.jsonl"
    with (
        patch.dict("os.environ", {"AUDIT_LOG_PATH": str(preferred)}, clear=False),
        patch("agent.audit_trail._probe_writable", return_value=False),
    ):
        trail = AuditTrail.from_env()
    assert trail._writable is False


def test_append_permission_error_disables_writes_without_raising(tmp_path: Path) -> None:
    trail = AuditTrail(tmp_path / "audit_log.jsonl")
    with patch.object(Path, "open", side_effect=PermissionError("denied")):
        trail.append_sync(
            username="admin",
            event_type="login",
            outcome="success",
            correlation_id="00000000-0000-0000-0000-000000000001",
        )
    assert trail._writable is False


def test_probe_writable_on_real_tmp_path() -> None:
    path = Path(__import__("tempfile").gettempdir()) / "clinical-copilot-probe-test.jsonl"
    try:
        assert _probe_writable(path) is True
    finally:
        path.unlink(missing_ok=True)


def test_resolve_writable_path_prefers_first_working_candidate(tmp_path: Path) -> None:
    preferred = tmp_path / "preferred" / "audit_log.jsonl"
    chosen, using_preferred = _resolve_writable_path(preferred)
    assert using_preferred is True
    assert chosen == preferred
