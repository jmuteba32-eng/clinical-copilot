"""FastAPI service exposing the Clinical Co-Pilot agent.

This module is the HTTP boundary: it owns request/response shapes,
correlation IDs, structured logging, and error handling. It does not
itself talk to OpenEMR or Anthropic -- that stays in `openemr_client.py`
and `agent.py` respectively -- and it does not decide what's true about a
patient. Its only jobs are "route the request in, route the answer back
out, and make sure every log line and every error can be traced to the
request that caused it."
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# Loads agent/.env for local development if present; a no-op in production
# (Railway, or any environment) where real variables are injected by the
# platform. Resolved relative to this file, not the process's current
# working directory: `uvicorn agent.main:app` is meant to be launched from
# the repo root, so a bare `load_dotenv()` (which searches upward from the
# CWD) would look in the repo root and never find `agent/.env`. Railway
# runs from a different CWD than a local machine too, so anything
# CWD-relative here would be fragile in exactly the same way. Runs before
# the `agent.*` imports below -- not because either currently reads env
# vars at import time (they don't, only inside functions/methods), but so
# that stays true by construction rather than by accident as this file
# grows.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agent import observability  # noqa: E402
from agent.audit_trail import AuditRecord, AuditTrail  # noqa: E402
from agent.baa_gate import BAA_REFUSAL_MESSAGE, baa_confirmed  # noqa: E402
from agent.agent import ClinicalCoPilot  # noqa: E402
from agent.cache import CachedPatientChart, PatientChartCache, fetch_patient_chart, refresh_chart_diagnosis  # noqa: E402
from agent.keep_warm import keep_warm_enabled, run_keep_warm_loop  # noqa: E402
from agent.openemr_client import OpenEMRClient, OpenEMRClientError, OpenEMRConnectionError  # noqa: E402
from agent.list_patients import list_openemr_patients  # noqa: E402
from agent.tools import patient_exists  # noqa: E402
from agent.verification import VerificationResult, records_for_verification, verify  # noqa: E402
from agent.session import OTHER_PATIENT_REFUSAL_MESSAGE, foreign_patient_uuids_in_text  # noqa: E402


# --------------------------------------------------------------------------
# Correlation ID
# --------------------------------------------------------------------------
#
# A ContextVar, not a function parameter threaded through every call, so
# that openemr_client.py, tools.py, and agent.py -- none of which know this
# HTTP layer exists -- can still have their log lines stamped with the ID of
# the request that triggered them. Correctness depends on this middleware
# setting the var *before* awaiting the route handler, in the same asyncio
# task: ContextVars propagate through nested awaits within one task, but
# not across a task boundary (e.g. `asyncio.create_task`) without an
# explicit copy. FastAPI/Starlette run a request's middleware -> route
# handler chain as nested awaits in one task, so that condition holds here.

_correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="-")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _resolve_correlation_id(request: Request) -> str:
    """Reuse an inbound X-Correlation-ID if it's a well-formed UUID, else mint one.

    Accepting an upstream ID (e.g. from a gateway that already assigned one)
    lets a trace stay joined end-to-end across services. Requiring UUID
    shape before reusing it is a deliberate guard against log injection --
    this value is written verbatim into structured JSON log lines, so an
    attacker-controlled header must never be trusted unvalidated.
    """
    incoming = request.headers.get("x-correlation-id")
    if incoming and _UUID_RE.match(incoming):
        return incoming
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# Structured JSON logging
# --------------------------------------------------------------------------
#
# Deliberately NOT the pattern AUDIT.md flagged in OpenEMR itself (full API
# response bodies persisted as plaintext PHI in `api_log.response`, with no
# encryption and no retention policy, under the documented default logging
# level). This formatter logs metadata about what happened -- resource
# types, source IDs, latency, error strings our own exception types already
# keep PHI-free -- never a FHIR resource body. Nothing in this codebase
# calls `logger.*()` with a raw tool result or FHIR JSON payload; keep it
# that way when adding new log lines.

_RESERVED_LOG_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "message": record.getMessage(),
        }
        # Anything passed via logger.info(..., extra={...}) lands directly
        # on the LogRecord's __dict__; pull it back out generically rather
        # than hardcoding field names, so "event", "tool", "latency_ms",
        # "error", etc. all flow through without this formatter needing to
        # know about each one individually.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS and key != "correlation_id":
                payload[key] = value
        if record.exc_info:
            # Server-side-only observability, never returned to a client
            # (see the exception handlers below) and never a FHIR payload --
            # a Python traceback shows source lines, not the PHI-bearing
            # values this project's exception types are designed to omit
            # from their messages in the first place (see openemr_client.py).
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _CorrelationIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Only fills in a default if the log call didn't already pass one
        # explicitly via `extra={"correlation_id": ...}` -- the exception
        # handlers below do that deliberately (see their comments for why
        # the ContextVar alone isn't trustworthy at that specific point).
        if not hasattr(record, "correlation_id"):
            record.correlation_id = _correlation_id_ctx.get()
        return True


_logging_configured = False


def _configure_logging() -> None:
    """Install the JSON formatter on the root logger, once.

    Attaching to the *root* logger (not this module's own logger) means
    every module-level `logging.getLogger(__name__)` call in
    openemr_client.py, tools.py, and agent.py -- none of which import this
    file -- automatically gets JSON formatting and a correlation_id via
    ordinary logging propagation. No call-site changes needed in those
    modules for this to work.
    """
    global _logging_configured
    if _logging_configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter())
    handler.addFilter(_CorrelationIdLogFilter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
    # httpx logs one INFO-level line per outbound request by default; our
    # own "http_request"/"ask_request_*" lines already cover that with
    # richer, correlation_id-bearing fields, so httpx's own line would just
    # be redundant noise in the structured log stream.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _logging_configured = True


_configure_logging()

logger = logging.getLogger(__name__)

_UI_HTML_PATH = Path(__file__).resolve().parent / "chat_ui.html"
_UI_HTML = _UI_HTML_PATH.read_text(encoding="utf-8")

# In-memory UI sessions (token -> username). Replaced when real auth lands.
_ui_sessions: dict[str, str] = {}


# --------------------------------------------------------------------------
# App lifespan: one shared OpenEMRClient + ClinicalCoPilot per process
# --------------------------------------------------------------------------
#
# Constructed once, not per-request: OpenEMRClient's in-memory token cache
# and refresh lock (see openemr_client.py) only pay off if one instance is
# reused across requests, and Anthropic's AsyncAnthropic is documented to be
# safe to share across concurrent calls.


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    observability.configure()
    chart_cache = PatientChartCache.from_env()
    audit_trail = AuditTrail.from_env()
    async with OpenEMRClient() as openemr_client:
        copilot = ClinicalCoPilot(openemr_client)
        app.state.openemr_client = openemr_client
        app.state.copilot = copilot
        app.state.chart_cache = chart_cache
        app.state.audit_trail = audit_trail
        keep_warm_task: asyncio.Task[None] | None = None
        if keep_warm_enabled():
            keep_warm_task = asyncio.create_task(run_keep_warm_loop(openemr_client))
        logger.info(
            "app_startup",
            extra={
                "event": "app_startup",
                "langfuse_enabled": observability.enabled(),
                "chart_cache_enabled": chart_cache is not None,
                "keep_warm_enabled": keep_warm_task is not None,
            },
        )
        try:
            yield
        finally:
            if keep_warm_task is not None:
                keep_warm_task.cancel()
                try:
                    await keep_warm_task
                except asyncio.CancelledError:
                    pass
    observability.shutdown()
    logger.info("app_shutdown", extra={"event": "app_shutdown"})


app = FastAPI(title="Clinical Co-Pilot", lifespan=_lifespan)


def get_openemr_client(request: Request) -> OpenEMRClient:
    return request.app.state.openemr_client


def get_copilot(request: Request) -> ClinicalCoPilot:
    return request.app.state.copilot


async def _verify_patient_exists(
    openemr_client: OpenEMRClient,
    patient_id: str,
    *,
    correlation_id: str,
) -> bool:
    """Run ``patient_exists`` with one automatic retry on cold-start timeout.

    Railway's free tier lets OpenEMR sleep; the first FHIR call after idle
    often times out while the container wakes. A single immediate retry
    usually succeeds without making the physician wait through two full
    request cycles manually.
    """
    try:
        return await patient_exists(openemr_client, patient_id)
    except OpenEMRConnectionError as exc:
        if "Timed out" not in str(exc):
            raise
        logger.warning(
            "patient_existence_check_timeout_retry",
            extra={
                "event": "patient_existence_check_timeout_retry",
                "patient_id": patient_id,
                "correlation_id": correlation_id,
            },
        )
        return await patient_exists(openemr_client, patient_id)


def _ui_username_from_request(request: Request) -> str | None:
    """Resolve the in-memory UI session username from a Bearer token, if any."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    return _ui_sessions.get(token)


def _record_audit(
    request: Request,
    *,
    event_type: str,
    outcome: str,
    username: str | None = None,
    patient_id: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append one audit event; never raises — failures are logged and dropped."""
    trail: AuditTrail | None = getattr(request.app.state, "audit_trail", None)
    if trail is None:
        return
    try:
        trail.append_sync(
            username=username or _ui_username_from_request(request) or "unauthenticated",
            event_type=event_type,  # type: ignore[arg-type]
            outcome=outcome,
            patient_id=patient_id,
            correlation_id=correlation_id or _correlation_id_for(request),
        )
    except Exception:
        logger.warning("audit_record_failed", extra={"event": "audit_record_failed"}, exc_info=True)


def _is_baa_confirmed() -> bool:
    """Safe read of admin BAA attestation — defaults to blocked on any failure."""
    try:
        return baa_confirmed()
    except Exception:
        logger.warning("baa_gate_read_failed", extra={"event": "baa_gate_read_failed"}, exc_info=True)
        return False


def _record_baa_gate_blocked(request: Request, *, patient_id: str, correlation_id: str) -> None:
    _record_audit(
        request,
        event_type="baa_gate_blocked",
        outcome="blocked",
        patient_id=patient_id,
        correlation_id=correlation_id,
    )
    logger.info(
        "baa_gate_blocked",
        extra={
            "event": "baa_gate_blocked",
            "patient_id": patient_id,
            "correlation_id": correlation_id,
        },
    )


# --------------------------------------------------------------------------
# Correlation ID + request-summary middleware
# --------------------------------------------------------------------------


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    correlation_id = _resolve_correlation_id(request)
    token = _correlation_id_ctx.set(correlation_id)
    # Also stashed on `request.state`, which is the same Request object
    # reference passed all the way down to route handlers and exception
    # handlers -- unlike the ContextVar, it can't be affected by Starlette's
    # `BaseHTTPMiddleware` running the rest of the stack in a separate task
    # (a documented Starlette quirk: a spawned task's copied context can
    # end up detached from this one by the time a registered exception
    # handler runs). Route/exception handlers that receive `request`
    # directly should prefer `request.state.correlation_id`; the ContextVar
    # remains the only mechanism for code with no Request object at all
    # (openemr_client.py, tools.py, agent.py) and works correctly there.
    request.state.correlation_id = correlation_id
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        # Extremely unlikely to be reached: the exception handlers below
        # normally convert a route-handler exception into a Response before
        # call_next() returns. This is a last-resort net for anything that
        # still escapes that layer, so the correlation_id context is torn
        # down cleanly either way rather than leaking into the next request
        # handled by this worker.
        _correlation_id_ctx.reset(token)
        raise

    latency_ms = round((time.monotonic() - start) * 1000, 2)
    response.headers["X-Correlation-ID"] = correlation_id
    if request.url.path != "/health":
        # /health is a liveness probe hit frequently by orchestrators; a
        # structured log line for every one of those adds noise without
        # adding information a human would ever look for.
        logger.info(
            "http_request",
            extra={
                "event": "http_request",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )
    _correlation_id_ctx.reset(token)
    return response


# --------------------------------------------------------------------------
# Error handling: never leak internals to the client
# --------------------------------------------------------------------------


def _correlation_id_for(request: Request) -> str:
    """The one place exception handlers get a correlation_id from.

    Prefers `request.state.correlation_id` (set by the middleware above)
    over the ContextVar -- see the middleware's comment for why the
    ContextVar specifically cannot be trusted inside a registered exception
    handler under `BaseHTTPMiddleware`. Falls back to the ContextVar only
    if `request.state` was somehow never populated (e.g. a request that
    failed before the middleware ran at all).
    """
    return getattr(request.state, "correlation_id", None) or _correlation_id_ctx.get()


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    correlation_id = _correlation_id_for(request)
    logger.info(
        "request_validation_failed",
        extra={"event": "request_validation_failed", "path": request.url.path, "correlation_id": correlation_id},
    )
    return JSONResponse(
        status_code=422,
        content={"error": "Invalid request.", "details": exc.errors(), "correlation_id": correlation_id},
        headers={"X-Correlation-ID": correlation_id},
    )


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: turn any unhandled exception into a clean, traceable 500.

    The client gets a generic message and a correlation_id -- nothing about
    `exc` itself (type, message, traceback) crosses this boundary, per the
    project rule against exposing exception details in user-facing output.
    The full exception, with traceback, is logged server-side via
    `logger.exception` (visible only in the structured JSON logs, keyed by
    the same correlation_id) so it's fully traceable without ever reaching
    the caller.
    """
    correlation_id = _correlation_id_for(request)
    logger.exception(
        "unhandled_exception",
        extra={
            "event": "unhandled_exception",
            "path": request.url.path,
            "method": request.method,
            "correlation_id": correlation_id,
        },
    )
    return JSONResponse(
        status_code=500,
        content={"error": "An internal error occurred. Please try again.", "correlation_id": correlation_id},
        headers={"X-Correlation-ID": correlation_id},
    )


# --------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------


class AskRequest(BaseModel):
    patient_id: str = Field(..., min_length=1, description="The FHIR Patient resource ID.")
    question: str = Field(..., min_length=1, description="The physician's natural-language question about this patient.")


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]]
    verification: VerificationResult
    correlation_id: str


class RespondRequest(BaseModel):
    question: str = Field(..., min_length=1, description="The physician's natural-language question. Routed through the Week 2 grounded-answer pipeline (scope classification, hybrid RAG retrieval, cited answer).")


class RespondCitation(BaseModel):
    source_type: str
    source_id: str
    page_or_section: str | None = None
    field_or_chunk_id: str | None = None
    quote_or_value: str | None = None


class RespondResponse(BaseModel):
    scope: str
    verified: bool
    answer: str
    refusal_reason: str | None = None
    citations: list[RespondCitation] = Field(default_factory=list)
    correlation_id: str


class ChatTurn(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant)$", description="Prior turn role in this session.")
    content: str = Field(..., min_length=1, description="Prior turn text (published Q&A only, not tool blocks).")


class ChatRequest(BaseModel):
    patient_id: str = Field(..., min_length=1, description="The FHIR Patient resource ID — fixed for the whole session.")
    message: str = Field(..., min_length=1, description="The physician's current message in this session.")
    conversation_history: list[ChatTurn] = Field(
        default_factory=list,
        description="Prior user/assistant turns for this session, in order.",
    )


class ChatResponse(BaseModel):
    answer: str
    verification: VerificationResult
    correlation_id: str
    updated_conversation_history: list[ChatTurn]


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    token: str
    username: str


class PatientSummary(BaseModel):
    """Picker row for GET /patients — chart identification, not agent clinical output.

    ``name`` is included so physicians can select the right chart. The agent's
    ``get_intake`` tool deliberately omits name (minimum-necessary PHI for Q&A).
    """

    id: str
    name: str | None = None
    age: int | None = None
    sex: str | None = None


# Returned by /ask's patient-existence gate below, verbatim -- a fixed
# string (not model-generated) is deliberate: a physician should never see
# a chart-summary-shaped answer for a patient_id that was never confirmed
# to exist, and a fixed message can't accidentally drift into implying
# otherwise the way a synthesized one might.
_PATIENT_NOT_FOUND_MESSAGE = "No patient with that identifier exists."


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Clinical chat UI — self-contained HTML, no build step."""
    return HTMLResponse(content=_UI_HTML)


@app.post("/login", response_model=LoginResponse)
async def login(request: Request, payload: LoginRequest) -> LoginResponse | JSONResponse:
    """MVP physician login — accepts configured credentials, returns an in-memory session token."""
    correlation_id = _correlation_id_for(request)
    expected_user = os.environ.get("AGENT_UI_USERNAME", "admin")
    expected_pass = os.environ.get("AGENT_UI_PASSWORD", "ClinicalCoPilot2026!")
    if payload.username != expected_user or payload.password != expected_pass:
        _record_audit(
            request,
            event_type="login",
            outcome="failure",
            username=payload.username,
            correlation_id=correlation_id,
        )
        return JSONResponse(status_code=401, content={"error": "Invalid credentials."})
    token = str(uuid.uuid4())
    _ui_sessions[token] = payload.username
    logger.info("ui_login", extra={"event": "ui_login", "username": payload.username})
    _record_audit(
        request,
        event_type="login",
        outcome="success",
        username=payload.username,
        correlation_id=correlation_id,
    )
    return LoginResponse(token=token, username=payload.username)


@app.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """End the in-memory UI session and record a logout audit event."""
    correlation_id = _correlation_id_for(request)
    username = _ui_username_from_request(request)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        _ui_sessions.pop(token, None)
    outcome = "success" if username else "failure"
    _record_audit(
        request,
        event_type="logout",
        outcome=outcome,
        username=username or "unknown",
        correlation_id=correlation_id,
    )
    if not username:
        return JSONResponse(status_code=401, content={"error": "Not signed in."})
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/audit", response_model=list[AuditRecord])
async def get_audit(request: Request) -> list[AuditRecord] | JSONResponse:
    """Return append-only access audit records (newest first). Requires UI session."""
    if _ui_username_from_request(request) is None:
        return JSONResponse(status_code=401, content={"error": "Sign in required."})
    trail: AuditTrail = request.app.state.audit_trail
    return await trail.read_records()


@app.get("/baa-status")
async def baa_status() -> dict[str, bool]:
    """Whether an administrator has attested a BAA for the configured LLM provider."""
    return {"baa_confirmed": _is_baa_confirmed()}


@app.get("/patients", response_model=list[PatientSummary])
async def list_patients(request: Request) -> list[PatientSummary] | JSONResponse:
    """Minimum-necessary patient list for the UI picker (id, age, sex only).

    Uses the same code path as ``python -m agent.list_patients``: a fresh
    ``OpenEMRClient`` context per request (not the lifespan singleton), so
    auth + FHIR search behave identically to the working CLI utility.
    """
    correlation_id = _correlation_id_for(request)
    try:
        async with OpenEMRClient() as client:
            rows = await list_openemr_patients(client)
    except OpenEMRClientError as exc:
        logger.error(
            "patients_list_failed",
            extra={
                "event": "patients_list_failed",
                "correlation_id": correlation_id,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "openemr_status_code": exc.status_code,
            },
            exc_info=True,
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Could not load patients from OpenEMR. Please try again."},
        )
    except Exception as exc:
        logger.exception(
            "patients_list_unexpected_error",
            extra={
                "event": "patients_list_unexpected_error",
                "correlation_id": correlation_id,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Could not load patients from OpenEMR. Please try again."},
        )

    return [PatientSummary(id=row["id"], name=row["name"], age=row["age"], sex=row["sex"]) for row in rows]


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness: is the process alive? No dependency checks -- see explanation."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request, openemr_client: OpenEMRClient = Depends(get_openemr_client)) -> Response:
    """Readiness: can this instance actually serve an /ask request right now?

    Checks the two things `answer()` cannot function without: an
    ANTHROPIC_API_KEY, and a working OpenEMR authentication. Deliberately
    does NOT make a FHIR data call -- "can we authenticate" and "can we read
    a specific resource type" are different failure modes (the latter also
    depends on per-resource scope/ACL grants), and this endpoint is scoped
    to the former.

    Uses `openemr_client.readiness_timeout_seconds` here rather than the
    client's default (`OPENEMR_TIMEOUT_SECONDS`, which bounds a live tool
    call during a physician's question): OpenEMR has no caching layer and
    10-40+ queries go into assembling one patient's picture (per AUDIT.md),
    and this deployment runs on Railway's free tier, which cold-starts
    slowly. A readiness probe can afford to wait that out; a physician
    waiting on an actual answer cannot -- which is exactly why this
    project's architecture pre-computes summaries rather than synthesizing
    everything live on every question.
    """
    failures: list[str] = []

    if not os.environ.get("ANTHROPIC_API_KEY"):
        failures.append("ANTHROPIC_API_KEY is not set")

    try:
        await openemr_client.ensure_authenticated(timeout_seconds=openemr_client.readiness_timeout_seconds)
    except OpenEMRClientError as exc:
        failures.append(f"OpenEMR authentication failed: {exc}")

    if failures:
        logger.warning(
            "readiness_check_failed",
            extra={"event": "readiness_check_failed", "failures": failures, "correlation_id": _correlation_id_for(request)},
        )
        return JSONResponse(status_code=503, content={"status": "not_ready", "failures": failures})

    return JSONResponse(status_code=200, content={"status": "ready"})


@app.post("/respond", response_model=RespondResponse)
async def respond_endpoint(
    request: Request,
    payload: RespondRequest,
) -> RespondResponse | JSONResponse:
    """Week 2 grounded-answer pipeline: scope -> hybrid RAG -> cited answer.

    Reuses the Week 1 BAA gate and correlation-ID/PHI-safe logging. The
    question text is never logged (it can restate chart specifics); only
    metadata is recorded, exactly as /ask does.
    """
    correlation_id = _correlation_id_for(request)

    if not _is_baa_confirmed():
        _record_baa_gate_blocked(request, patient_id="week2-respond", correlation_id=correlation_id)
        return JSONResponse(
            status_code=403,
            content={"error": BAA_REFUSAL_MESSAGE, "correlation_id": correlation_id},
        )

    logger.info(
        "respond_request",
        extra={"event": "respond_request", "correlation_id": correlation_id, "question_length": len(payload.question)},
    )

    from agent.pipeline.respond import respond as _run_pipeline

    grounded = await asyncio.to_thread(_run_pipeline, payload.question, correlation_id)

    return RespondResponse(
        scope=grounded.scope,
        verified=grounded.verified,
        answer=grounded.answer_text,
        refusal_reason=grounded.refusal_reason,
        citations=[RespondCitation(**c.model_dump()) for c in grounded.citations],
        correlation_id=correlation_id,
    )


@app.post("/ask", response_model=AskResponse)
async def ask(
    request: Request,
    payload: AskRequest,
    copilot: ClinicalCoPilot = Depends(get_copilot),
    openemr_client: OpenEMRClient = Depends(get_openemr_client),
) -> AskResponse | JSONResponse:
    correlation_id = _correlation_id_for(request)

    if not _is_baa_confirmed():
        _record_baa_gate_blocked(request, patient_id=payload.patient_id, correlation_id=correlation_id)
        return JSONResponse(
            status_code=403,
            content={"error": BAA_REFUSAL_MESSAGE, "correlation_id": correlation_id},
        )

    # Logs WHAT was asked about (patient_id -- an opaque FHIR resource ID)
    # and how long the question was, never the question's text: free-text
    # physician questions can easily restate chart specifics ("does his
    # A1c look better than last visit"), so the text itself is treated as
    # PHI-adjacent and kept out of logs entirely.
    logger.info(
        "ask_request_received",
        extra={
            "event": "ask_request_received",
            "patient_id": payload.patient_id,
            "question_length": len(payload.question),
            "correlation_id": correlation_id,
        },
    )

    # One Langfuse trace per request (observability.py), joined to this
    # request's correlation_id (see trace_ask_request's docstring). Every
    # LLM-call/tool-call span `copilot.answer()` opens below nests under
    # this trace automatically via OpenTelemetry context propagation --
    # that only holds as long as `answer()` runs INSIDE this `with` block,
    # on this same asyncio task (same condition the correlation_id
    # ContextVar above already depends on).
    with observability.trace_ask_request(
        correlation_id=correlation_id, patient_id=payload.patient_id, question_length=len(payload.question)
    ) as trace:
        # Confirms the patient actually exists BEFORE running any of the
        # four tools -- see `tools.py`'s `patient_exists()` docstring and
        # `ToolResult`'s docstring in schemas.py for the full reasoning.
        # Short version: OpenEMR's FHIR search endpoints return HTTP 200
        # with an empty Bundle for BOTH "no patient with this id" and
        # "patient exists, zero records of this type" -- indistinguishable
        # from inside a single tool's result. Skipping this check would let
        # a typo'd or stale patient_id sail through all four tools as
        # `missing=True`, and the agent would report that as a normal, if
        # sparse, chart summary -- false confidence about a chart that was
        # never actually read, for a patient who may not exist at all.
        with observability.trace_patient_existence_check() as existence_span:
            try:
                patient_found = await _verify_patient_exists(
                    openemr_client,
                    payload.patient_id,
                    correlation_id=correlation_id,
                )
            except OpenEMRClientError as exc:
                # OpenEMR being unreachable/denying auth is NOT the same
                # fact as "this patient does not exist" -- collapsing the
                # two would report a transient outage as a data-entry
                # error. Logged with the full exception server-side only;
                # `exc`'s message never crosses into the client response
                # (same rule the rest of this file follows for every other
                # OpenEMR failure).
                existence_span.update(output={"patient_found": False})
                logger.error(
                    "patient_existence_check_failed",
                    extra={
                        "event": "patient_existence_check_failed",
                        "patient_id": payload.patient_id,
                        "correlation_id": correlation_id,
                    },
                    exc_info=True,
                )
                trace.update(output={"patient_existence_check": "failed"})
                _record_audit(
                    request,
                    event_type="patient_access",
                    outcome="openemr_unreachable",
                    patient_id=payload.patient_id,
                    correlation_id=correlation_id,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "Could not verify the patient exists in OpenEMR. Please try again.",
                        "correlation_id": correlation_id,
                    },
                )
            existence_span.update(output={"patient_found": patient_found})

        if not patient_found:
            logger.info(
                "patient_not_found",
                extra={
                    "event": "patient_not_found",
                    "patient_id": payload.patient_id,
                    "correlation_id": correlation_id,
                },
            )
            # No clinical claims were made -- and none of the four tools
            # ran -- so a trivial pass is the honest verification outcome
            # here, not a skipped/omitted field. Scored the same as any
            # other trace so a "patient not found" turn doesn't distort a
            # hallucination-rate dashboard by counting as a failure.
            trace.score_trace(name="verification_passed", value=1, data_type="BOOLEAN")
            trace.update(output={"patient_found": False})
            _record_audit(
                request,
                event_type="patient_access",
                outcome="patient_not_found",
                patient_id=payload.patient_id,
                correlation_id=correlation_id,
            )
            return JSONResponse(
                status_code=404,
                content=AskResponse(
                    answer=_PATIENT_NOT_FOUND_MESSAGE,
                    tool_calls=[],
                    verification=VerificationResult(passed=True, checked_claims=0),
                    correlation_id=correlation_id,
                ).model_dump(mode="json"),
            )

        start = time.monotonic()

        session_chart = await _patient_session_chart(request, payload.patient_id, openemr_client, correlation_id)

        result = await copilot.answer(
            payload.patient_id,
            payload.question,
            prefetched_chart=session_chart,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        logger.info(
            "ask_request_completed",
            extra={
                "event": "ask_request_completed",
                "patient_id": payload.patient_id,
                "tool_call_count": len(result["tool_calls"]),
                "record_count": len(result["records"]),
                "latency_ms": latency_ms,
                "correlation_id": correlation_id,
            },
        )

        # Runs AFTER the agent produces its answer and BEFORE anything is
        # returned to the client -- see verification.py's module docstring
        # for why this has to be a separate, deterministic pass rather than
        # the agent trusting its own citations. Never skipped, never
        # short-circuited by a `passed` check here: a failed verification
        # still gets returned (this endpoint's job is to surface the
        # failure structurally, per requirement 4, not to swallow the
        # answer or turn it into a 5xx).
        with observability.trace_verification() as verification_span:
            verification_known = records_for_verification(result["records"], session_chart)
            verification_result = verify(result["answer"], verification_known)
            verification_fields = {
                "passed": verification_result.passed,
                "hallucinated_source_count": len(verification_result.hallucinated_sources),
                "uncited_claim_count": len(verification_result.uncited_claims),
                "boundary_violation_count": len(verification_result.boundary_violations),
                "boundary_violations": verification_result.boundary_violations,
                "checked_claims": verification_result.checked_claims,
            }
            verification_span.update(output=verification_fields)

        log_fn = logger.info if verification_result.passed else logger.warning
        log_fn(
            "verification_completed",
            extra={
                "event": "verification_completed",
                "patient_id": payload.patient_id,
                "correlation_id": correlation_id,
                **verification_fields,
            },
        )

        # Requirement 5: a boolean score on the trace itself (not just the
        # verification span) so hallucination rate can be charted directly
        # from Langfuse's trace-level scores over time, without having to
        # drill into each trace's child spans first.
        trace.score_trace(name="verification_passed", value=1 if verification_result.passed else 0, data_type="BOOLEAN")
        # Requirement 2's trace "output" is deliberately a redacted summary,
        # never `result["answer"]` itself -- that string IS the synthesized
        # clinical content this whole module exists to keep out of Langfuse.
        trace.update(output={"answer_length": len(result["answer"]), "verification": verification_fields})

    _record_audit(
        request,
        event_type="patient_access",
        outcome="success",
        patient_id=payload.patient_id,
        correlation_id=correlation_id,
    )
    return AskResponse(
        answer=result["answer"],
        tool_calls=result["tool_calls"],
        verification=verification_result,
        correlation_id=correlation_id,
    )


def _updated_conversation_history(
    prior: list[ChatTurn],
    message: str,
    answer: str,
) -> list[ChatTurn]:
    return [*prior, ChatTurn(role="user", content=message), ChatTurn(role="assistant", content=answer)]


async def _patient_session_chart(
    request: Request,
    patient_id: str,
    openemr_client: OpenEMRClient,
    correlation_id: str,
) -> CachedPatientChart:
    """Assembled first-visit chart for prefetch and session-scoped verification.

    Uses the encrypted cache when configured; otherwise fetches from OpenEMR
    on each call. Diagnoses are always re-fetched from OpenEMR on every
    session load (even on cache hit) so problem-list edits are not masked by
    the chart cache TTL — see ``refresh_chart_diagnosis``.
    """
    chart_cache: PatientChartCache | None = request.app.state.chart_cache
    if chart_cache is not None:
        cached = await chart_cache.get(patient_id)
        if cached is not None:
            logger.info(
                "cache_hit",
                extra={"event": "cache_hit", "patient_id": patient_id, "correlation_id": correlation_id},
            )
            chart = await refresh_chart_diagnosis(openemr_client, patient_id, cached)
            await chart_cache.put(patient_id, chart)
            logger.info(
                "diagnosis_cache_refresh",
                extra={
                    "event": "diagnosis_cache_refresh",
                    "patient_id": patient_id,
                    "correlation_id": correlation_id,
                    "condition_count": len(chart.get_diagnosis.records),
                    "condition_ids": [r.source_id for r in chart.get_diagnosis.records],
                },
            )
            return chart

        logger.info(
            "cache_miss",
            extra={"event": "cache_miss", "patient_id": patient_id, "correlation_id": correlation_id},
        )
        chart = await fetch_patient_chart(openemr_client, patient_id)
        await chart_cache.put(patient_id, chart)
        return chart

    return await fetch_patient_chart(openemr_client, patient_id)


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    payload: ChatRequest,
    copilot: ClinicalCoPilot = Depends(get_copilot),
    openemr_client: OpenEMRClient = Depends(get_openemr_client),
) -> ChatResponse | JSONResponse:
    correlation_id = _correlation_id_for(request)

    if not _is_baa_confirmed():
        _record_baa_gate_blocked(request, patient_id=payload.patient_id, correlation_id=correlation_id)
        return ChatResponse(
            answer=BAA_REFUSAL_MESSAGE,
            verification=VerificationResult(passed=True, checked_claims=0),
            correlation_id=correlation_id,
            updated_conversation_history=_updated_conversation_history(
                payload.conversation_history,
                payload.message,
                BAA_REFUSAL_MESSAGE,
            ),
        )

    logger.info(
        "chat_request_received",
        extra={
            "event": "chat_request_received",
            "patient_id": payload.patient_id,
            "message_length": len(payload.message),
            "history_turn_count": len(payload.conversation_history),
            "correlation_id": correlation_id,
        },
    )

    # Structural patient lock (pre-LLM): if the current message names another
    # patient's UUID, refuse before any tools run. Tool dispatch is already
    # hard-scoped to payload.patient_id (agent.py::_dispatch_tool_call); this
    # gate stops the turn earlier when the physician (or an injection in the
    # message) tries to pivot the conversation to a different chart.
    foreign_ids = foreign_patient_uuids_in_text(payload.message, payload.patient_id)
    if foreign_ids:
        logger.info(
            "chat_other_patient_refused",
            extra={
                "event": "chat_other_patient_refused",
                "patient_id": payload.patient_id,
                "foreign_patient_ids": foreign_ids,
                "correlation_id": correlation_id,
            },
        )
        _record_audit(
            request,
            event_type="access_refused",
            outcome="refused",
            patient_id=payload.patient_id,
            correlation_id=correlation_id,
        )
        return ChatResponse(
            answer=OTHER_PATIENT_REFUSAL_MESSAGE,
            verification=VerificationResult(passed=True, checked_claims=0),
            correlation_id=correlation_id,
            updated_conversation_history=_updated_conversation_history(
                payload.conversation_history,
                payload.message,
                OTHER_PATIENT_REFUSAL_MESSAGE,
            ),
        )

    history_dicts = [turn.model_dump() for turn in payload.conversation_history]

    with observability.trace_ask_request(
        correlation_id=correlation_id,
        patient_id=payload.patient_id,
        question_length=len(payload.message),
    ) as trace:
        with observability.trace_patient_existence_check() as existence_span:
            try:
                patient_found = await _verify_patient_exists(
                    openemr_client,
                    payload.patient_id,
                    correlation_id=correlation_id,
                )
            except OpenEMRClientError:
                existence_span.update(output={"patient_found": False})
                logger.error(
                    "patient_existence_check_failed",
                    extra={
                        "event": "patient_existence_check_failed",
                        "patient_id": payload.patient_id,
                        "correlation_id": correlation_id,
                    },
                    exc_info=True,
                )
                trace.update(output={"patient_existence_check": "failed"})
                _record_audit(
                    request,
                    event_type="patient_access",
                    outcome="openemr_unreachable",
                    patient_id=payload.patient_id,
                    correlation_id=correlation_id,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "Could not verify the patient exists in OpenEMR. Please try again.",
                        "correlation_id": correlation_id,
                    },
                )
            existence_span.update(output={"patient_found": patient_found})

        if not patient_found:
            logger.info(
                "patient_not_found",
                extra={
                    "event": "patient_not_found",
                    "patient_id": payload.patient_id,
                    "correlation_id": correlation_id,
                },
            )
            trace.score_trace(name="verification_passed", value=1, data_type="BOOLEAN")
            trace.update(output={"patient_found": False})
            _record_audit(
                request,
                event_type="patient_access",
                outcome="patient_not_found",
                patient_id=payload.patient_id,
                correlation_id=correlation_id,
            )
            return JSONResponse(
                status_code=404,
                content=ChatResponse(
                    answer=_PATIENT_NOT_FOUND_MESSAGE,
                    verification=VerificationResult(passed=True, checked_claims=0),
                    correlation_id=correlation_id,
                    updated_conversation_history=_updated_conversation_history(
                        payload.conversation_history,
                        payload.message,
                        _PATIENT_NOT_FOUND_MESSAGE,
                    ),
                ).model_dump(mode="json"),
            )

        start = time.monotonic()
        session_chart = await _patient_session_chart(request, payload.patient_id, openemr_client, correlation_id)

        result = await copilot.chat(
            payload.patient_id,
            payload.message,
            history_dicts,
            prefetched_chart=session_chart,
        )
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        logger.info(
            "chat_request_completed",
            extra={
                "event": "chat_request_completed",
                "patient_id": payload.patient_id,
                "tool_call_count": len(result["tool_calls"]),
                "record_count": len(result["records"]),
                "latency_ms": latency_ms,
                "correlation_id": correlation_id,
            },
        )

        with observability.trace_verification() as verification_span:
            verification_known = records_for_verification(result["records"], session_chart)
            verification_result = verify(result["answer"], verification_known)
            verification_fields = {
                "passed": verification_result.passed,
                "hallucinated_source_count": len(verification_result.hallucinated_sources),
                "uncited_claim_count": len(verification_result.uncited_claims),
                "boundary_violation_count": len(verification_result.boundary_violations),
                "boundary_violations": verification_result.boundary_violations,
                "checked_claims": verification_result.checked_claims,
            }
            verification_span.update(output=verification_fields)

        log_fn = logger.info if verification_result.passed else logger.warning
        log_fn(
            "verification_completed",
            extra={
                "event": "verification_completed",
                "patient_id": payload.patient_id,
                "correlation_id": correlation_id,
                **verification_fields,
            },
        )

        trace.score_trace(name="verification_passed", value=1 if verification_result.passed else 0, data_type="BOOLEAN")
        trace.update(output={"answer_length": len(result["answer"]), "verification": verification_fields})

    _record_audit(
        request,
        event_type="patient_access",
        outcome="success",
        patient_id=payload.patient_id,
        correlation_id=correlation_id,
    )
    return ChatResponse(
        answer=result["answer"],
        verification=verification_result,
        correlation_id=correlation_id,
        updated_conversation_history=_updated_conversation_history(
            payload.conversation_history,
            payload.message,
            result["answer"],
        ),
    )


# --------------------------------------------------------------------------
# Local/Railway entry point
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        # log_config=None and access_log=False: uvicorn's own default
        # logging setup would otherwise overwrite the root logger's
        # handlers configured above, and its built-in access log would
        # duplicate the structured "http_request" summary the correlation
        # middleware already emits with richer fields (correlation_id,
        # latency_ms).
        log_config=None,
        access_log=False,
    )
