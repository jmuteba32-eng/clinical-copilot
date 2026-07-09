"""The only module in this codebase permitted to talk to OpenEMR.

Every other module -- tools, the agent orchestrator, the FastAPI routes --
must go through :class:`OpenEMRClient`. Concentrating all OpenEMR I/O in one
module means there is exactly one place that can violate the access-control
guarantees this project depends on, and exactly one place to audit for that.

Why this ALWAYS goes through the authenticated REST/FHIR path
----------------------------------------------------------------
OpenEMR has two Policy Enforcement Points (PEPs) for a normal HTTP request:
(1) OAuth2 bearer-token validation and (2) scope/ACL checks
(`src/RestControllers/Subscriber/AuthorizationListener.php`). But OpenEMR
also has "local API" / `skipAuthorization` code paths
(`LocalApiAuthorizationController`, `RestConfig::setLocalCall()`, and
`SkipAuthorizationStrategy`'s unauthenticated allowlist) meant for trusted,
in-process, non-user-facing callers -- and those paths skip BOTH PEPs
entirely, plus the audit trail that `EventAuditLogger` records for a normal
API hit.

This client deliberately never touches those shortcuts. It only ever speaks
HTTP to `/oauth2/default/token` and `/apis/default/fhir/*`, the same paths
any external, audited API consumer would use. That is a deliberate
trade-off of speed for correctness: this agent reads real patient data on
behalf of a real physician, and "access must be scoped and audited" is not
negotiable for that. See AUDIT.md ("Auth is sound but bypassable") and
ARCHITECTURE.md ("Access enforced at the data layer") for the fuller
reasoning -- the LLM is never the security boundary; OpenEMR's own PEPs are.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
#
# Four distinct failure categories, not one generic "OpenEMRError". The
# reason is clinical, not stylistic: "the record doesn't exist" and "we
# couldn't ask OpenEMR" are different facts that tools must report
# differently (ToolResult.missing vs ToolResult.error -- see schemas.py).
# Collapsing them into one exception type would force every caller to
# re-derive that distinction from a status code, and it is easy to get
# that derivation wrong in exactly the place where getting it wrong matters
# most (silently telling a physician "no records" when the real story is
# "the network timed out").


class OpenEMRClientError(Exception):
    """Base class for every error this module raises.

    Never raised directly -- always one of the subclasses below, so a
    caller doing ``except OpenEMRClientError`` to log-and-reraise still gets
    a specific, typed cause via ``type(exc)`` or an ``isinstance`` check.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenEMRConfigError(OpenEMRClientError):
    """Required configuration (env vars) is missing or malformed.

    Raised at client construction time, before any network call. Kept
    distinct from the runtime errors below because a misconfigured
    deployment should fail fast and loudly at startup, not surface as a
    confusing per-request auth failure once a physician is already using
    the agent.
    """


class OpenEMRAuthError(OpenEMRClientError):
    """OpenEMR was reached and responded, but refused the request.

    Covers: bad resource-owner credentials or client credentials at the
    token endpoint, and a bearer token rejected or found lacking the
    required scope/ACL grant at the FHIR endpoint (HTTP 401/403).

    This means OpenEMR's two Policy Enforcement Points did their job -- the
    request was evaluated and denied. It is NOT evidence about whether the
    requested record exists (that is :class:`OpenEMRNotFoundError`) and it
    is NOT a network problem (that is :class:`OpenEMRConnectionError`).
    """


class OpenEMRNotFoundError(OpenEMRClientError):
    """OpenEMR authenticated the request and confirmed the resource is absent.

    Maps to HTTP 404 from the FHIR API. This is a *clinical fact about the
    chart* (e.g. "this patient has no DiagnosticReport"), not a failure of
    the tool. Callers MUST translate this into ``ToolResult(missing=True)``,
    never into ``ToolResult(error=...)`` -- conflating the two would either
    hide a real gap from the physician or falsely claim a gap when the tool
    actually broke.
    """


class OpenEMRConnectionError(OpenEMRClientError):
    """OpenEMR's answer, if any, was never received.

    Covers timeouts, DNS failures, connection refusals, and TLS errors --
    anything where no HTTP response came back at all. Because no response
    exists, this says nothing about whether the record exists; callers MUST
    translate this into ``ToolResult(error=...)``, never into
    ``ToolResult(missing=True)``.
    """


class OpenEMRAPIError(OpenEMRClientError):
    """OpenEMR responded with an error status that isn't one of the above.

    Covers malformed requests (400), server errors (5xx), and anything else
    outside the auth/not-found/network buckets. Kept separate so a
    genuinely unexpected server-side failure is never silently mistaken for
    "no data" or "access denied".
    """


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenEMRConfig:
    """Immutable connection settings for one OpenEMR site.

    A plain dataclass (not the ``OpenEMRClient`` reading os.environ inline)
    so tests can construct one by hand with fake values, per this project's
    dependency-injection convention: business logic should receive its
    configuration, not reach into global state itself. :meth:`from_env` is
    the *default* way to build one, not the only way.
    """

    base_url: str
    client_id: str
    client_secret: str
    username: str
    password: str

    @classmethod
    def from_env(cls) -> OpenEMRConfig:
        """Build config from OPENEMR_* environment variables.

        Validates all five variables up front and reports every missing one
        in a single error, rather than failing on the first ``KeyError`` --
        a deployment with three missing variables should not require three
        separate failed-startup-and-fix cycles to diagnose.
        """
        env_to_field = {
            "OPENEMR_BASE_URL": "base_url",
            "OPENEMR_CLIENT_ID": "client_id",
            "OPENEMR_CLIENT_SECRET": "client_secret",
            "OPENEMR_USERNAME": "username",
            "OPENEMR_PASSWORD": "password",
        }
        values: dict[str, str] = {}
        missing: list[str] = []
        for env_var, field_name in env_to_field.items():
            value = os.environ.get(env_var)
            if not value:
                missing.append(env_var)
            else:
                values[field_name] = value

        if missing:
            raise OpenEMRConfigError(
                "Missing required environment variable(s): "
                f"{', '.join(missing)}. Set them (e.g. in agent/.env, loaded "
                "before this client is constructed) before starting the agent."
            )

        return cls(
            base_url=values["base_url"].rstrip("/"),
            client_id=values["client_id"],
            client_secret=values["client_secret"],
            username=values["username"],
            password=values["password"],
        )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class OpenEMRClient:
    """Authenticated async client for OpenEMR's OAuth2 + FHIR REST API.

    One instance is meant to be constructed once (e.g. as a FastAPI
    dependency / app-lifespan singleton) and reused across requests -- the
    in-memory token cache and the ``asyncio.Lock`` guarding refreshes only
    provide their benefit (avoiding a fresh login per request, and avoiding
    a stampede of concurrent re-logins) if the same instance is shared.
    """

    _TOKEN_PATH: Final[str] = "/oauth2/default/token"
    _FHIR_PATH_TEMPLATE: Final[str] = "/apis/default/fhir/{resource}"
    _TIMEOUT_SECONDS: Final[float] = 10.0
    # Refresh a little before actual expiry so a token that is valid at the
    # moment we check it doesn't expire mid-flight while the FHIR request is
    # still in transit.
    _EXPIRY_BUFFER: Final[timedelta] = timedelta(seconds=30)
    _USER_ROLE: Final[str] = "users"  # provider/staff login, not "patient" portal role.

    # Deliberately narrow: read-only (`.read`, never `.write`/`.cruds`) and
    # resource-scoped (`user/Condition.read`, never a `user/*.read` or
    # `system/*` wildcard) -- the least privilege this agent's read-only
    # tool set needs, per AUDIT.md's "request the narrowest scope" guidance.
    # `offline_access` is requested so a refresh token is issued, letting a
    # long-lived agent process renew its session without re-sending the
    # resource owner's password on every expiry.
    SCOPE: Final[str] = (
        "openid fhirUser offline_access api:fhir "
        "user/Patient.read user/Condition.read user/Encounter.read "
        "user/MedicationRequest.read user/Observation.read user/DiagnosticReport.read"
    )

    def __init__(
        self,
        config: OpenEMRConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a client.

        Both arguments are optional so ``OpenEMRClient()`` works out of the
        box in the running agent (reading env vars, owning its own HTTP
        client), while tests can inject a fake ``OpenEMRConfig`` and an
        ``httpx.AsyncClient`` wired to a mock transport instead of reaching
        the network at all.
        """
        self._config = config or OpenEMRConfig.from_env()
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(self._TIMEOUT_SECONDS))
        # Only close a client we created ourselves -- closing one the
        # caller injected would be a surprising side effect on their object.
        self._owns_http_client = http_client is None

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: datetime | None = None
        # Guards token fetch/refresh so concurrent requests sharing one
        # client instance don't each independently notice an expired token
        # and all fire off simultaneous re-logins.
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> OpenEMRClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool, if we own it."""
        if self._owns_http_client:
            await self._http.aclose()

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    async def get_fhir(self, resource: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch a FHIR resource or search result.

        ``resource`` is the path segment appended to the FHIR base, e.g.
        ``"Condition"`` for a search (paired with ``params={"patient": ...}``)
        or ``"Condition/<id>"`` for a direct read. This is the ONLY method
        tools should use to reach OpenEMR -- it always resolves to
        ``{base_url}/apis/default/fhir/{resource}``, the audited REST/FHIR
        path, never a local/in-process shortcut (see module docstring).

        Raises one of :class:`OpenEMRAuthError`, :class:`OpenEMRNotFoundError`,
        :class:`OpenEMRConnectionError`, or :class:`OpenEMRAPIError` on
        failure -- never returns a partial/empty dict to signal an error,
        since that would be indistinguishable from a genuinely empty FHIR
        Bundle.
        """
        token = await self._get_valid_access_token()
        url = f"{self._config.base_url}{self._FHIR_PATH_TEMPLATE.format(resource=resource)}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/fhir+json",
        }

        try:
            response = await self._http.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise OpenEMRConnectionError(f"Timed out fetching '{resource}' from OpenEMR after {self._TIMEOUT_SECONDS}s.") from exc
        except httpx.RequestError as exc:
            raise OpenEMRConnectionError(f"Network error fetching '{resource}' from OpenEMR: {exc!r}") from exc

        logger.debug("OpenEMR FHIR request resource=%s status=%s", resource, response.status_code)

        if response.status_code in (401, 403):
            raise OpenEMRAuthError(
                f"OpenEMR denied access to '{resource}' (HTTP {response.status_code}): "
                "the bearer token was rejected or lacks the required scope/ACL grant.",
                status_code=response.status_code,
            )
        if response.status_code == 404:
            raise OpenEMRNotFoundError(
                f"No '{resource}' resource found on OpenEMR (HTTP 404).",
                status_code=404,
            )
        if response.status_code >= 400:
            raise OpenEMRAPIError(
                f"OpenEMR returned an unexpected error for '{resource}' (HTTP {response.status_code}).",
                status_code=response.status_code,
            )

        return response.json()

    # ----------------------------------------------------------------
    # Token management
    # ----------------------------------------------------------------

    async def _get_valid_access_token(self) -> str:
        """Return a token known to be valid, authenticating/refreshing first if needed."""
        async with self._token_lock:
            if self._access_token is None or self._is_token_expired():
                if self._refresh_token is not None:
                    try:
                        await self._refresh()
                    except OpenEMRAuthError:
                        # Refresh token itself may have expired/been revoked
                        # (offline_access refresh tokens outlive access
                        # tokens but are not immortal) -- fall back to a
                        # full password re-authentication rather than
                        # propagating a refresh-specific failure.
                        await self._authenticate()
                else:
                    await self._authenticate()

            if self._access_token is None:
                # Unreachable in practice -- _authenticate/_refresh always
                # set this or raise -- but handled explicitly rather than
                # asserted, per this project's null-safety convention.
                raise OpenEMRAuthError("Failed to obtain an OpenEMR access token.")
            return self._access_token

    def _is_token_expired(self) -> bool:
        if self._token_expires_at is None:
            return True
        return datetime.now(timezone.utc) >= (self._token_expires_at - self._EXPIRY_BUFFER)

    async def _authenticate(self) -> None:
        """Full OAuth2 password-grant login using the resource owner's credentials."""
        payload = {
            "grant_type": "password",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "scope": self.SCOPE,
            "user_role": self._USER_ROLE,
            "username": self._config.username,
            "password": self._config.password,
        }
        token_data = await self._request_token(payload)
        self._store_token(token_data)

    async def _refresh(self) -> None:
        """Renew the access token using the cached refresh token.

        Preferred over :meth:`_authenticate` for routine renewal because it
        never re-sends the resource owner's password over the wire once the
        initial login has happened.
        """
        if self._refresh_token is None:
            raise OpenEMRAuthError("No refresh token cached; a full re-authentication is required.")
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "refresh_token": self._refresh_token,
            "scope": self.SCOPE,
        }
        token_data = await self._request_token(payload)
        self._store_token(token_data)

    async def _request_token(self, payload: dict[str, str]) -> dict[str, Any]:
        url = f"{self._config.base_url}{self._TOKEN_PATH}"
        try:
            response = await self._http.post(url, data=payload)
        except httpx.TimeoutException as exc:
            raise OpenEMRConnectionError(f"Timed out contacting OpenEMR's token endpoint after {self._TIMEOUT_SECONDS}s.") from exc
        except httpx.RequestError as exc:
            raise OpenEMRConnectionError(f"Network error contacting OpenEMR's token endpoint: {exc!r}") from exc

        if response.status_code >= 400:
            # Deliberately not including the response body in the message:
            # OAuth2 error bodies are usually just {"error": "invalid_grant"}
            # but must not be assumed PHI-free by this generic path -- log
            # or display only the status code to any downstream consumer.
            raise OpenEMRAuthError(
                f"OpenEMR rejected the '{payload['grant_type']}' grant (HTTP {response.status_code}). "
                "Check client credentials, resource-owner credentials, and requested scope.",
                status_code=response.status_code,
            )
        return response.json()

    def _store_token(self, token_data: dict[str, Any]) -> None:
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OpenEMRAuthError("OpenEMR's token endpoint response did not include an access_token.")

        expires_in_raw = token_data.get("expires_in", 3600)
        try:
            expires_in_seconds = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in_seconds = 3600

        self._access_token = access_token
        # A refresh-token grant response may omit refresh_token (some OAuth2
        # servers rotate it, some don't) -- keep the existing one if so,
        # rather than treating its absence as "no refresh token available".
        new_refresh_token = token_data.get("refresh_token")
        if isinstance(new_refresh_token, str) and new_refresh_token:
            self._refresh_token = new_refresh_token
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
