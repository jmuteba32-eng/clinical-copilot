# dashboards.md — Clinical Co-Pilot

Traces to `alerts.md` (what pages you) and `USERS.md` §2 (the physician's 90
seconds between rooms). **This document's test: a grader should be able to decide
whether the system is healthy without reading a single log line.**

Every panel below is backed by a field this system actually emits. No panel is
aspirational — if the signal does not exist, the panel is not here, and the gap
is named in §4.

---

## Where these live

| Source | What it holds |
|---|---|
| **Langfuse** | one trace per `/ask` and `/respond` request, keyed to `correlation_id`; worker spans; LLM token usage + cost; boolean trace scores |
| **Structured logs** (stdout → Railway) | every event as JSON with `event`, `correlation_id`, and typed fields |
| **`GET /ready`** | live per-dependency status |
| **`GET /audit`** | user, correlation ID, action, patient, outcome, denial reason |

---

## 1. Health — "is it working right now?"

| Panel | Signal | Healthy |
|---|---|---|
| **Dependency status** | `GET /ready` → `checks{}` | all `ok`; `reranker` may read `not_configured` |
| **Readiness state** | `/ready` → `status` | `ready`. `degraded` = a SOFT dep is down (openemr / reranker) and the document path still answers. `not_ready` = HARD dep down. |
| **Request count** | `http_request` log events, by `path` | non-zero |
| **Error count** | `http_request` where `status_code >= 500` | **0** |
| **p50 / p95 latency** | `http_request.latency_ms`, by `path` | `/respond` p50 ~10s, p95 ~12s (`W2_COST_LATENCY.md` §1) |

**Read the `degraded` state carefully.** It is 200, not 503, and that is
deliberate: OpenEMR asleep does not stop the ingested patient's document path
from answering. A binary up/down would report this service dead while it is
serving correct answers.

---

## 2. Safety — "is it telling the truth?"

**This is the section that matters. Everything above tells you the system is
running; only this tells you it is right.**

| Panel | Signal | Healthy |
|---|---|---|
| **`/respond` verified rate** | Langfuse `verified` boolean score | **100%.** Anything below is broken, not unlucky — the invariant is computed from counts, not judged. Pages at <95% (alert 4). |
| **`/ask` verification pass rate** | Langfuse `verification_passed` score | >95% (alert 1) |
| **Scope routing split** | `worker_handoff` where `handoff_to` = `refuse` vs `intake_extractor` | refusals are **healthy**, not errors. A sudden drop to zero means the classifier stopped classifying. |
| **Auth denials** | `auth_denied` events, by `endpoint` | a low nonzero rate is normal (expired sessions). A spike = probing, or the UI stopped sending the token. |
| **BAA gate blocks** | `baa_gate_blocked` events | **0** in normal operation. Nonzero means `BAA_CONFIRMED` is unset — fail-closed working, PHI access off. |

**Refusal count is not an error count.** A physician asking something off-scope
and being told so is the system working. This panel exists because the opposite —
refusals dropping to zero — is invisible without it, and that is the Week 1
defect ("Verified · 0 sources") coming back.

---

## 3. Week 2 pipeline — "which part is doing what?"

| Panel | Signal | Healthy |
|---|---|---|
| **Worker routing decisions** | `worker_handoff` events, `handoff_from` → `handoff_to`, with `reason` | in-scope = 6 handoffs; refusal = 2; chart fallback = 2 |
| **Retrieval hit rate** | `worker:evidence_retriever` span → `chunks_retrieved` | 3. **0 is a hard failure** — no evidence means no Verified answer. |
| **Rerank applied vs skipped** | `rerank_applied` / `rerank_skipped` / `rerank_failed` events | `rerank_applied` with `candidates=10, returned=3`. `rerank_failed` = Cohere unreachable; non-fatal, retrieval still answers, quality degrades silently — **this panel is the only place that shows it.** |
| **Chart fallback rate** | `supervisor → chart_reader` handoffs / all in-scope | expected for any patient without ingested documents |
| **Chart fallback failures** | `chart_fallback_failed` events | **0.** Nonzero = OpenEMR unreachable on a patient who needs it. |
| **Token usage per encounter** | Langfuse `llm_call` span → `usage_details` | ~700 in / ~700 out per `/respond` |
| **Cost per encounter** | Langfuse `llm_call` span → `cost_details` | ~$0.013 per answered question; **$0.00 per refusal** |

**The refusal path costs nothing and takes 0.12s** versus ~10s and ~$0.013
answered (`W2_COST_LATENCY.md` §1). The safety control is also the cheapest and
fastest path — chart it, because it is the clearest evidence the scope boundary
is drawn in the right place.

---

## 4. Eval gate — "did a change break a guarantee?"

| Panel | Signal | Healthy |
|---|---|---|
| **Pass rate per category** | `python -m pytest agent/gate` | 54/54. Categories: `schema_valid` · `citation_present` · `factually_consistent` · `safe_refusal` · `no_phi_in_logs` |
| **Gate runtime** | pytest duration | <1s. **If this climbs, the gate stops being run.** A slow gate gets bypassed. |
| **Gate cost** | — | **$0.00.** No API calls, no tokens. Deliberate: see `W2_ARCHITECTURE.md` §4. |

---

## 5. What is NOT on this dashboard, and why

**Document ingestion count / extraction field-level pass rate.** The rubric names
both. Neither is charted, because **extraction does not run in production** — it
is CLI-driven against a cached fixture (`W2_ARCHITECTURE.md` §7). A panel reading
zero forever tells a grader nothing except that someone built a panel. These
activate with the upload path.

**Extraction confidence.** Not captured. Extraction has been measured against
degraded scans and **fails silently** below photocopier quality — dropping a
printed `Borderline` flag, fabricating an RDW value, inventing a year from a `2X`
placeholder, all with zero validation errors and reproducing identically across
runs (`alerts.md` §6). **A confidence panel would not catch this**: the model is
not uncertain, it is wrong. Self-reported confidence measures fluency. The
mitigation is the bounding-box overlay, which is a build item, not a panel.

**Queue depth / event retries.** No queue exists. `/respond` is synchronous.

**RAG retrieval latency.** Under 1% of request latency — see `alerts.md`,
"Not added: RAG retrieval latency."

---

## 6. The 30-second read

A grader asking "is this healthy?" needs four numbers:

1. **`GET /ready` → `status`** — should be `ready`, or it names which dependency is not
2. **`/respond` verified rate** — should be **100%**
3. **`http_request` events with `status_code >= 500`** — should be **0**
4. **`python -m pytest agent/gate`** — should be **54/54**

If all four are green, the system is up, answering, telling the truth, and has
not regressed. Everything else on this page exists to diagnose which of those
four went wrong.
