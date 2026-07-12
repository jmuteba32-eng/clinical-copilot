"""Append-only access audit log for the Clinical Co-Pilot HTTP layer.

Records WHO accessed WHICH patient (opaque UUID) and WHEN — never chart
contents, questions, answers, or other clinical data. Mirrors the AUDIT.md
finding that OpenEMR persisted full API response bodies; this store is
metadata-only by design.

Persistence path is controlled by ``AUDIT_LOG_PATH``. At startup the app
probes whether that path is writable; on ``PermissionError`` it falls back to
``/tmp`` (or ``TMPDIR``) so the service never crashes when a Railway volume
is root-owned. Structured JSON logs still record ``audit_log_path_fallback``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_ENV_AUDIT_LOG_PATH: Final[str] = "AUDIT_LOG_PATH"
_DEFAULT_RELATIVE_PATH: Final[str] = "agent/audit_log.jsonl"
_FALLBACK_FILENAME: Final[str] = "clinical-copilot-audit.jsonl"
_VOLUME_SUBDIR_NAME: Final[str] = "clinical-copilot-audit"

AuditEventType = Literal["login", "patient_access", "access_refused", "logout", "baa_gate_blocked"]


class AuditRecord(BaseModel):
    """One immutable access event — no PHI fields."""

    timestamp: str = Field(..., description="UTC ISO-8601 time the event was recorded.")
    username: str = Field(..., description="Authenticated UI user, or 'unauthenticated' for API-only calls.")
    event_type: AuditEventType
    patient_id: str | None = Field(
        default=None,
        description="Opaque FHIR Patient resource UUID when the event concerns a chart.",
    )
    correlation_id: str = Field(..., description="Request correlation ID for joining to server logs.")
    outcome: str = Field(..., description="Result of the event (e.g. success, failure, refused).")


class AuditTrail:
    """Thread-safe append-only JSONL audit store."""

    def __init__(self, path: Path, *, writable: bool = True) -> None:
        self._path = path
        self._writable = writable
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> AuditTrail:
        """Build an audit trail, never raising — falls back if the preferred path is not writable."""
        raw = os.environ.get(_ENV_AUDIT_LOG_PATH, _DEFAULT_RELATIVE_PATH).strip()
        preferred = _resolve_audit_log_path(raw)
        path, using_preferred = _resolve_writable_path(preferred)
        if using_preferred:
            logger.info(
                "audit_log_ready",
                extra={"event": "audit_log_ready", "path": str(path), "preferred": True},
            )
            return cls(path, writable=True)
        if path is not None:
            logger.warning(
                "audit_log_path_fallback",
                extra={
                    "event": "audit_log_path_fallback",
                    "preferred_path": str(preferred),
                    "active_path": str(path),
                },
            )
            return cls(path, writable=True)
        logger.warning(
            "audit_log_writes_disabled",
            extra={
                "event": "audit_log_writes_disabled",
                "preferred_path": str(preferred),
                "reason": "no_writable_audit_path",
            },
        )
        return cls(preferred, writable=False)

    def append_sync(
        self,
        *,
        username: str,
        event_type: AuditEventType,
        outcome: str,
        correlation_id: str,
        patient_id: str | None = None,
    ) -> None:
        if not self._writable:
            return
        record = AuditRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            username=username,
            event_type=event_type,
            patient_id=patient_id,
            correlation_id=correlation_id,
            outcome=outcome,
        )
        line = record.model_dump_json() + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except PermissionError:
            self._writable = False
            logger.warning(
                "audit_log_write_permission_denied",
                extra={"event": "audit_log_write_permission_denied", "path": str(self._path)},
            )
        except OSError as exc:
            self._writable = False
            logger.warning(
                "audit_log_write_failed",
                extra={"event": "audit_log_write_failed", "path": str(self._path), "error": str(exc)},
            )

    async def append(
        self,
        *,
        username: str,
        event_type: AuditEventType,
        outcome: str,
        correlation_id: str,
        patient_id: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self.append_sync,
                username=username,
                event_type=event_type,
                outcome=outcome,
                correlation_id=correlation_id,
                patient_id=patient_id,
            )

    async def read_records(self) -> list[AuditRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._read_records_sync)

    def _read_records_sync(self) -> list[AuditRecord]:
        try:
            if not self._path.is_file():
                return []
            records: list[AuditRecord] = []
            with self._path.open(encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        records.append(AuditRecord.model_validate_json(stripped))
                    except Exception:
                        logger.warning(
                            "audit_log_parse_skipped",
                            extra={"event": "audit_log_parse_skipped", "line_no": line_no},
                        )
            records.sort(key=lambda r: r.timestamp, reverse=True)
            return records
        except PermissionError:
            logger.warning(
                "audit_log_read_permission_denied",
                extra={"event": "audit_log_read_permission_denied", "path": str(self._path)},
            )
            return []
        except OSError as exc:
            logger.warning(
                "audit_log_read_failed",
                extra={"event": "audit_log_read_failed", "path": str(self._path), "error": str(exc)},
            )
            return []


def _resolve_audit_log_path(raw: str) -> Path:
    """Resolve ``AUDIT_LOG_PATH``: absolute paths are used as-is; relative paths are repo-rooted."""
    path = Path(raw)
    if path.is_absolute():
        return path
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / path


def _tmp_fallback_path() -> Path:
    return Path(tempfile.gettempdir()) / _FALLBACK_FILENAME


def _home_fallback_path() -> Path:
    return Path.home() / ".clinical-copilot" / _FALLBACK_FILENAME


def _volume_subdir_fallback(preferred: Path) -> Path | None:
    """If the preferred path targets a volume mount, try a subdir the app may own."""
    parts = preferred.parts
    if len(parts) >= 2 and parts[1] == "data":
        return Path("/data") / _VOLUME_SUBDIR_NAME / _FALLBACK_FILENAME
    return None


def _probe_writable(path: Path) -> bool:
    """Return True if ``path`` can be opened for append without raising."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def _resolve_writable_path(preferred: Path) -> tuple[Path | None, bool]:
    """Pick the first writable candidate. Returns (path, using_preferred)."""
    candidates: list[Path] = [preferred]
    subdir = _volume_subdir_fallback(preferred)
    if subdir is not None:
        candidates.append(subdir)
    candidates.extend((_home_fallback_path(), _tmp_fallback_path()))
    seen: set[Path] = set()
    for index, candidate in enumerate(candidates):
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _probe_writable(candidate):
            return candidate, index == 0
    return None, False

