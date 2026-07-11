"""Append-only access audit log for the Clinical Co-Pilot HTTP layer.

Records WHO accessed WHICH patient (opaque UUID) and WHEN — never chart
contents, questions, answers, or other clinical data. Mirrors the AUDIT.md
finding that OpenEMR persisted full API response bodies; this store is
metadata-only by design.

Persistence path is fully controlled by the ``AUDIT_LOG_PATH`` environment
variable (default ``agent/audit_log.jsonl`` relative to the repo root).
For Railway, mount a persistent volume (e.g. at ``/data``) and set
``AUDIT_LOG_PATH=/data/audit_log.jsonl`` so events survive redeploys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_ENV_AUDIT_LOG_PATH: Final[str] = "AUDIT_LOG_PATH"
_DEFAULT_RELATIVE_PATH: Final[str] = "agent/audit_log.jsonl"

AuditEventType = Literal["login", "patient_access", "access_refused", "logout"]


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

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._ensure_parent_dir()

    def _ensure_parent_dir(self) -> None:
        """Create the parent directory if needed (e.g. ``/data`` on a Railway volume)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning(
                "audit_log_dir_permission_denied",
                extra={"event": "audit_log_dir_permission_denied", "path": str(self._path.parent)},
            )
        except OSError as exc:
            logger.warning(
                "audit_log_dir_unavailable",
                extra={"event": "audit_log_dir_unavailable", "path": str(self._path.parent), "error": str(exc)},
            )

    @classmethod
    def from_env(cls) -> AuditTrail:
        raw = os.environ.get(_ENV_AUDIT_LOG_PATH, _DEFAULT_RELATIVE_PATH).strip()
        return cls(_resolve_audit_log_path(raw))

    def append_sync(
        self,
        *,
        username: str,
        event_type: AuditEventType,
        outcome: str,
        correlation_id: str,
        patient_id: str | None = None,
    ) -> None:
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
            self._ensure_parent_dir()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except PermissionError:
            logger.warning(
                "audit_log_write_permission_denied",
                extra={"event": "audit_log_write_permission_denied", "path": str(self._path)},
            )
        except OSError as exc:
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


def _resolve_audit_log_path(raw: str) -> Path:
    """Resolve ``AUDIT_LOG_PATH``: absolute paths are used as-is; relative paths are repo-rooted."""
    path = Path(raw)
    if path.is_absolute():
        return path
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / path
