# ARCHITECTURE.md — Clinical Co-Pilot Agent Plan

Traces to USERS.md. Every capability points to a use case there.

## Summary — key decisions
- Single agent + typed read-only tools — bounded task; fewer moving parts when a wrong answer means patient harm.
- Verification before display — every claim maps to a source ID; clinical constraints checked deterministically in code.
- Access enforced at the data layer — the LLM is never the security boundary.
- Missing data surfaced, never filled — a confident guess is worse than an admitted gap.

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
