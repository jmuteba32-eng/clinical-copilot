"""The single agent that orchestrates Clinical Co-Pilot's tool-calling loop.

`ClinicalCoPilot` is the only place that talks to the Anthropic API. It
interprets a physician's question, asks Claude which of the four read-only
tools (see tools.py) to call, executes those tools against OpenEMR, and
feeds the results back until Claude produces a final, sourced answer.

Why one agent, not several: the task is bounded to retrieval + synthesis
over one patient's chart. Every additional agent in a pipeline is another
place a claim can lose its source on the way to the physician (see
ARCHITECTURE.md, "ONE agent, typed tools"). This class is deliberately thin
-- it does not itself decide what's true about the patient; it plumbs
Claude's tool choices through to typed tools and hands the result back
unmodified for a separate verification layer to check.

Security note: the LLM is never the security boundary here, and that is
enforced structurally, not by prompting. Every tool call this class
dispatches is hard-scoped to the `patient_id` the caller passed into
`answer()` -- never to whatever `patient_id` argument the model happened to
put in a tool_use block. See `_dispatch_tool_call` for where that is
enforced. A confused or adversarially-prompted model turn cannot make this
agent touch a different patient's data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Final

from anthropic import AsyncAnthropic
from anthropic.types import Message

from agent import observability
from agent.cache import CachedPatientChart
from agent.openemr_client import OpenEMRClient
from agent.schemas import SourcedRecord, ToolResult
from agent.tools import TOOL_FUNCTIONS, TOOL_SPECS

logger = logging.getLogger(__name__)


class ClinicalCoPilotConfigError(Exception):
    """Raised when required agent configuration (e.g. ANTHROPIC_API_KEY) is missing.

    Kept as a distinct, fail-fast exception -- same rationale as
    OpenEMRConfigError in openemr_client.py: a missing API key should be a
    loud startup error, not a confusing failure on the first physician
    question.
    """


# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------
#
# These rules are the agent's actual safety mechanism, not a formality --
# but per ARCHITECTURE.md's trust-boundary principle, prompting is a second
# layer of defense, not the primary one. The primary defenses are
# structural: tools can only return typed, sourced records (schemas.py);
# tool_use dispatch ignores the model's own patient_id (see
# _dispatch_tool_call); and a verification layer downstream of this agent
# re-checks every citation against the records actually retrieved. The
# system prompt below is what keeps Claude's *language* aligned with those
# structural guarantees -- it cannot by itself create them.

_HARD_RULES: Final[str] = """\
You are a clinical decision SUPPORT tool used by a physician. You are not a \
clinician and you do not practice medicine. Follow these rules exactly, with \
no exceptions:

1. You never diagnose, never prescribe, never suggest or imply a dose, and \
never state that a treatment, medication, or course of action is right, \
wrong, appropriate, or inappropriate. You surface sourced facts from the \
patient's record; the physician makes every clinical judgment.

2. Every clinical claim you make MUST be immediately followed by its source \
in square brackets, in the form [ResourceType/id] -- for example: "the \
patient has Type 2 diabetes [Condition/abc-123]." A claim with no bracketed \
source must not be stated as fact. If a single statement draws on multiple \
sources, cite all of them, e.g. [Condition/abc-123][Encounter/xyz-789].

3. If a tool result has missing=true for a category of data, say plainly \
that this data does not exist for this patient (e.g. "No imaging reports \
are on file for this patient."). Do not speculate about why it's absent.

4. If a tool result has a non-null error, say plainly that you could not \
retrieve that data -- state only that retrieval failed, do not speculate \
about the cause. NEVER present a tool failure as "no data" and never \
silently omit it from your answer. Reporting a failure as an absence is \
fabrication by omission and is strictly forbidden.

5. Never invent, estimate, or infer a value that was not literally present \
in a tool result. If part of a question cannot be answered from retrieved \
data, say so explicitly rather than filling the gap with a plausible-sounding \
guess.

6. If a question asks you to diagnose, prescribe, recommend a dose, or judge \
whether a treatment is appropriate, decline that part of the question, state \
plainly that it is the physician's decision, and instead surface whatever \
sourced facts might help them decide.

7. If a question names or references a patient identifier that is NOT this \
session's patient_id, do not repeat, echo, or otherwise include that other \
identifier anywhere in your answer -- not even to say data for it was not \
found. State plainly that you can only address the patient scoped to this \
session and answer strictly about that patient instead. (Tool execution is \
already hard-scoped to this session's patient regardless of what you ask for \
-- this rule is about what you SAY, not what data you can reach.)

8. You may add brief general medical background to help a physician \
interpret a citable fact (for example, what an ICD-10 code or drug class \
commonly refers to in general). Any such background is not drawn from this \
patient's record and must never be blended into the same sentence as a \
chart-specific claim without that claim's own citation. Introduce it on its \
own line beginning with exactly "General context:" so it is never mistaken \
for a sourced statement about this patient -- it needs no bracketed source \
of its own, because rule 2 only requires one for claims ABOUT this patient.

9. Physician-facing presentation (between-rooms recap — read in ~5 seconds):
- Demographics from get_intake: one compact phrase only, e.g. "22-year-old \
male" or "45-year-old female". No patient_id, UUID, or other metadata \
inline.
- Diagnoses: list each condition by its `display` name on its own line with \
its [Condition/id] citation — plain names only, no active/inactive narration, \
no counts, no status commentary. Include all conditions returned by \
get_diagnosis (active, inactive, resolved) in recap/summary answers. If \
get_diagnosis has missing=true or zero records, say only "No diagnoses on \
file." Use each record's `display` field as the condition name (it may be a \
readable title or, when OpenEMR omits one, the ICD code itself). Never \
invent a name. Do not add `clinical_status`, `source_date`, or \
parenthetical status/date commentary inline.
- Medications and other facts: plain language names; avoid dumping internal \
status or recorded-date metadata inline unless the physician specifically \
asks.
- Keep "General context:" lines (rule 8) and explicit missing-data / \
retrieval-failure wording (rules 3–4) — those are appropriate as written.
"""


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


class ClinicalCoPilot:
    """Single orchestrating agent: interpret, call tools, synthesize, cite."""

    _DEFAULT_MODEL: Final[str] = "claude-sonnet-4-5"
    _MAX_TOKENS: Final[int] = 2048
    # Tool-use rounds, not raw API calls. Each round is one Claude turn; a
    # round that returns tool_use triggers another round with the results
    # fed back. Capped so a confused model can't loop indefinitely burning
    # tokens/latency against a physician who needs an answer in seconds.
    _MAX_TOOL_ITERATIONS: Final[int] = 5

    def __init__(
        self,
        openemr_client: OpenEMRClient,
        *,
        anthropic_client: AsyncAnthropic | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Create the agent.

        `openemr_client` is required and injected (never constructed here)
        so a caller controls the OpenEMR session/credentials this agent's
        tool calls run under. `anthropic_client` is optional and exists so
        tests can inject an `AsyncAnthropic` wired to a mock transport
        instead of making real API calls; when omitted, one is built from
        `api_key` or the ANTHROPIC_API_KEY environment variable.
        """
        self._openemr = openemr_client
        self._model = model or self._DEFAULT_MODEL

        if anthropic_client is not None:
            self._anthropic = anthropic_client
            return

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ClinicalCoPilotConfigError(
                "ANTHROPIC_API_KEY is not set. Set it in the environment "
                "(e.g. agent/.env) or pass api_key explicitly before "
                "constructing ClinicalCoPilot."
            )
        self._anthropic = AsyncAnthropic(api_key=resolved_key)

    def _build_system_prompt(self, patient_id: str) -> str:
        """Fixed hard rules plus the one piece of per-request context: which patient."""
        return (
            f"{_HARD_RULES}\n"
            f'You are assisting with exactly one patient for this session: '
            f'patient_id="{patient_id}". Always call tools with this '
            f"patient_id. (Even if you were to pass a different value, "
            f"tool execution is hard-scoped to this session's patient at "
            f"the data layer, not controlled by your tool-call arguments.)"
        )

    async def answer(
        self,
        patient_id: str,
        question: str,
        *,
        prefetched_chart: CachedPatientChart | None = None,
    ) -> dict[str, Any]:
        """Answer one stateless physician question about one patient.

        Thin wrapper around `chat()` with an empty history — keeps /ask and
        the eval suite on the original single-turn path.
        """
        return await self.chat(
            patient_id,
            question,
            [],
            prefetched_chart=prefetched_chart,
        )

    async def chat(
        self,
        patient_id: str,
        message: str,
        conversation_history: list[dict[str, str]],
        *,
        prefetched_chart: CachedPatientChart | None = None,
    ) -> dict[str, Any]:
        """Answer one turn in a multi-turn session about one patient.

        `conversation_history` is the prior published user/assistant turns
        for this session (plain text only — not internal tool_use blocks).
        The current `message` is appended as the latest user turn before
        the tool-calling loop runs.

        Returns a dict with three keys, per this project's verification
        design (a downstream layer re-checks every [ResourceType/id]
        citation in `answer` against `records` before anything reaches the
        physician -- this method does not self-verify):

        - "answer": the final text Claude produced.
        - "tool_calls": every tool invocation made this turn (name, the
          model's requested input, and the outcome), for audit/debugging.
        - "records": every SourcedRecord returned by every tool call this
          turn, flattened into one list -- the ground truth the
          verification layer checks citations against.

        `prefetched_chart` -- a `CachedPatientChart` from `cache.py`, when
        `main.py` already loaded the four first-visit tools (cache hit or
        post-fetch store). Tool dispatch returns those results without
        re-hitting OpenEMR; the LLM still drives which tools it asks for.
        """
        prefetch_map: dict[str, ToolResult[Any]] | None = None
        if prefetched_chart is not None:
            prefetch_map = prefetched_chart.as_prefetch_map()
        messages: list[dict[str, Any]] = []
        for turn in conversation_history:
            role = turn["role"]
            if role not in ("user", "assistant"):
                raise ValueError(f"conversation_history role must be 'user' or 'assistant', got {role!r}.")
            messages.append({"role": role, "content": turn["content"]})
        messages.append({"role": "user", "content": message})
        tool_calls_made: list[dict[str, Any]] = []
        records_retrieved: list[SourcedRecord] = []
        final_text: str | None = None

        for _ in range(self._MAX_TOOL_ITERATIONS):
            # Traced as a Langfuse generation span (observability.py) --
            # never with the actual prompt/response text as input/output,
            # only token counts/cost/stop_reason: `messages` and `system`
            # carry the physician's question and, once tools have run,
            # patient chart data pulled straight from OpenEMR. See
            # observability.py's module docstring for why that boundary is
            # structural (an allowlist), not a discipline applied here.
            with observability.trace_llm_call(model=self._model) as llm_span:
                response = await self._anthropic.messages.create(
                    model=self._model,
                    max_tokens=self._MAX_TOKENS,
                    system=self._build_system_prompt(patient_id),
                    messages=messages,
                    tools=TOOL_SPECS,
                )
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", None) if usage is not None else None
                output_tokens = getattr(usage, "output_tokens", None) if usage is not None else None
                usage_details, cost_details = observability.llm_usage_and_cost(
                    model=self._model, input_tokens=input_tokens, output_tokens=output_tokens
                )
                llm_span.update(
                    output={
                        "stop_reason": response.stop_reason,
                        "tool_call_count": sum(1 for block in response.content if block.type == "tool_use"),
                    },
                    usage_details=usage_details,
                    cost_details=cost_details,
                )
            # The SDK's response content blocks serialize correctly when
            # fed straight back into the next request's `messages` -- no
            # need to hand-roll a dict form of the assistant turn.
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

            if response.stop_reason != "tool_use" or not tool_use_blocks:
                final_text = _extract_final_text(response)
                break

            # Requirement 2: independent tool calls in one turn run
            # concurrently, not sequentially. The four first-visit tools
            # are independent FHIR reads with no data dependency between
            # them -- awaiting them one at a time would only add dead time
            # to a physician's seconds-level latency budget for no benefit.
            results = await asyncio.gather(
                *(self._dispatch_tool_call(block, patient_id, prefetch_map) for block in tool_use_blocks)
            )

            tool_result_content: list[dict[str, Any]] = []
            for block, result in zip(tool_use_blocks, results):
                tool_calls_made.append(
                    {
                        "tool": block.name,
                        "input": block.input,
                        "missing": result.missing,
                        "error": result.error,
                        "record_count": len(result.records),
                    }
                )
                records_retrieved.extend(result.records)
                tool_result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        # Serialized JSON, not a Python repr: Claude needs
                        # the field names and null/true/false the tool
                        # contract defines (missing/error/records), not an
                        # ad hoc string rendering of the ToolResult.
                        "content": result.model_dump_json(),
                    }
                )
            messages.append({"role": "user", "content": tool_result_content})
        else:
            # Iteration cap hit without Claude ever returning a non-tool_use
            # turn. Per rule 5 (never invent an answer), this is reported
            # honestly as an incomplete answer, not silently swallowed or
            # guessed at.
            final_text = (
                "I was unable to reach a final answer within the allotted "
                f"number of tool-call rounds ({self._MAX_TOOL_ITERATIONS}). "
                "Please try a narrower or simpler question."
            )

        return {
            "answer": final_text or "",
            "tool_calls": tool_calls_made,
            "records": records_retrieved,
        }

    async def _dispatch_tool_call(
        self,
        block: Any,
        trusted_patient_id: str,
        prefetched: dict[str, ToolResult[Any]] | None = None,
    ) -> ToolResult[Any]:
        """Execute one tool_use block against OpenEMR.

        `trusted_patient_id` -- the value `answer()` was called with, never
        the model's own tool-input value -- is what's actually used. See
        the module docstring: the LLM is never the security boundary, and
        that is enforced right here, not by asking nicely in the prompt.

        `prefetched` -- when `main.py` already assembled the four first-visit
        tool results (encrypted cache hit or fresh fetch), serve those
        without a second OpenEMR round-trip for the same data.
        """
        tool_fn = TOOL_FUNCTIONS.get(block.name)
        if tool_fn is None:
            return ToolResult(records=[], missing=False, error=f"Unknown tool '{block.name}'.")

        if prefetched is not None and block.name in prefetched:
            return prefetched[block.name]

        requested_input = block.input if isinstance(block.input, dict) else {}
        requested_patient_id = requested_input.get("patient_id")
        if requested_patient_id != trusted_patient_id:
            # Logged, never acted on. A mismatch may just mean a confused
            # model turn, or in the worst case a prompt-injection attempt
            # trying to redirect a tool at a different patient's data --
            # either way the response is identical: ignore it, use the
            # session's own patient_id.
            logger.warning(
                "Tool '%s' requested patient_id=%r but this session is scoped to %r; using the session's patient_id.",
                block.name,
                requested_patient_id,
                trusted_patient_id,
            )

        start = time.monotonic()
        # Traced as a span (observability.py) with only the outcome shape
        # -- missing/has_error/record_count -- never `result.records`
        # itself, which is exactly the patient chart data this tool call
        # just retrieved.
        with observability.trace_tool_call(tool_name=block.name) as tool_span:
            try:
                result = await tool_fn(self._openemr, trusted_patient_id)
            except Exception:
                # Tools are contracted (see tools.py) to catch their own
                # OpenEMR/parsing failures and return a populated
                # ToolResult.error rather than raising. This is a last-resort
                # safety net for a genuinely unexpected bug in a tool, not the
                # primary error-handling path -- so it's logged loudly.
                logger.exception("Tool '%s' raised unexpectedly instead of returning a ToolResult", block.name)
                result = ToolResult(records=[], missing=False, error=f"Tool '{block.name}' failed unexpectedly.")

            tool_span.update(
                output={
                    "missing": result.missing,
                    "has_error": result.error is not None,
                    "record_count": len(result.records),
                    "latency_ms": round((time.monotonic() - start) * 1000, 2),
                }
            )
        return result


def _extract_final_text(response: Message) -> str:
    """Concatenate a response's text blocks; flag truncation honestly rather than hiding it."""
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if response.stop_reason == "max_tokens":
        # A technical caveat about the response itself, not a clinical
        # claim -- appending it doesn't violate rule 5, and hiding a
        # truncated (possibly mid-citation) answer would be worse.
        text += (
            "\n\n[Note: this response was truncated because it reached the "
            "model's output length limit; it may be incomplete, including "
            "possibly a cut-off source citation.]"
        )
    return text
