# Clinical Co-Pilot — OpenEMR AI Agent

A trust-first AI agent for OpenEMR that gives a physician fast, sourced patient context in the ~90 seconds between rooms (see USERS.md).

## Live deployments

| Service | URL | Login |
|---------|-----|-------|
| **OpenEMR** | https://openemr-production-10ed.up.railway.app | `admin` / `ClinicalCoPilot2026!` |
| **Clinical Co-Pilot agent** | https://agent-production-b516.up.railway.app | — |

**Agent endpoints**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness — process is up |
| `GET` | `/ready` | Readiness — Anthropic key + OpenEMR auth |
| `POST` | `/ask` | `{"patient_id": "<uuid>", "question": "..."}` → answer, tool_calls, verification, correlation_id |

Try `/ready` first, then `/ask` with a patient UUID from `python -m agent.list_patients`.

## Architecture (what's built)

Single `ClinicalCoPilot` agent (`agent/agent.py`) with four typed read-only FHIR tools — `get_intake`, `get_diagnosis`, `get_treatment`, `get_testing` — run in parallel (`asyncio.gather`). Every tool returns `SourcedRecord` instances with required `source_id` / `source_type`.

- **Verification before display** — `verification.py` checks citations and prescriptive language after generation, before the response leaves `/ask`
- **Patient-existence gate** — `patient_exists()` in `main.py` runs before any tool; not in `TOOL_SPECS` (the model never decides whether to validate)
- **Security** — tool calls hard-scoped to the session's `patient_id`; OpenEMR access only via authenticated FHIR path (`openemr_client.py`)
- **Observability** — Langfuse tracing with PHI redaction (`observability.py`); `correlation_id` on every log line and `X-Correlation-ID` header

Details: ARCHITECTURE.md. Production alerts: agent/alerts.md.

## Docs

| File | Contents |
|------|----------|
| [USERS.md](USERS.md) | Target user, 90-second workflow, use cases |
| [AUDIT.md](AUDIT.md) | OpenEMR fork audit (security, performance, data quality, compliance) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Agent plan and design decisions |
| [agent/alerts.md](agent/alerts.md) | Three production alerts (verification rate, latency, readiness) |

## Run locally

From repo root (requires `agent/.env` with OpenEMR + Anthropic credentials):

```bash
uvicorn agent.main:app --reload
```

`agent/.env` is loaded relative to `agent/main.py`, so this works regardless of CWD. Docker: `agent/Dockerfile`.

## Run evals

Boundary, invariant, adversarial, and regression suites against live OpenEMR + Anthropic:

```bash
python -m agent.evals.run_evals
```

Costs real API tokens. See `agent/evals/`.

## Known gaps (MVP)

- No PHI cache (audit recommends one; not built)
- OAuth2 password grant (MVP only; production needs authorization-code flow)
- Sonnet-only (`claude-sonnet-4-5`); Haiku/Opus tiering documented in ARCHITECTURE.md, not wired
- Cost attribution to Langfuse not fully wired

## Data & compliance

**Demo / synthetic patients only — no real PHI.** Per project rules, assume a signed BAA with LLM providers and no training use before sending clinical content to an external model.
