# ARCHITECTURE.md — Clinical Co-Pilot Agent Plan

Traces to USERS.md. Every capability points to a use case there.

## Summary

**Problem context.** USERS.md frames Dr. Alvarez's constraint: ~90 seconds between rooms to reload one patient's scattered story. The Clinical Co-Pilot (`agent/`) answers her questions by synthesizing sourced facts from OpenEMR — not another retrieval dashboard. Trust is the problem: a confident, uncited, or hallucinated claim can harm a patient more than a slow or empty answer.

**Audit findings → consequences.** AUDIT.md: OpenEMR's two PEPs (OAuth2 scopes + legacy ACL) are real but bypassable via local/`skipAuthorization` paths — `openemr_client.py` uses only `/oauth2/default/token` and `/apis/default/fhir/*`. No caching layer and 10–40+ queries per patient picture mean live synthesis cannot hold a seconds bar without a future cache. Zero foreign keys and inconsistent field population mean tools treat missing/malformed FHIR as normal (`tools.py`, `ToolResult`). Default `api_log_option=2` persists plaintext API response bodies — a PHI-store pattern `observability.py` avoids. No BAA-covered LLM path exists; PHI to Anthropic requires an executed BAA before production.

**Architecture decisions.** One `ClinicalCoPilot` (`agent/agent.py`). Four typed read-only tools — `get_intake`, `get_diagnosis`, `get_treatment`, `get_testing` (`tools.py`, `TOOL_SPECS`) — returning `SourcedRecord` subclasses (`schemas.py`), run in parallel via `asyncio.gather`. LLM: `claude-sonnet-4-5` wired today; Haiku routing and Opus escalation are documented (section 3) but not implemented.

**Why this architecture.** Bounded to retrieval + synthesis over one record. Every extra agent is another unsourced failure point. Simplicity is a safety feature when a wrong answer means patient harm.

**Security / data access.** The LLM is never the security boundary (`agent.py` `_dispatch_tool_call` hard-scopes to the session's `patient_id`). `openemr_client.py` requests minimum-necessary read-only `user/*.read` scopes — never `system/*`. `patient_exists()` runs unconditionally in `main.py` before any tool and is absent from `TOOL_SPECS`. MVP tradeoff: OAuth2 password grant (`openemr_client.py`); production would use authorization-code flow.

**Failure modes.** `ToolResult` separates `missing` from `error` — a broken tool cannot read as a clean chart (system-prompt rule 4). Third state from testing: FHIR search returns HTTP 200 + empty Bundle for an invalid patient ID, indistinguishable from a valid patient with no records — hence `patient_exists()` before trusting any `missing=True`.

**Verification.** `verification.py` runs after `answer()` and before `/ask` returns (`main.py`). Claims must cite `[ResourceType/id]` against retrieved records; hallucinated sources are worse than uncited claims because they look verified. Prescriptive language checked deterministically. Honest limit: checks attribution, not fidelity.

**Observability.** Langfuse (`observability.py`), PHI redacted via metadata allowlist — not OpenEMR's response-body logging. `correlation_id` joins JSON logs to traces. Langfuse is self-hostable; per AUDIT.md finding 5, a SaaS tracer with PHI is a second BAA-triggering disclosure.

**Evals.** `agent/evals/`: invariants, boundaries, adversarial, regression against live OpenEMR + Anthropic. Real findings: adversarial suite caught echoing a foreign patient's UUID while access control held (fixed, rule 7); `_parse_condition` dropped Conditions with empty ICD-10 display — audit data-quality finding biting in production (fixed).

**Cost strategy.** Sonnet workhorse today; Haiku/Opus tiering planned. Prompt caching is the dominant lever once implemented (`TOOL_SPECS` + system prompt repeat per turn). No PHI cache yet.

**MVP tradeoffs + future.** Password grant; synthetic demo patients; no cache, `get_visit`, Recap, or Trend yet. Next: those capabilities; consent-gated history tools; authorization-code OAuth; encrypted short-TTL cache per AUDIT.md.

## 1. Where the agent lives
A separate service alongside OpenEMR, reading patient data through OpenEMR's REST/FHIR API layer so it inherits OpenEMR's existing access controls (OAuth2 scopes + legacy ACL, the two Policy Enforcement Points found in the audit) rather than reinventing them.

## 2. Agent design — ONE agent, typed tools
Single agent orchestrates: interprets the question, decides which tools to call, synthesizes the answer. Single (not multi-agent) because the task is bounded to retrieval + synthesis over one record; every extra agent is another unsourced failure point. All tools READ-ONLY, return typed records with source IDs. Tools retrieve; humans decide.
First-visit tools (parallel): get_intake, get_testing, get_diagnosis, get_treatment. Follow-up: get_visit; get_medical_history, get_family_history (consent-gated). Capabilities (not tools): Recap and Trend.

## 3. LLM selection — tiered
Claude Sonnet workhorse (tool-calling + synthesis; strong structured output, fast, ~5x cheaper than Opus); Haiku for routing; Opus for hard reasoning only. Prompt caching (~90% off cached input) is the main cost lever.

## 4. Verification
Source attribution: every claim maps to a record ID; unattributable claims not stated as fact; runs after generation, before display. Domain-constraint enforcement in code (dosage/interaction). Agent surfaces sourced patterns; physician draws every clinical conclusion. Human-in-the-loop non-negotiable.

## 5. Trust boundaries
LLM is never the security boundary. Access filtered at the data layer before data reaches the model, so prompt injection cannot leak what was never loaded. Every tool call runs in the requesting user's authorization context. No prompt-based authorization.

## 6. Failure modes
Tool fails -> say so, no guess. Record incomplete -> surface the gap. Ambiguous query -> clarify. Unauthorized -> refuse at the data layer.

## 7. Tradeoffs
Single vs multi-agent: chose single. Speed vs completeness: pre-compute summaries (freshness tradeoff) — justified by audit finding of no caching layer and 10-40 queries per patient picture. OpenEMR data access via API layer. LLM as security boundary: never.
