# ARCHITECTURE.md — Clinical Co-Pilot Agent Plan

Traces to USERS.md. Every capability points to a use case there.

## Summary

**Problem context.** USERS.md frames Dr. Alvarez's constraint: ~90 seconds between rooms to reload one patient's scattered story. The Clinical Co-Pilot (`agent/`) answers her questions by synthesizing sourced facts from OpenEMR — not another retrieval dashboard. Trust is the problem: a confident, uncited, or hallucinated claim can harm a patient more than a slow or empty answer.

**Audit findings → consequences.** AUDIT.md: OpenEMR's two PEPs (OAuth2 scopes + legacy ACL) are real but bypassable via local/`skipAuthorization` paths — `openemr_client.py` uses only `/oauth2/default/token` and `/apis/default/fhir/*`. No caching layer and 10–40+ queries per patient picture mean live synthesis cannot hold a seconds bar without the agent's encrypted cache. Zero foreign keys and inconsistent field population mean tools treat missing/malformed FHIR as normal (`tools.py`, `ToolResult`). Default `api_log_option=2` persists plaintext API response bodies — contrast `observability.py` metadata logging and the agent's PHI-free audit trail (`audit_trail.py`). PHI to the configured LLM requires an executed BAA; `baa_gate.py` fail-closes retrieval until an admin sets `BAA_CONFIRMED` (enforcement, not the contract itself).

**Architecture decisions.** One `ClinicalCoPilot` (`agent/agent.py`). Four typed read-only tools — `get_intake`, `get_diagnosis`, `get_treatment`, `get_testing` (`tools.py`, `TOOL_SPECS`) — returning `SourcedRecord` subclasses (`schemas.py`), run in parallel via `asyncio.gather`. LLM: `claude-sonnet-4-5` wired today; Haiku routing and Opus escalation are documented (section 3) but not implemented.

**Why this architecture.** Bounded to retrieval + synthesis over one record. Every extra agent is another unsourced failure point. Simplicity is a safety feature when a wrong answer means patient harm.

**Security / data access.** The LLM is never the security boundary (`agent.py` `_dispatch_tool_call` hard-scopes to the session's `patient_id`). `openemr_client.py` requests minimum-necessary read-only `user/*.read` scopes — never `system/*`. `patient_exists()` runs unconditionally in `main.py` before any tool and is absent from `TOOL_SPECS`. **BAA gate** (`baa_gate.py`): admin env `BAA_CONFIRMED` (default `false`) — not a legal agreement, only a fail-closed enforcement flag; when unset, `/ask` and `/chat` refuse all PHI retrieval before `patient_exists` or OpenEMR and route users to compliance/legal. MVP tradeoff: OAuth2 password grant (`openemr_client.py`); production would use authorization-code flow.

**Failure modes.** `ToolResult` separates `missing` from `error` — a broken tool cannot read as a clean chart (system-prompt rule 4). Third state from testing: FHIR search returns HTTP 200 + empty Bundle for an invalid patient ID, indistinguishable from a valid patient with no records — hence `patient_exists()` before trusting any `missing=True`. **BAA not confirmed** → hard refusal, no tools, no OpenEMR (fail-closed).

**Verification.** `verification.py` runs after `answer()` / `chat()` and before `/ask` or `/chat` returns (`main.py`). Claims must cite `[ResourceType/id]` against retrieved records (session chart unioned on follow-ups via `records_for_verification()`); hallucinated sources are worse than uncited claims because they look verified. `General context:` explanatory lines exempt per rule 8. Prescriptive language checked deterministically. Honest limit: checks attribution, not fidelity.

**Observability.** Langfuse (`observability.py`), PHI redacted via metadata allowlist — not OpenEMR's response-body logging. `correlation_id` joins JSON logs to traces. Langfuse is self-hostable; per AUDIT.md finding 5, a SaaS tracer with PHI is a second BAA-triggering disclosure. **Agent audit trail** (`audit_trail.py`): append-only PHI-free JSONL (username, event_type, patient UUID, correlation_id, timestamp, outcome); motivated by AUDIT.md finding that OpenEMR `api_log` stores full response bodies. Admin-only `GET /audit` + UI table view.

**Evals.** `agent/evals/`: invariants, boundaries, adversarial, regression against live OpenEMR + Anthropic. Real findings: adversarial suite caught echoing a foreign patient's UUID while access control held (fixed, rule 7); `_parse_condition` dropped Conditions with empty ICD-10 display — audit data-quality finding biting in production (fixed).

**Cost strategy.** Sonnet workhorse today; Haiku/Opus tiering planned. Prompt caching is the dominant lever once implemented (`TOOL_SPECS` + system prompt repeat per turn). **Encrypted PHI cache** (`cache.py`): Fernet + short TTL when `CACHE_ENCRYPTION_KEY` is set; disables entirely without a key (no plaintext fallback); diagnoses re-fetched on every session load so problem-list edits are not masked.

**MVP tradeoffs + future.** Password grant; synthetic demo patients; no `get_visit`, Recap, or Trend yet. **Shipped:** multi-turn `/chat`, physician UI at `GET /` (login, patient picker, session lock), keep-warm task for OpenEMR cold-starts (`keep_warm.py`), BAA gate, audit trail. Next: those missing capabilities; consent-gated history tools; authorization-code OAuth.

## 1. Where the agent lives
A separate service alongside OpenEMR, reading patient data through OpenEMR's REST/FHIR API layer so it inherits OpenEMR's existing access controls (OAuth2 scopes + legacy ACL, the two Policy Enforcement Points found in the audit) rather than reinventing them.

## 2. Agent design — ONE agent, typed tools
Single agent orchestrates: interprets the question, decides which tools to call, synthesizes the answer. Single (not multi-agent) because the task is bounded to retrieval + synthesis over one record; every extra agent is another unsourced failure point. All tools READ-ONLY, return typed records with source IDs. Tools retrieve; humans decide.
First-visit tools (parallel): get_intake, get_testing, get_diagnosis, get_treatment. Follow-up: get_visit; get_medical_history, get_family_history (consent-gated). Capabilities (not tools): Recap and Trend. **HTTP surface:** `POST /ask` (single turn), `POST /chat` (multi-turn with `conversation_history`); physician UI at `GET /` (login, patient picker, per-patient session lock).

## 3. LLM selection — tiered
Claude Sonnet workhorse (tool-calling + synthesis; strong structured output, fast, ~5x cheaper than Opus); Haiku for routing; Opus for hard reasoning only. Prompt caching (~90% off cached input) is the main cost lever.

## 4. Verification
Source attribution: every claim maps to a record ID; unattributable claims not stated as fact; runs after generation, before display. Domain-constraint enforcement in code (dosage/interaction). Agent surfaces sourced patterns; physician draws every clinical conclusion. Human-in-the-loop non-negotiable.

## 5. Trust boundaries
LLM is never the security boundary. Access filtered at the data layer before data reaches the model, so prompt injection cannot leak what was never loaded. Every tool call runs in the requesting user's authorization context. No prompt-based authorization. **BAA gate** blocks all PHI paths when `BAA_CONFIRMED` is not true — enforcement only; a signed BAA remains a legal contract outside this codebase. **Session lock** (`session.py`, `/chat`): foreign patient UUIDs in user text refused before tools; tool dispatch hard-scoped to session `patient_id`.

## 6. Failure modes
Tool fails -> say so, no guess. Record incomplete -> surface the gap. Ambiguous query -> clarify. Unauthorized -> refuse at the data layer. BAA not confirmed -> refuse before OpenEMR; audit `baa_gate_blocked`. Cache unavailable (no key) -> live fetch only, no plaintext cache.

## 7. Tradeoffs
Single vs multi-agent: chose single. Speed vs completeness: encrypted short-TTL chart cache (`cache.py`) plus keep-warm pings reduce repeat latency and OpenEMR cold-starts; diagnoses always refreshed from FHIR. OpenEMR data access via API layer. LLM as security boundary: never. Agent audit log: metadata-only JSONL (contrast OpenEMR `api_log` PHI pattern); not a substitute for enterprise audit infrastructure.
