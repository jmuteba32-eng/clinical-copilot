# Clinical Co-Pilot — OpenEMR AI Agent

A trust-first AI agent for OpenEMR that gives a physician fast, sourced patient context in the ~90 seconds between rooms (see USERS.md).

## Live deployments

| Service | URL | Login |
|---------|-----|-------|
| **OpenEMR** | https://openemr-production-10ed.up.railway.app | `admin` / `ClinicalCoPilot2026!` |
| **Clinical Co-Pilot agent** | https://agent-production-b516.up.railway.app | UI: same credentials as OpenEMR demo |

**Physician UI:** open the agent URL in a browser (`GET /`) — login, pick a patient, multi-turn chat with verification badges.

**Agent endpoints**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Chat UI (login, patient picker, session-scoped chat) |
| `GET` | `/health` | Liveness — process is up |
| `GET` | `/ready` | Readiness — Anthropic key + OpenEMR auth |
| `GET` | `/baa-status` | `{"baa_confirmed": bool}` — admin BAA gate state |
| `POST` | `/login` | UI session token |
| `POST` | `/logout` | End UI session |
| `GET` | `/patients` | Patient picker list (id, name, age, sex) |
| `GET` | `/audit` | Access audit log (JSON, newest first; requires UI session) |
| `POST` | `/ask` | Single turn: `{"patient_id": "<uuid>", "question": "..."}` |
| `POST` | `/chat` | Multi-turn: `patient_id`, `message`, `conversation_history` |

Try `/ready` first. API-only: `/ask` or `/chat` with a patient UUID from `python -m agent.list_patients`. UI: sign in at `/` and select a patient.

## Architecture (what's built)

Single `ClinicalCoPilot` agent (`agent/agent.py`) with four typed read-only FHIR tools — `get_intake`, `get_diagnosis`, `get_treatment`, `get_testing` — run in parallel (`asyncio.gather`). Every tool returns `SourcedRecord` instances with required `source_id` / `source_type`.

- **Verification before display** — `verification.py` checks citations and prescriptive language after generation, before `/ask` or `/chat` returns; follow-ups verify against the session chart, not only the current turn's records
- **Patient-existence gate** — `patient_exists()` runs before any tool; not in `TOOL_SPECS`; one automatic retry on cold-start timeout
- **BAA compliance gate** — `BAA_CONFIRMED` env (admin-only, default `false`). When not true, `/ask` and `/chat` refuse all PHI retrieval before OpenEMR (fail-closed) with a message routing to compliance/legal. This is enforcement, not a signed BAA. Blocked requests audit as `baa_gate_blocked`
- **Audit trail** — append-only PHI-free JSONL (`audit_trail.py`): username, event_type, patient UUID, correlation_id, timestamp, outcome. `GET /audit` + UI table. Contrast: AUDIT.md finding that OpenEMR `api_log` stores response bodies
- **Encrypted PHI cache** — Fernet + short TTL when `CACHE_ENCRYPTION_KEY` is set; disables without a key (no plaintext fallback); diagnoses re-fetched every session load
- **Keep-warm** — background task pings OpenEMR token endpoint every 4 min (`KEEP_WARM_ENABLED`, default on) to reduce Railway cold-starts
- **Security** — tool calls hard-scoped to session `patient_id`; foreign UUIDs in chat text refused; OpenEMR only via authenticated FHIR path (`openemr_client.py`)
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

Open http://localhost:8000/ for the chat UI. `agent/.env` is loaded relative to `agent/main.py`, so this works regardless of CWD. Docker: `agent/Dockerfile`.

**Key env vars**

| Variable | Default | Purpose |
|----------|---------|---------|
| `BAA_CONFIRMED` | `false` | Admin attestation that a BAA exists; must be `true` for PHI retrieval |
| `CACHE_ENCRYPTION_KEY` | — | Fernet key; cache off if unset |
| `AUDIT_LOG_PATH` | `agent/audit_log.jsonl` | Append-only audit log path |
| `KEEP_WARM_ENABLED` | `true` | Background OpenEMR keep-warm loop |
| `AGENT_UI_USERNAME` / `AGENT_UI_PASSWORD` | `admin` / `ClinicalCoPilot2026!` | Physician UI login |

## Run evals

Boundary, invariant, adversarial, and regression suites against live OpenEMR + Anthropic:

```bash
python -m agent.evals.run_evals
```

Costs real API tokens. See `agent/evals/`.

## Known gaps (MVP)

- OAuth2 password grant (MVP only; production needs authorization-code flow)
- Sonnet-only (`claude-sonnet-4-5`); Haiku/Opus tiering documented in ARCHITECTURE.md, not wired
- Cost attribution to Langfuse not fully wired
- `get_visit`, Recap, Trend not built

## Data & compliance

**Demo / synthetic patients only — no real PHI.** A signed BAA with the configured LLM provider (and any SaaS observability vendor receiving metadata) is required before production PHI use. `BAA_CONFIRMED=true` is administrator attestation that such agreements exist — not a substitute for executing them. Set it only after compliance/legal confirms.
