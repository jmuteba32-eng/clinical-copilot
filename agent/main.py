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
from fastapi.responses import JSONResponse
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

from agent.agent import ClinicalCoPilot  # noqa: E402
from agent.openemr_client import OpenEMRClient, OpenEMRClientError  # noqa: E402


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
    async with OpenEMRClient() as openemr_client:
        copilot = ClinicalCoPilot(openemr_client)
        app.state.openemr_client = openemr_client
        app.state.copilot = copilot
        logger.info("app_startup", extra={"event": "app_startup"})
        yield
    logger.info("app_shutdown", extra={"event": "app_shutdown"})


app = FastAPI(title="Clinical Co-Pilot", lifespan=_lifespan)


def get_openemr_client(request: Request) -> OpenEMRClient:
    return request.app.state.openemr_client


def get_copilot(request: Request) -> ClinicalCoPilot:
    return request.app.state.copilot


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
    correlation_id: str


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


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


@app.post("/ask", response_model=AskResponse)
async def ask(request: Request, payload: AskRequest, copilot: ClinicalCoPilot = Depends(get_copilot)) -> AskResponse:
    correlation_id = _correlation_id_for(request)

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

    start = time.monotonic()
    result = await copilot.answer(payload.patient_id, payload.question)
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

    return AskResponse(answer=result["answer"], tool_calls=result["tool_calls"], correlation_id=correlation_id)


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
