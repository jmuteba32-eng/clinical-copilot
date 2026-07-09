"""Langfuse observability wiring for Clinical Co-Pilot.

Traces one Langfuse trace per `/ask` request (see `main.py`), with child
spans for each LLM call, each tool call, and the verification step (see
`agent.py` and `verification.py`). This module owns exactly two
responsibilities, and nothing else:

1. PHI never reaches Langfuse. Every span input/output is filtered through
   `redact()` -- a strict ALLOWLIST of known-safe metadata keys (tool
   names, resource types, source IDs, counts, token counts, latency,
   booleans), not a blocklist trying to pattern-match PHI out of free text.
   AUDIT.md documents OpenEMR's own audit log persisting full API response
   bodies as plaintext PHI with no retention policy -- this module is
   built specifically to not repeat that. See `redact()`'s docstring for
   why an allowlist, and `_SafeObservation.update()` for why redaction is
   applied structurally (every call site gets it for free) rather than left
   as a discipline each call site has to remember.
2. Observability can never break a clinical request. Every Langfuse SDK
   call in this module is wrapped so that if Langfuse is unreachable,
   misconfigured, or raises for any reason, the caller's actual work
   (an LLM call, a tool call, verification) still runs to completion --
   only the trace/span for it is silently dropped, logged as a warning
   server-side. `LANGFUSE_SECRET_KEY`/`LANGFUSE_PUBLIC_KEY` absent is not
   an error at all: this module runs with tracing disabled, same as if
   the keys had failed, via the same no-op code path (see `_SafeObservation`
   and `_observe`).

Nothing outside this module imports the `langfuse` package directly --
`main.py` and `agent.py` only ever call the functions/context managers
below, so both stay decoupled from the Langfuse SDK's own API surface and
from whether tracing is even enabled.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Final, Iterator, Optional

from langfuse import Langfuse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# PHI redaction
# --------------------------------------------------------------------------
#
# ALLOWLIST, not a blocklist: a blocklist/regex-based PHI scrubber can only
# ever catch shapes of PHI its author anticipated (a name here, a date
# there) and will silently miss anything it didn't anticipate -- exactly
# the failure mode this project's schemas.py already rejects for a
# different reason (see its "minimum-necessary PHI" design principle). An
# allowlist inverts the risk: a key not on this list is dropped, full stop,
# regardless of what it contains. The cost is that a legitimately safe new
# metadata field must be added here explicitly before it will show up in
# Langfuse -- a deliberate one-line tax in exchange for "PHI cannot leak
# through a key nobody thought to blocklist."
#
# Every key here is metadata ABOUT a record/call (its type, its ID, whether
# it succeeded, how long it took, how many there were) -- never a field
# that could itself hold clinical free text (schemas.py's `display`,
# `drug_name`, `dosage`, `sig`, `value`, `report_text`, `test_name`, the
# agent's `answer`/`question`, etc. are all deliberately absent).
_ALLOWED_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        # Request/trace identity -- opaque IDs, not clinical content.
        "patient_id",
        "correlation_id",
        "question_length",
        "answer_length",
        # LLM call (generation) span fields.
        "model",
        "stop_reason",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        # Tool call span fields. Deliberately "has_error" (bool), never the
        # raw error string -- schemas.py's ToolResult.error is explicitly
        # documented there as "not verified for PHI content", so it does
        # not get a pass here just because it looks like a short
        # diagnostic message.
        "tool",
        "resource_type",
        "source_id",
        "missing",
        "has_error",
        "record_count",
        "latency_ms",
        # Verification step fields -- all counts/booleans, plus the
        # boundary-violation phrases themselves, which are safe because
        # they are drawn from `verification.py`'s own fixed, small,
        # developer-authored phrase list (never text extracted from the
        # answer) -- a closed vocabulary, not free text.
        "passed",
        "hallucinated_source_count",
        "uncited_claim_count",
        "boundary_violation_count",
        "boundary_violations",
        "checked_claims",
        "verification",
    }
)


def redact(*, data: Any, **_: Any) -> Any:
    """Strip anything not on `_ALLOWED_METADATA_KEYS` before it reaches Langfuse.

    Signature matches `langfuse.types.MaskFunction` (`data`, keyword-only,
    plus arbitrary kwargs) so this doubles as the SDK's own `mask=` hook
    (see `configure()`) as a second, independent layer -- belt and
    suspenders with `_SafeObservation.update()`'s explicit call, not a
    substitute for it: relying on only one of the two would mean a single
    missed call site or a client-construction change, respectively, is
    enough to leak.

    Recurses into dicts/lists so a nested structure under an allowed key
    (e.g. `verification`'s sub-dict) gets the same filtering, not a free
    pass because its parent key was allowed.
    """
    if isinstance(data, dict):
        return {key: redact(data=value) for key, value in data.items() if key.lower() in _ALLOWED_METADATA_KEYS}
    if isinstance(data, (list, tuple)):
        return [redact(data=item) for item in data]
    return data


# --------------------------------------------------------------------------
# Approximate cost estimation
# --------------------------------------------------------------------------
#
# HONEST LIMITATION: Anthropic's API does not return a cost figure, only
# token counts (see `_llm_usage_details` below) -- so a dollar cost can
# only ever be an estimate against a hand-maintained price table, which WILL
# go stale as pricing changes. Wrong numbers here degrade a nice-to-have
# cost dashboard, never anything safety-critical, which is why this stays a
# best-effort helper rather than something worth wiring up to a live
# pricing API. If a model isn't matched, no cost_details is sent at all --
# Langfuse's own model-cost table fills the gap when it recognizes the
# model string, so omitting is strictly better than guessing.
_APPROX_USD_PER_MILLION_TOKENS: Final[tuple[tuple[str, float, float], ...]] = (
    # (substring matched against the model name, input $/1M tokens, output $/1M tokens)
    ("opus", 15.00, 75.00),
    ("sonnet", 3.00, 15.00),
    ("haiku", 0.80, 4.00),
)


def _estimate_cost_usd(model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[dict[str, float]]:
    if input_tokens is None or output_tokens is None:
        return None
    lowered = model.lower()
    for substring, input_rate, output_rate in _APPROX_USD_PER_MILLION_TOKENS:
        if substring in lowered:
            total = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
            return {"total_cost": round(total, 6)}
    return None


# --------------------------------------------------------------------------
# Client lifecycle
# --------------------------------------------------------------------------

_client: Optional[Langfuse] = None
_configured = False


def configure() -> None:
    """Initialize the Langfuse client once, from env, if configured.

    Called once from `main.py`'s app lifespan. `LANGFUSE_SECRET_KEY` and
    `LANGFUSE_PUBLIC_KEY` absent (either or both) is treated as "tracing
    intentionally disabled", not a startup error, per requirement 6 --
    unlike `OpenEMRConfig`/`ClinicalCoPilotConfigError`, which fail loudly
    because those ARE required for the agent to function at all.
    `LANGFUSE_HOST` is read by the SDK itself if set, and defaults to
    Langfuse Cloud if not -- no explicit handling needed here.

    A construction failure (bad host, network issue at startup, whatever)
    is caught and logged rather than raised, for the same requirement-6
    reason: a physician's ability to ask a question must never depend on
    Langfuse being reachable.
    """
    global _client, _configured
    if _configured:
        return
    _configured = True

    if not (os.environ.get("LANGFUSE_SECRET_KEY") and os.environ.get("LANGFUSE_PUBLIC_KEY")):
        logger.info(
            "langfuse_disabled",
            extra={"event": "langfuse_disabled", "reason": "LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY not set"},
        )
        return

    try:
        # `mask=redact` registers the second redaction layer described in
        # `redact()`'s docstring. Passing no explicit public_key/secret_key/
        # host: the SDK reads LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/
        # LANGFUSE_HOST from the environment itself when they're omitted,
        # which is requirement 1's "read from env", done by the library
        # rather than duplicated here.
        _client = Langfuse(mask=redact)
        logger.info("langfuse_enabled", extra={"event": "langfuse_enabled"})
    except Exception:
        logger.warning("langfuse_init_failed", extra={"event": "langfuse_init_failed"}, exc_info=True)
        _client = None


def enabled() -> bool:
    return _client is not None


def shutdown() -> None:
    """Flush buffered spans before the process exits. Best-effort, like everything else here."""
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception:
        logger.warning("langfuse_shutdown_failed", extra={"event": "langfuse_shutdown_failed"}, exc_info=True)


# --------------------------------------------------------------------------
# Safe observation wrapper
# --------------------------------------------------------------------------


class _SafeObservation:
    """Wraps a real Langfuse observation, or nothing, behind one interface.

    Every call site (`agent.py`, `main.py`) always gets an instance of this
    -- never a real Langfuse object, never `None` -- and always calls
    `.update()`/`.score_trace()` unconditionally, with no
    `if tracing_enabled:` branching at the call site. That collapses two
    failure modes (tracing disabled; tracing enabled but this one call
    failed) into a single code path: quietly do nothing.
    """

    def __init__(self, real: Any) -> None:
        self._real = real

    def update(self, **kwargs: Any) -> None:
        if self._real is None:
            return
        # Structural redaction (see module docstring): applied here, not
        # left to each call site to remember, so passing an un-redacted
        # dict for input/output/metadata is not even possible through this
        # wrapper.
        safe_kwargs = {key: (redact(data=value) if key in ("input", "output", "metadata") else value) for key, value in kwargs.items()}
        try:
            self._real.update(**safe_kwargs)
        except Exception:
            logger.warning("langfuse_observation_update_failed", extra={"event": "langfuse_observation_update_failed"}, exc_info=True)

    def score_trace(self, *, name: str, value: float, data_type: str = "BOOLEAN", **kwargs: Any) -> None:
        if self._real is None:
            return
        try:
            self._real.score_trace(name=name, value=value, data_type=data_type, **kwargs)
        except Exception:
            logger.warning("langfuse_score_failed", extra={"event": "langfuse_score_failed"}, exc_info=True)


_NOOP_OBSERVATION: Final[_SafeObservation] = _SafeObservation(None)


# --------------------------------------------------------------------------
# Span/generation context managers
# --------------------------------------------------------------------------


@contextmanager
def _observe(*, as_type: str, name: str, trace_context: Optional[dict[str, str]] = None, input_data: Any = None, metadata: Any = None, **extra: Any) -> Iterator[_SafeObservation]:
    """Shared implementation behind every `trace_*` function below.

    Starting the observation and entering its context are both wrapped:
    if either raises (or if tracing is disabled), the caller still gets a
    working `_SafeObservation` -- just a no-op one -- and its own work
    proceeds unaffected. If the CALLER's code inside the `with` block
    raises, that exception is forwarded to the real observation's
    `__exit__` (so Langfuse can mark the span errored) and then
    re-raised unchanged -- this module only swallows ITS OWN failures,
    never the caller's.
    """
    if _client is None:
        yield _NOOP_OBSERVATION
        return

    try:
        kwargs: dict[str, Any] = {"as_type": as_type, "name": name}
        if trace_context is not None:
            kwargs["trace_context"] = trace_context
        if input_data is not None:
            kwargs["input"] = redact(data=input_data)
        if metadata is not None:
            kwargs["metadata"] = redact(data=metadata)
        kwargs.update(extra)
        cm = _client.start_as_current_observation(**kwargs)
        observation = cm.__enter__()
    except Exception:
        logger.warning("langfuse_observation_start_failed", extra={"event": "langfuse_observation_start_failed", "span_name": name}, exc_info=True)
        yield _NOOP_OBSERVATION
        return

    try:
        yield _SafeObservation(observation)
    except BaseException:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:
            logger.warning("langfuse_observation_end_failed", extra={"event": "langfuse_observation_end_failed", "span_name": name}, exc_info=True)
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            logger.warning("langfuse_observation_end_failed", extra={"event": "langfuse_observation_end_failed", "span_name": name}, exc_info=True)


def trace_ask_request(*, correlation_id: str, patient_id: str, question_length: int) -> AbstractContextManager[_SafeObservation]:
    """Root span for one /ask request -- one Langfuse trace per request.

    The trace's ID is deterministically derived from `correlation_id` via
    `create_trace_id(seed=...)`, and `correlation_id` is also placed in the
    trace's metadata verbatim -- two independent ways to join a Langfuse
    trace back to this app's own structured logs (requirement 2), in case
    only one is convenient at hand (metadata is human-readable in the
    Langfuse UI immediately; the seeded ID lets code recompute the exact
    trace_id from a correlation_id found in a log line without an API call).
    """
    trace_context: Optional[dict[str, str]] = None
    if _client is not None:
        try:
            trace_context = {"trace_id": _client.create_trace_id(seed=correlation_id)}
        except Exception:
            logger.warning("langfuse_trace_id_failed", extra={"event": "langfuse_trace_id_failed"}, exc_info=True)

    return _observe(
        as_type="span",
        name="clinical_copilot_ask",
        trace_context=trace_context,
        input_data={"patient_id": patient_id, "question_length": question_length},
        metadata={"correlation_id": correlation_id},
    )


def trace_llm_call(*, model: str) -> AbstractContextManager[_SafeObservation]:
    """One span per Claude call inside `ClinicalCoPilot.answer()`'s tool-use loop."""
    return _observe(as_type="generation", name="llm_call", model=model)


def trace_tool_call(*, tool_name: str) -> AbstractContextManager[_SafeObservation]:
    """One span per tool dispatched from `ClinicalCoPilot._dispatch_tool_call()`."""
    return _observe(as_type="span", name=f"tool:{tool_name}", input_data={"tool": tool_name})


def trace_verification() -> AbstractContextManager[_SafeObservation]:
    """One span for the `verify()` call in `main.py`'s /ask handler."""
    return _observe(as_type="span", name="verification")


def llm_usage_and_cost(*, model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> tuple[Optional[dict[str, int]], Optional[dict[str, float]]]:
    """Build `usage_details`/`cost_details` for `trace_llm_call`'s span from an Anthropic response's `.usage`.

    Returns `(None, None)` if token counts are unavailable (e.g. a
    truncated/errored response) rather than fabricating zeros, which would
    misrepresent a real call as a free one on a cost dashboard.
    """
    if input_tokens is None or output_tokens is None:
        return None, None
    usage_details = {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens}
    cost_details = _estimate_cost_usd(model, input_tokens, output_tokens)
    return usage_details, cost_details
