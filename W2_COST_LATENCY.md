# W2_COST_LATENCY.md — Clinical Co-Pilot, Week 2

**Measured against the deployed service:** https://agent-production-b516.up.railway.app
**Date:** 2026-07-14 · **Model:** `claude-sonnet-4-6` · **Config:** `RAG_MODE=bm25`
**Rates:** $3.00 / MTok input, $15.00 / MTok output ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing))

---

## 1. Latency — measured

Five consecutive `POST /respond` calls against the deployed service, warm container,
in-scope clinical question (*"do you recommend surgery for John Doe?"*):

| Run | Wall clock |
|---|---|
| 1 | 10.16 s |
| 2 | 8.84 s |
| 3 | 10.70 s |
| 4 | 8.72 s |
| 5 | 11.58 s |

| Metric | In-scope (answered) | Out-of-scope (refused) |
|---|---|---|
| **p50** | **10.16 s** | — |
| **p95** | **11.58 s** | — |
| mean | 10.00 s | **0.12 s** |
| min / max | 8.72 s / 11.58 s | — |

Measured client-side from Austin over the public internet to Railway `sfo`, so each figure
includes round-trip network time.

### The headline number: 0.12 s vs 10.16 s

The out-of-scope refusal path is **~85× faster** than the answered path. This is not an
optimization — it is the scope classifier from §2 of the architecture doc doing its job.
An off-scope question is classified and refused by the supervisor **before any worker
runs**: no intake load, no retrieval, no LLM call.

The same control is simultaneously:

- a **safety** property (no "Verified · 0 sources" on a question the chart cannot answer),
- a **latency** property (0.12 s instead of 10 s),
- a **cost** property (**$0.00** instead of ~$0.013).

A safety control that also happens to be the cheapest and fastest path is a good sign the
boundary is drawn in the right place.

---

## 2. Bottleneck analysis

The refusal path isolates the fixed overhead cleanly, which lets the rest be attributed by
subtraction:

| Stage | Time | Basis |
|---|---|---|
| Network + FastAPI + middleware + scope classification | **~0.12 s** | measured directly (refusal path) |
| Intake load (cached JSON fixture) | <0.01 s | local file read, no I/O of consequence |
| BM25 retrieval over ~500 paragraph chunks | ~0.01–0.05 s | in-memory scoring; index built once via `lru_cache` |
| **Anthropic call (`claude-sonnet-4-6`, ≤700 output tokens)** | **~9.9 s** | remainder |

**The LLM call is ~99% of end-to-end latency.** Everything this project built — the graph,
the supervisor routing, hybrid retrieval, schema validation — costs roughly 1% of the
request. Optimizing any of it would be optimizing the wrong thing.

The dominant term inside that 9.9 s is **output token generation**, not input processing:
the answers are long, structured markdown running near the 700-token cap, and generation is
sequential. Latency here is essentially a function of how much text is asked for.

### Levers, in order of effect

1. **Cap output length.** ~700 tokens → ~350 would roughly halve wall-clock. The current
   answers are arguably too long for a physician glancing at a screen mid-clinic; this is a
   product decision that happens to also be the biggest performance lever.
2. **Stream the response.** Time-to-first-token would drop to ~1 s; total unchanged, but
   perceived latency changes completely. The single highest-value change for the actual user.
3. **Prompt caching.** The instruction block and intake summary are identical across turns
   for a session; cache reads bill at ~10% of input rate. Small absolute saving here (input
   is only ~25% of cost) but effectively free to enable.
4. **Cold start.** Not represented in these numbers — the container was warm. First request
   after a redeploy pays index-build cost on top.

### Not a bottleneck

Retrieval. BM25 over ~500 in-memory chunks is milliseconds, and the index builds once per
process. The `RAG_MODE=bm25` deployment choice (see architecture §3) removes the embedding
model entirely from the production path, so there is no model load at request time either.

---

## 3. Cost per operation

Token counts are **estimates from the prompt structure**, not metered counts. Langfuse is
wired into this service and holds the authoritative per-call usage; these figures are for
budgeting, and should be reconciled against Langfuse before being relied on.

### 3.1 Document ingestion (one-time, per document)

Six pages rendered at 150 DPI (~1275×1650 px each), all sent in a single vision call.

| Component | Est. tokens | Cost |
|---|---|---|
| 6 page images (~2,800 tok/image) | ~16,800 | $0.050 |
| Extraction instruction | ~450 | $0.001 |
| Output (strict JSON) | ~400 | $0.006 |
| **Total per intake form** | | **≈ $0.058** |

Images dominate — ~97% of ingestion input cost. Ingestion is a one-time cost per document,
not per question: the extracted facts are cached and re-read on every subsequent turn.
**Lower DPI is the obvious lever** if extraction accuracy holds at 100–120 DPI, cost drops
roughly proportionally. Untested.

### 3.2 Per question — in-scope

| Component | Est. tokens | Cost |
|---|---|---|
| Intake summary | ~120 | |
| 3 retrieved chunks (≤600 chars each) | ~450 | |
| Prompt instruction + question | ~130 | |
| **Input subtotal** | ~700 | $0.0021 |
| **Output** (≤700 cap, typically near it) | ~700 | $0.0105 |
| **Total per answered question** | | **≈ $0.013** |

**Output is ~83% of per-question cost.** The 5× output-to-input rate ratio means answer
length drives the bill far more than retrieved context does — which is why retrieving 3
chunks instead of 1 costs almost nothing, and why capping answer length is the real lever.

### 3.3 Per question — out-of-scope

**$0.00.** No LLM call is made.

---

## 4. Actual development spend

| Item | Cost |
|---|---|
| Intake extractions during development (~6 runs × $0.058) | ~$0.35 |
| `/respond` calls during development and testing (~40 × $0.013) | ~$0.52 |
| Week 1 live eval suite runs (real OpenEMR + Anthropic, ~3 runs) | ~$0.30 |
| Embedding model (`all-MiniLM-L6-v2`, local) | $0.00 |
| BM25 / retrieval | $0.00 |
| Offline eval gate (24 cases, zero API calls) | $0.00 |
| **Estimated Week 2 Anthropic spend** | **≈ $1.20** |

Railway hosting (agent + OpenEMR + MySQL) runs on existing free/hobby allocation.

**The gate costing $0.00 is a design outcome, not luck.** The 24-case offline suite runs on
every push and spends nothing, because it tests schema contracts, citation shape, routing,
and log hygiene — not model behavior. The Week 1 live suite, which *does* spend tokens, runs
pre-deploy only. Had the gate been built on the live suite, it would cost real money on
every commit and take 7 minutes instead of 0.4 seconds.

---

## 5. Projected production cost

Assumptions: one intake form ingested per new patient; physicians ask ~4 questions per
patient encounter; ~15% of questions are out-of-scope (free, per §3.3).

| Scale | Ingestion | Questions | **Monthly total** |
|---|---|---|---|
| 100 patients/mo (400 questions) | $5.80 | $4.42 | **≈ $10.22** |
| 1,000 patients/mo (4,000 questions) | $58.00 | $44.20 | **≈ $102.20** |
| 10,000 patients/mo (40,000 questions) | $580.00 | $442.00 | **≈ $1,022.00** |

Cost scales linearly — there is no fixed inference cost, and retrieval is free.

**Ingestion is ~57% of projected spend**, which is counterintuitive: the expensive operation
is reading the document once, not answering questions about it. Two levers follow directly:

- **Lower render DPI** — ingestion cost is ~97% image tokens.
- **Route extraction to a cheaper model** — extraction is a transcription task constrained by
  a strict schema that rejects malformed output. Haiku 4.5 ($1/$5) would cut ingestion cost
  ~3×, and the schema is already the safety net that would catch a weaker model's mistakes.
  Worth testing; not tested.

### What is not modeled

- **Cohere rerank is now enabled in production** but not priced above. It adds one API call per
  in-scope question (10 short documents), and a small latency increment. Not re-measured — the
  p50/p95 figures in §1 predate it.
- FHIR write-back to OpenEMR (not implemented)
- **Lab PDF ingestion is implemented but not included in the projection above.** A second
  document per patient would roughly double per-patient ingestion cost (~$0.058 → ~$0.116),
  taking the 1,000-patient/mo projection from ~$102 to ~$160.
- Prompt caching (not enabled)
- Production-tier Railway hosting

---

## 6. Honest limitations of this report

1. **Token counts are estimated, not metered.** Langfuse holds the authoritative numbers for
   this service; these are derived from prompt structure. Directionally sound, not audited.
2. **n=5 for latency.** Enough to see that the spread is tight (8.7–11.6 s) and that the
   refusal path is a different order of magnitude. Not enough for a real p95 — that number is
   "the slowest of five," not a distribution tail.
3. **Warm container only.** Cold-start latency after a redeploy is unmeasured.
4. **No per-step instrumentation.** The stage breakdown in §2 is attribution by subtraction
   from the refusal-path baseline, not measured spans. Per-worker tracing spans are listed as
   unimplemented in architecture §7.
5. **Single scenario.** All measurements use one patient and one class of question.
