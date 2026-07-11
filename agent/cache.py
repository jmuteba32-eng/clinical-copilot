"""Encrypted in-memory PHI cache for the four first-visit FHIR tool results.

WHY this exists (AUDIT.md performance finding + compliance implication):
OpenEMR has no caching layer; assembling one patient's picture costs an
estimated 10–40+ FHIR round-trips. On Railway's free tier that pushed tool
p95 past ~90s — longer than the physician's between-rooms budget in
USERS.md. Caching repeated reads of the same patient_id is the direct fix.

WHY encryption + short TTL are non-negotiable here:
Caching PHI outside OpenEMR creates a *new* PHI store that inherits none of
OpenEMR's existing protections — no `EventAuditLogger` trail per read, no
OpenEMR ACL enforcement on the bytes sitting in RAM, no field-level
encryption from `CryptoGen`, no retention policy from the platform. AUDIT.md
explicitly warns that an agent-side cache "needs its own encryption-at-rest,
access control, and expiry." This module supplies the first two for the
in-process store: values are Fernet-encrypted before they ever sit in the
cache dict (never plaintext PHI in the structure), and entries are deleted
on TTL expiry — not merely marked stale. The cache *key* is patient_id
only; clinical content never appears in keys.

Access control is NOT delegated to the cache: `main.py`'s /ask handler still
runs `patient_exists()` and OpenEMR auth on every request before a cache
lookup. A cache hit skips OpenEMR *tool* round-trips, not authorization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

from agent.openemr_client import OpenEMRClient
from agent.schemas import DiagnosisRecord, IntakeRecord, MedicationRecord, SourcedRecord, TestingRecord, ToolResult
from agent.tools import get_diagnosis, get_intake, get_testing, get_treatment

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS: Final[int] = 300
_ENV_TTL: Final[str] = "CACHE_TTL_SECONDS"
_ENV_KEY: Final[str] = "CACHE_ENCRYPTION_KEY"

# The four parallel first-visit tools ARCHITECTURE.md section 2 names; this
# cache only covers that set — not future tools like get_visit.
CHART_TOOL_NAMES: Final[tuple[str, ...]] = (
    "get_intake",
    "get_diagnosis",
    "get_treatment",
    "get_testing",
)


class CachedPatientChart(BaseModel):
    """Assembled ToolResults from the four read-only chart tools."""

    get_intake: ToolResult[IntakeRecord]
    get_diagnosis: ToolResult[DiagnosisRecord]
    get_treatment: ToolResult[MedicationRecord]
    get_testing: ToolResult[TestingRecord]

    def as_prefetch_map(self) -> dict[str, ToolResult[Any]]:
        """Map tool name -> ToolResult for `ClinicalCoPilot._dispatch_tool_call`."""
        return {
            "get_intake": self.get_intake,
            "get_diagnosis": self.get_diagnosis,
            "get_treatment": self.get_treatment,
            "get_testing": self.get_testing,
        }

    def all_records(self) -> list[SourcedRecord]:
        """Every SourcedRecord across the four first-visit tools — the session's known chart."""
        records: list[SourcedRecord] = []
        for tool_result in (
            self.get_intake,
            self.get_diagnosis,
            self.get_treatment,
            self.get_testing,
        ):
            records.extend(tool_result.records)
        return records


class PatientChartCache:
    """In-memory, Fernet-encrypted, TTL-bounded cache keyed by patient_id."""

    def __init__(self, *, fernet: Fernet, ttl_seconds: float) -> None:
        self._fernet = fernet
        self._ttl_seconds = ttl_seconds
        # patient_id -> (monotonic expiry time, encrypted blob). Key is the
        # opaque FHIR id only — never clinical content.
        self._entries: dict[str, tuple[float, bytes]] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return True

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @classmethod
    def from_env(cls) -> PatientChartCache | None:
        """Build a cache from env, or return None if encryption is not configured.

        Without `CACHE_ENCRYPTION_KEY`, caching is disabled entirely rather
        than storing plaintext PHI — see module docstring.
        """
        raw_key = os.environ.get(_ENV_KEY)
        if not raw_key or not raw_key.strip():
            logger.warning(
                "chart_cache_disabled",
                extra={
                    "event": "chart_cache_disabled",
                    "reason": f"{_ENV_KEY} is not set; PHI will not be cached",
                },
            )
            return None

        try:
            fernet = Fernet(raw_key.strip().encode("ascii"))
        except (ValueError, TypeError) as exc:
            logger.error(
                "chart_cache_disabled",
                extra={
                    "event": "chart_cache_disabled",
                    "reason": f"{_ENV_KEY} is not a valid Fernet key",
                },
            )
            raise ValueError(f"{_ENV_KEY} must be a Fernet key (use Fernet.generate_key()).") from exc

        raw_ttl = os.environ.get(_ENV_TTL)
        ttl_seconds = _DEFAULT_TTL_SECONDS
        if raw_ttl is not None and raw_ttl.strip():
            try:
                ttl_seconds = float(raw_ttl.strip())
            except ValueError as exc:
                raise ValueError(f"{_ENV_TTL} must be a number, got {raw_ttl!r}.") from exc
            if ttl_seconds <= 0:
                raise ValueError(f"{_ENV_TTL} must be positive, got {ttl_seconds}.")

        return cls(fernet=fernet, ttl_seconds=ttl_seconds)

    async def get(self, patient_id: str) -> CachedPatientChart | None:
        """Return a fresh chart if cached and not expired; else None.

        Expired entries are deleted immediately — not left as stale tombstones.
        """
        now = time.monotonic()
        async with self._lock:
            entry = self._entries.get(patient_id)
            if entry is None:
                return None
            expires_at, ciphertext = entry
            if now >= expires_at:
                del self._entries[patient_id]
                return None
            blob = ciphertext

        try:
            plaintext = self._fernet.decrypt(blob)
        except InvalidToken:
            # Key rotation or corruption — drop the entry rather than serve
            # garbage or crash the request.
            async with self._lock:
                self._entries.pop(patient_id, None)
            logger.warning(
                "chart_cache_decrypt_failed",
                extra={"event": "chart_cache_decrypt_failed", "patient_id": patient_id},
            )
            return None

        return CachedPatientChart.model_validate_json(plaintext.decode("utf-8"))

    async def put(self, patient_id: str, chart: CachedPatientChart) -> None:
        """Encrypt and store a chart; overwrites any prior entry for this patient."""
        ciphertext = self._fernet.encrypt(chart.model_dump_json().encode("utf-8"))
        expires_at = time.monotonic() + self._ttl_seconds
        async with self._lock:
            self._entries[patient_id] = (expires_at, ciphertext)


async def refresh_chart_diagnosis(
    client: OpenEMRClient,
    patient_id: str,
    chart: CachedPatientChart,
) -> CachedPatientChart:
    """Re-fetch get_diagnosis only — diagnoses change in-chart more often than demographics/meds.

    The encrypted chart cache (300s TTL by default) can otherwise serve stale
    Condition ids/names after an OpenEMR edit until the whole entry expires.
    Intake, medications, and testing stay cached; only the problem list is
    refreshed on each session load so recap and verification see current data.
    """
    diagnosis = await get_diagnosis(client, patient_id)
    return chart.model_copy(update={"get_diagnosis": diagnosis})


async def fetch_patient_chart(client: OpenEMRClient, patient_id: str) -> CachedPatientChart:
    """Run the four first-visit tools in parallel (same pattern as agent.py)."""
    intake, diagnosis, treatment, testing = await asyncio.gather(
        get_intake(client, patient_id),
        get_diagnosis(client, patient_id),
        get_treatment(client, patient_id),
        get_testing(client, patient_id),
    )
    return CachedPatientChart(
        get_intake=intake,
        get_diagnosis=diagnosis,
        get_treatment=treatment,
        get_testing=testing,
    )
