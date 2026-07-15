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

## Week 2 — Multimodal evidence agent

**Branch:** `week2-mvp` · **Merge request:** open against `main` on GitLab
**Deployed:** https://agent-production-b516.up.railway.app (same URL, same login — the UI now runs the Week 2 pipeline)

Week 2 adds two capabilities to the Week 1 agent: it can **read clinical documents**, and it
**proves its own quality with a CI gate that blocks regressions**.

### What changed from Week 1

| | Week 1 (baseline, still present) | Week 2 (new) |
|---|---|---|
| Endpoint | `POST /chat`, `POST /ask` | `POST /respond` |
| Data source | structured OpenEMR/FHIR resources | intake-form PDF + lab report PDF (both VLM-extracted to strict schemas) + guideline corpus |
| Orchestration | single agent + tool layer | LangGraph: supervisor + 2 workers, logged handoffs |
| UI | chat → `/chat` | **chat → `/respond`** (grounded answers, click-to-source citations) |
| Evals | `agent/evals/` — live suite, ~7 min, spends tokens | `agent/gate/` — 50 offline cases, 0.8 s, $0.00, **blocks pushes** |

**The UI now calls `/respond`.** Week 1's `/chat` and `/ask` endpoints are unchanged and still
live for API/grader use, but the browser chat box runs the Week 2 pipeline. This is
deliberate: the Week 1 grader feedback was that the UI stamped "Verified · 0 sources" on
off-scope questions, and the fix has to be visible in the interface a physician actually uses.

Week 2 reuses Week 1's BAA gate, correlation-ID middleware, and PHI-safe logging rather than
reimplementing them — an unconfirmed BAA blocks `/respond` exactly as it blocks `/ask`.

### Run the Week 2 flow (graders start here)

```bash
git checkout week2-mvp
cd agent
pip install -r requirements.txt
```

**In the browser** — open https://agent-production-b516.up.railway.app, log in with the demo
credentials above, and ask:

| Ask this | Expect |
|---|---|
| `What surgical treatment is indicated for this patient's eyelid condition?` | **Verified · 3 sources** — grounded answer, all citations from `dermatochalasis_guideline.pdf`, expandable quotes |
| `do you recommend surgery for John Doe?` | Same — retrieval is query-expanded with the patient's chief concern, so it works even though the question names no condition |
| `who is LeBron James?` | **Out of clinical scope** — refused before any worker runs, never marked Verified |
| `2+2` | Same refusal |

The patient picker is **cosmetic in Week 2 mode** — `/respond` does not take a `patient_id`
and answers about the cached intake fixture. Known wart, documented in `W2_ARCHITECTURE.md` §7.

**Locally, from the repo root:**

```bash
# full pipeline, one question
python -m agent.pipeline.respond "do you recommend surgery for John Doe?"

# document extraction only: PDF -> VLM -> strict Pydantic
python -m agent.ingestion.extract "agent/ingestion/test_docs/John_Doe-Intake-Form.pdf"

# lab report extraction (second document type)
python -m agent.ingestion.extract lab_pdf

# retrieval only
python -m agent.rag.retrieve "dermatochalasis eyelid"
```

### The eval gate

50 boolean-rubric cases, all five required categories, fully offline — no OpenEMR, no
Anthropic, no tokens, ~0.8 s.

```bash
python -m pytest agent/gate -q          # run it
```

**Install the PR-blocking hook** (git hooks are not version-controlled, so a tracked copy
lives in the repo):

```bash
cp agent/gate/pre-push.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

Every `git push` now runs the gate and **aborts the push** on any failure. To verify it
blocks: add a failing assertion to any file in `agent/gate/`, commit, and attempt a push.

| Category | Cases | File |
|---|---|---|
| `schema_valid` | 5 | `agent/gate/test_schema_valid.py` |
| `citation_present` | 2 | `agent/gate/test_citation_present.py` |
| `factually_consistent` | ✅ | `agent/gate/test_grounded_answer.py` |
| `safe_refusal` | ✅ | `agent/gate/test_grounded_answer.py` |
| `no_phi_in_logs` | 3 | `agent/gate/test_no_phi_in_logs.py` |
| intake extraction schema | 4 | `agent/gate/test_intake_extraction_schema.py` |
| lab extraction schema | 8 | `agent/gate/test_lab_extraction_schema.py` |
| golden set (9 off-scope + 9 in-scope) | 18 | `agent/gate/test_golden_set.py` |

The Week 1 live suite (`agent/evals/`) is **not** in the gate — it needs live OpenEMR and
Anthropic, takes ~7 minutes, and has failed on free-tier OAuth timeouts unrelated to any code
change. It runs pre-deploy. Rationale in `W2_ARCHITECTURE.md` §4.

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | — | VLM extraction + answer composition |
| `BAA_CONFIRMED` | **yes** | `false` | BAA gate. **Unset or false blocks all PHI access, including `/respond`.** Must be `true` to run the Week 2 flow. |
| `RAG_MODE` | no | `hybrid` | `hybrid` = BM25 + local dense vectors. **`bm25` = keyword only, skips loading torch.** Production runs `bm25`. |
| `OPENEMR_BASE_URL` | Week 1 only | — | `/respond` has no OpenEMR dependency |
| `OPENEMR_CLIENT_ID` / `OPENEMR_CLIENT_SECRET` | Week 1 only | — | |
| `OPENEMR_USERNAME` / `OPENEMR_PASSWORD` | Week 1 only | — | |
| `OPENEMR_TIMEOUT_SECONDS` | no | `30` | Free-tier OpenEMR is slow; evals override to 60 |
| `CACHE_ENCRYPTION_KEY` | no | — | Week 1 encrypted PHI chart cache; unset = caching disabled |
| `AGENT_UI_USERNAME` / `AGENT_UI_PASSWORD` | no | `admin` / see above | UI login |

Local development reads `agent/.env` (never committed). Railway injects these as service
variables.

**`BAA_CONFIRMED` is the one that catches people.** It defaults to `false` — PHI access stays
off unless an administrator explicitly attests. If `/respond` returns a 403 about a Business
Associate Agreement, that is the gate working, not a bug.

> The BAA gate is a **deployment control that enforces a requirement**. It is not proof of
> HIPAA compliance and is not presented as such. The legal agreement is a separate artifact
> between the healthcare organization and its data/AI providers.

### Week 2 layout

```
agent/
├── ingestion/          # document -> VLM -> strict Pydantic
│   ├── extract.py      #   6-page PDF -> one vision call -> IntakeFormExtraction
│   ├── schemas.py      #   strict schema; inherits Week 1 SourcedRecord
│   └── test_docs/      #   John_Doe-Intake-Form.pdf, CBC_Lab_Report.pdf (synthetic)
├── rag/                # hybrid retrieval over the guideline corpus
│   ├── retrieve.py     #   paragraph chunking, BM25 + dense, citation shape
│   └── guidelines/     #   3 ophthalmology guideline PDFs
├── pipeline/
│   ├── graph.py        #   LangGraph: supervisor + intake_extractor + evidence_retriever
│   ├── answer.py       #   GroundedAnswer contract; verified is DERIVED, not settable
│   ├── respond.py      #   invokes the graph, adapts state -> GroundedAnswer
│   └── john_doe_intake.json   # cached extraction (see W2_ARCHITECTURE.md §7)
└── gate/               # offline PR-blocking eval gate
    └── pre-push.sh     #   install as .git/hooks/pre-push
```

### Week 2 documents

| Doc | Contents |
|---|---|
| `W2_ARCHITECTURE.md` | ingestion flow, worker graph, RAG design, eval gate, **known gaps (§7)**, failure modes, testing strategy |
| `W2_COST_LATENCY.md` | p50/p95 latency, bottleneck analysis, cost per operation, dev spend, production projection |

**Read `W2_ARCHITECTURE.md` §7 first.** It lists what is *not* built — FHIR write-back, the lab
PDF, the bbox overlay, Cohere rerank — plainly rather than buried. Where a
requirement is unmet, it says so.

### All synthetic data

John Doe, the intake form, and the patient records are synthetic. No real PHI is present in
this repository or either deployed instance. The intake form contains a fabricated SSN which
the extraction schema **deliberately does not extract** — minimum-necessary PHI, enforced by a
gate test.
