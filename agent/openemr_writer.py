"""OpenEMR write-back client (Week 2, Step 2).

Separate from the read-only OpenEMRClient on purpose: writes use a distinct
OAuth client registered with user/document.write scope, and keeping them apart
preserves the read client's narrow, audited, read-only contract.

OpenEMR 7.0.3 rejects FHIR write scopes (user/Observation.write -> invalid_scope),
so derived facts are persisted via the standard REST API's document endpoint,
which the spec explicitly allows ("appropriate FHIR resources OR OpenEMR records").
Both the source PDF and a companion JSON of the extracted facts are written to the
patient's Medical Records, giving a traceable round-trip without duplicate records.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("agent.openemr_writer")

_TOKEN_PATH = "/oauth2/default/token"
_DOC_PATH = "/apis/default/api/patient/{pid}/document"
_PATIENT_LIST_PATH = "/apis/default/api/patient"
_CATEGORY = "/Medical Record"
_SCOPE = "openid api:oemr user/patient.read user/patient.write user/document.read user/document.write"


class OpenEMRWriteError(RuntimeError):
    """A write-back to OpenEMR failed. Non-fatal to ingestion by design."""


class OpenEMRWriter:
    """Persists ingested documents + derived facts to OpenEMR via the REST API."""

    def __init__(self) -> None:
        self._base = os.environ["OPENEMR_BASE_URL"].rstrip("/")
        self._client_id = os.environ["OPENEMR_WRITE_CLIENT_ID"]
        self._client_secret = os.environ["OPENEMR_WRITE_CLIENT_SECRET"]
        self._username = os.environ["OPENEMR_USERNAME"]
        self._password = os.environ["OPENEMR_PASSWORD"]
        self._timeout = float(os.environ.get("OPENEMR_TIMEOUT_SECONDS", "30"))

    @staticmethod
    def is_configured() -> bool:
        """True only if the write client creds are present. Lets /ingest skip
        write-back cleanly (and log it) rather than crash when unconfigured."""
        return bool(
            os.environ.get("OPENEMR_WRITE_CLIENT_ID")
            and os.environ.get("OPENEMR_WRITE_CLIENT_SECRET")
            and os.environ.get("OPENEMR_BASE_URL")
        )

    async def _token(self, http: httpx.AsyncClient) -> str:
        resp = await http.post(
            f"{self._base}{_TOKEN_PATH}",
            data={
                "grant_type": "password",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "user_role": "users",
                "username": self._username,
                "password": self._password,
                "scope": _SCOPE,
            },
        )
        if resp.status_code != 200:
            raise OpenEMRWriteError(f"token request failed (HTTP {resp.status_code})")
        token = resp.json().get("access_token")
        if not token:
            raise OpenEMRWriteError("token response had no access_token")
        return token

    async def _pid_for_uuid(self, http: httpx.AsyncClient, token: str, patient_uuid: str) -> int:
        """Map a FHIR UUID to the numeric pid the document endpoint needs."""
        resp = await http.get(
            f"{self._base}{_PATIENT_LIST_PATH}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            raise OpenEMRWriteError(f"patient list failed (HTTP {resp.status_code})")
        for row in resp.json().get("data", []):
            if row.get("uuid") == patient_uuid:
                return int(row["pid"])
        raise OpenEMRWriteError(f"no pid found for patient uuid {patient_uuid}")

    async def write_document(
        self, *, patient_uuid: str, filename: str, content: bytes, correlation_id: str
    ) -> str:
        """Upload one document to the patient's Medical Records. Returns the
        filename written. Raises OpenEMRWriteError on any failure."""
        async with httpx.AsyncClient(timeout=self._timeout, verify=True) as http:
            token = await self._token(http)
            pid = await self._pid_for_uuid(http, token, patient_uuid)
            url = f"{self._base}{_DOC_PATH.format(pid=pid)}"
            resp = await http.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"path": _CATEGORY},
                files={"document": (filename, content)},
            )
            if resp.status_code != 200 or resp.text.strip().lower().startswith("<"):
                raise OpenEMRWriteError(
                    f"document write failed (HTTP {resp.status_code}) for pid {pid}"
                )
            logger.info(
                "openemr_write_ok",
                extra={"event": "openemr_write_ok", "correlation_id": correlation_id,
                       "pid": pid, "doc_filename": filename},
            )
            return filename




