# W2_ARCHITECTURE.md — Clinical Co-Pilot, Week 2

**Deployed:** https://agent-production-b516.up.railway.app
**Week 2 endpoint:** `POST /respond`
**Branch:** `week2-mvp`

---

## 0. Week 1 baseline vs Week 2 behavior

Graders should be able to tell the two apart without guessing.

| | Week 1 (baseline, unchanged) | Week 2 (new) |
|---|---|---|
| Endpoints | `/ask`, `/chat`, `/login`, `/patients`, `/audit`, `/baa-status`, `/health`, `/ready` | `POST /respond` |
| Data source | Structured OpenEMR/FHIR resources via `openemr_client.py` | Uploaded documents (intake form PDF) + guideline corpus |
| Code | `agent/main.py`, `agent/agent.py`, `agent/tools.py`, `agent/schemas.py`, `agent/verification.py`, `agent/baa_gate.py`, `agent/audit_trail.py` | `agent/ingestion/`, `agent/rag/`, `agent/pipeline/`, `agent/gate/` |
| Evals | `agent/evals/` — live suite (real OpenEMR + real Anthropic) | `agent/gate/` — fast offline rubric gate, PR-blocking |

Week 2 **reuses** Week 1's BAA gate, correlation-ID middleware, and PHI-safe logging discipline rather than reimplementing them. `/respond` sits behind the same BAA gate as `/ask`: an unconfirmed BAA blocks document-grounded answers exactly as it blocks chart access.

**Note on the BAA gate (language matters):** it is a *deployment control that enforces a requirement*. It is **not** proof of HIPAA compliance and is not presented as such. The legal agreement is a separate artifact between the healthcare organization and its data/AI providers.

---

## 1. Document ingestion flow

**Two document types are implemented:** the patient intake form (`extract_intake_form`) and
the lab report (`extract_lab_report`). Both follow the identical contract described below —
render every page, one vision call, strict Pydantic validation or hard failure. The lab
schema (`LabResult` / `LabReportExtraction`) carries every PRD-required field: test name,
value, unit, reference range, collection date, abnormal flag, and source citation.

Two lab-specific decisions worth naming:

- **`abnormal_flag` is a closed set (`High` / `Low` / `Borderline` / `None`), not a boolean.**
  It is transcribed from the flag the lab itself printed, never computed from value vs
  reference range. Flattening it to `abnormal=True` would destroy the direction of the
  abnormality, which is the clinically meaningful part; computing it ourselves would mean
  silently disagreeing with the issuing lab — a fabricated claim.
- **Result values stay strings.** Real reports carry `<0.01`, `150000`, and `Not Detected`.
  Coercing to float would either crash or invent precision.

The lab fixture (`agent/ingestion/test_docs/CBC_Lab_Report.pdf`) is a synthetic CBC whose
patient is **Yash M. Patel, not John Doe**. The schema documents that a report's patient is
whoever the document says it is; reconciling report identity against chart identity is not
done (see §7).

```
intake form PDF
   └─> pymupdf renders every page -> PNG @150 DPI
        └─> single Claude vision call (all pages in one message)
             └─> raw JSON
                  └─> IntakeFormExtraction.model_validate()  ← STRICT BOUNDARY
                       └─> validated typed object, or hard failure
```

**Files:** `agent/ingestion/extract.py`, `agent/ingestion/schemas.py`

### The schema is the source of truth

Raw VLM output never bypasses validation. `extract_intake_form()` raises `ValidationError` (bad shape) or `JSONDecodeError` (not JSON) rather than coercing or best-guessing. This is deliberate: a malformed extraction must fail loudly, not silently propagate into a physician-facing claim.

`IntakeFormExtraction` inherits Week 1's `SourcedRecord`, so the existing trust guarantee — *no record without a source* — carries over structurally. `source_id`/`source_type` are required by the base class. `source_page` is added for documents, because a document citation must point at a page, not just a file.

### Multi-page extraction (a design decision, not an accident)

Early single-page extraction failed validation in a useful way: page 4 has the chief concern but no patient name; page 1 has the name but no chief concern. Both are required fields, so **no single page could satisfy the schema** — the schema correctly reported that this extraction is inherently multi-page. All six pages are now sent in one vision call and merged into one validated record.

### Minimum-necessary PHI scope

The intake form contains far more than we extract: insurance policies, pharmacy, emergency contacts, consent text, a 43-item review-of-systems checklist, **and an SSN**. The schema deliberately captures only the PRD-required clinical subset — demographics (no SSN, no address, no contact numbers), chief concern, current medications, allergies, family history.

This is a minimum-necessary decision, not an oversight. There is no clinical use case for the co-pilot knowing the patient's SSN, and one of the eval rubric categories is `no_phi_in_logs` — extracting an SSN into a fact that then flows through traces is exactly the leak that category exists to prevent. Narrower is the stronger design here.

### Explicit negatives

"None" on the allergies section is represented as a single entry `{allergen: "None"}`, **not** an empty list. "The patient stated no allergies" and "we never asked" are clinically different and must stay distinguishable downstream.

---

## 2. Worker graph

**Implemented with LangGraph** (`agent/pipeline/graph.py`). One supervisor, two workers.

```
START -> supervisor -> refuse                      -> END   (out of scope)
                    -> intake_extractor  -> supervisor
                    -> evidence_retriever -> supervisor
                    -> answer                      -> END
```

**The supervisor is not a black box.** It owns every routing decision and workers always
return to it — it decides what happens next by asking "what is still missing to answer
safely?":

| State | Supervisor decision |
|---|---|
| scope unknown | classify; `out_of_scope` → `refuse` |
| scope `out_of_scope` | `refuse` (no worker runs at all) |
| no intake facts | → `intake_extractor` |
| no evidence | → `evidence_retriever` |
| facts + evidence present | → `answer` |

Routing is explicit and rule-based rather than model-chosen, so a run is reproducible
and reviewable.

### Logged handoffs

Every decision and every worker return is (a) emitted as a structured log line on the
`agent.graph` logger carrying the request's correlation ID, and (b) appended to
`state["handoffs"]`, so the full path is reconstructable after the fact:

```json
{"event": "worker_handoff", "correlation_id": "4a0aa982-...",
 "handoff_from": "supervisor", "handoff_to": "evidence_retriever",
 "reason": "no guideline evidence yet"}
```

Handoff records carry **routing metadata only** — `from`, `to`, `reason`. No patient
state, no question text. This is enforced by a gate test (`test_no_phi_in_logs.py`),
because the handoff log is new PHI-leak surface introduced with the graph.

### Workers

| Node | Role | Backing code |
|---|---|---|
| `supervisor` | scope classification + all routing | `agent/pipeline/answer.py::classify_scope` |
| `intake_extractor` | supplies the patient's extracted intake facts | `agent/pipeline/respond.py::_load_intake` |
| `evidence_retriever` | supplies guideline evidence for the question | `agent/rag/retrieve.py::retrieve_evidence` |
| `answer` | composes the grounded answer from facts + evidence | Anthropic call, prompt-constrained to the supplied evidence |

`respond()` invokes the graph and adapts the final state into `GroundedAnswer`, which is
where the verified/citation invariant is enforced. A critic agent is extension work per
the PRD and is not built.

### Routing is inspectable in the product, not just the logs

`state["handoffs"]` is returned through `GroundedAnswer` → `RespondResponse` → the UI, which
renders it as an expandable **"Agent routing · N handoffs"** trace under every answer:
each step lists `from → to` and the supervisor's reason. The same routing-metadata-only
contract as the structured logs applies — no patient state, no question text (enforced by
`test_no_phi_in_logs.py`).

An out-of-scope question shows **2 handoffs** — `supervisor → refuse`, `refuse → END` —
which is the visible proof that no worker ran and no LLM was called. "The supervisor must
not be a black box" is not satisfied by logging alone if a reviewer cannot see it.

---

## 3. RAG design

**Files:** `agent/rag/retrieve.py`, corpus in `agent/rag/guidelines/`

**Corpus:** three ophthalmology guideline PDFs — dermatochalasis (the test patient's condition), dry eye, keratoconus.

### Chunking

**Paragraph-level**, 120–600 characters, tagged with the source page and an index within
that page (`Chunk.index`) so citation IDs stay unique (`file#p5c2`).

This replaced page-level chunking, which was the root cause of the ranking defect
described in §5.1. Pages are split on blank lines; oversized paragraphs are hard-split;
runt fragments are merged forward.

### Query expansion

Physician questions frequently omit the condition — *"do you recommend surgery?"* contains
no clinical term at all, leaving keyword retrieval nothing to match on and returning noise.
Before retrieval, the question is expanded with the patient's own extracted
`chief_concern` and `concern_symptoms`:

```
"do you recommend surgery for John Doe?"
  -> "do you recommend surgery for John Doe? Dermatochalasis visual field
      obstruction where hanging skin blocks peripheral vision"
```

Retrieval is therefore grounded in what the patient actually has, not in how the question
happened to be phrased. **The LLM prompt still receives the original question, unchanged** —
expansion affects retrieval only. This is the PRD's "query rewriting" contextual-retrieval
improvement.

**Retrieval:** hybrid — BM25 (keyword) + local dense vectors (`all-MiniLM-L6-v2` via sentence-transformers), scores min-max normalized and summed.

**Reranking:** none. The PRD names "Cohere Rerank **or equivalent**." A normalized sparse+dense hybrid is offered as the equivalent for a 3-document corpus. Cohere rerank is not wired in — tracked in §7.

**Citation shape** (matches the PRD contract):
```json
{
  "source_type": "guideline",
  "source_id": "dermatochalasis_guideline.pdf",
  "page_or_section": "page 5",
  "field_or_chunk_id": "dermatochalasis_guideline.pdf#p5",
  "quote_or_value": "Notice: Services performed for any given diagnosis must meet..."
}
```

### `RAG_MODE` — the deployment tradeoff

`sentence-transformers` pulls torch plus the full CUDA stack (~2 GB) into a CPU-only container. `RAG_MODE=bm25` makes the dense half optional: the retriever skips the model import entirely and scores BM25-only.

**Production runs `RAG_MODE=bm25`.** Local development runs full hybrid. This is an honest tradeoff — prod retrieval is keyword-only — accepted because BM25 alone ranks the correct guideline first for the test scenario, and because a lighter, reliably-building image was worth more than marginal ranking quality for this milestone.

The lazy import also keeps the offline eval gate fast: importing `agent.rag.retrieve` in the gate does not drag in torch. (Measured: gate went 0.16s → 8.19s when the import was top-level, back to 0.30s once made lazy.)

---

## 4. Eval gate

**Files:** `agent/gate/`, hook at `agent/gate/pre-push.sh`

### Two suites, deliberately separated

| | `agent/gate/` (Week 2) | `agent/evals/` (Week 1) |
|---|---|---|
| Runs | every push, blocking | pre-deploy, manual |
| Speed | ~0.3s | ~7 min |
| Network | none | live OpenEMR + live Anthropic |
| Determinism | total | subject to free-tier OAuth timeouts |

**Why:** the Week 1 live suite is not a safe foundation for a blocking gate. Observed in practice: a 60-second OpenEMR token-endpoint timeout caused a false failure that had nothing to do with any code change. A gate that flakes for infrastructure reasons either blocks good code or trains people to bypass it. The PRD's own engineering requirements ask for integration tests that "pass in CI without live API access" — the offline gate is that.

### Installation (graders)

Git hooks are not version-controlled, so a tracked copy lives at `agent/gate/pre-push.sh`:

```bash
cp agent/gate/pre-push.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

After that, every `git push` runs `python -m pytest agent/gate -q` and **aborts the push** on any failure.

### Proven, not asserted

The gate was verified by injecting a deliberate regression, attempting a push, and confirming it was blocked:

```
[pre-push] Running Week 2 offline eval gate (agent/gate/)...
.....F
FAILED agent/gate/test_schema_valid.py::test_INJECTED_REGRESSION_should_block_push
error: failed to push some refs to 'https://labs.gauntletai.com/...'
```

The commit stayed local. The stub was then reverted.

### Rubric coverage — 50 cases, all 5 categories

| Category | Cases | File |
|---|---|---|
| `schema_valid` | 5 | `test_schema_valid.py` |
| `citation_present` | 2 | `test_citation_present.py` |
| `factually_consistent` | ✅ | `test_grounded_answer.py`, `test_golden_set.py` |
| `safe_refusal` | ✅ | `test_grounded_answer.py`, `test_golden_set.py` |
| `no_phi_in_logs` | 3 | `test_no_phi_in_logs.py` |
| intake extraction schema | 4 | `test_intake_extraction_schema.py` |
| lab extraction schema | 8 | `test_lab_extraction_schema.py` |
| golden set (9 off-scope + 9 in-scope) | 18 | `test_golden_set.py` |

**50 cases, all five boolean categories.** Runs offline in ~0.8 s and spends nothing.

The golden set earned its keep immediately: it surfaced a real bug on first run. The scope
classifier matched the pronoun `"he "` as a substring, which matches inside `"the"` — so
questions were being classified in-scope for the wrong reason. Fixed with word-boundary
regex (`\b(he|his|she|her|them)\b`).

---

## 5. Known limitations

### 5.1 Retrieval quality — FIXED

**This section previously documented a live defect. It has been fixed; the record is kept
because the fix and the failed attempt before it are both part of the design rationale.**

#### The original defect

With page-level chunks, the query *"What does the guideline recommend for this patient's
dermatochalasis?"* returned:

```
[10.808] dermatochalasis_guideline.pdf p5   <- correct
[10.356] keratoconus_guideline.pdf p33      <- "APPENDIX 1. QUALITY OF OPHTHALMIC CARE"
[10.341] dry_eye_guideline.pdf p31          <- the same boilerplate, other document
```

Irrelevant boilerplate scored within 4% of the correct page. A ~2000-character page offers
BM25 dozens of common words ("patient", "care", "quality") to match. The answer model
reliably discarded the junk — *"[2] and [3] are not relevant to this patient's condition"* —
but the citations panel still surfaced it to the physician, and the Verified badge sat above
an answer stating its own sources were irrelevant. **That is the Week 1 "Verified · 0 sources"
defect in a new form**, and it was the reason to fix rather than document it.

#### Failed attempt (kept as a documented negative result)

Filtering stopwords out of the query was tried first. It was **measured as worse**: the
correct dermatochalasis page dropped from #1 to #2, an unrelated dry-eye page took the top
slot, and scores collapsed from 10.8 to 4.6. Reverted. `_STOPWORDS` and `_tokenize_query()`
remain in `retrieve.py`, unwired, with a comment recording the result.

The attempt failed because it treated a symptom (generic words matching) rather than the
cause (chunks large enough for generic words to dominate).

#### The fix

Two changes, together:

1. **Paragraph-level chunking** (§3) — term frequency means something again.
2. **Query expansion** (§3) — every retrieval carries the patient's condition.

Result for the same class of query:

```
[17.202] dermatochalasis_guideline.pdf p6
[15.776] dermatochalasis_guideline.pdf p5
[14.030] dermatochalasis_guideline.pdf p5
```

All three from the correct guideline, cleanly separated from noise instead of 4% margins.
Deployed output now quotes real clinical text — *"A common functional indication for upper
eyelid blepharoplasty is dermatochalasis, a superior visual field defect secondary to
redundant upper eyelid tissue..."* — rather than a page header.

A relative-score floor (`_MIN_RELATIVE_SCORE = 0.4`) drops weak matches. It was ineffective
against the old 4% margins and is meaningful now that scores separate.

**Residual limitation:** retrieval quality has been validated against one patient scenario
(dermatochalasis) and a three-document corpus. It is not known how it behaves at larger
corpus sizes or for conditions whose guideline vocabulary overlaps more heavily. A reranker
remains the principled next step.

### 5.2 Audit log path fallback

Deployment logs show:
```
[WARN] audit_log_path_fallback preferred_path="/data/audit_log.jsonl"
       active_path="/home/appuser/.clinical-copilot/clinical-copilot-audit.jsonl"
```
The audit log is not landing on the mounted Railway volume, so **it does not survive redeploys.** The volume is mounted; the container user likely lacks write permission on it. Not yet fixed.

### 5.3 Scope classifier is rule-based

`classify_scope()` is keyword/regex based — deterministic, zero tokens, offline-testable. It is blunt: unusual phrasing could misroute. It defaults to `out_of_scope` when nothing clinical is named, on the principle that refusing-and-labeling is safer than fabricating a Verified answer. An LLM-based classifier is the upgrade.

### 5.4 Container image is ~2 GB of unused CUDA

`requirements.txt` pins torch, so the image installs the full nvidia stack (cublas, cudnn, nccl, triton…) that a CPU-only container running `RAG_MODE=bm25` never touches. It builds and runs; it is wasteful. Split requirements would fix it.

---

## 6. Week 1 grader feedback → Week 2 fixes

| Feedback | Status |
|---|---|
| *"2+2" and "Testing" display Verified with zero sources* | ✅ Fixed structurally. `GroundedAnswer.verified` is derived in `model_post_init` from `scope == "in_scope" and len(citations) > 0`. Not settable by a caller. Verified live in the deployed UI. |
| *Off-chart questions should be rejected before the model runs* | ✅ Fixed. The supervisor classifies scope first and routes to `refuse` — no worker runs, no retrieval, no LLM call. |
| *BAA flag is not proof of HIPAA compliance* | ✅ Language corrected in this document and the demo. |
| *Audit log should capture denial reason* | ✅ **Fixed.** `_record_audit` now accepts `denial_reason`, and `AuditRecord` persists it. Auth denials audit as `outcome="denied"` with `denial_reason="unauthenticated: no valid session token"`. |
| *Eval report predates the final code* | ✅ Fixed structurally — the gate runs on every push; stale evidence cannot accumulate silently. |
| *No tests on the HTTP authorization boundary* | ✅ **Fixed — and the finding was worse than reported.** The grader flagged missing *tests*; the underlying hole was that `/ask` and `/chat` had **no auth check at all**, and `/respond` inherited the same gap. All three now reject unauthenticated requests with 401 before any work, verified in production. Found by reading this project's own audit trail, which logged `week2-respond` requests as `unauthenticated`. |

**The theme of that feedback — "make sure your evidence matches your claims" — is the organizing principle of this document.** Where a requirement is unmet, it is listed as unmet.

---

## 7. Not implemented (required by the PRD)

Stated plainly rather than buried.

1. **FHIR persistence.** Extracted intake facts are **not** written back to OpenEMR as FHIR
   resources, and the source document is not stored in OpenEMR. The `intake_extractor`
   worker reads a cached JSON fixture (`agent/pipeline/john_doe_intake.json`) produced by a
   real extraction run. The loader is a deliberate seam: swapping it for an OpenEMR read
   changes nothing else in the graph.
2. **Visual PDF bounding-box overlay.** Required by the citation contract. Not built.
   `source_page` and `Chunk.index` exist as the anchors it would hang from. Click-to-source
   snippets **are** implemented in the UI (expandable citation quotes); the box overlay is
   the missing half.
3. **Cohere rerank.** Hybrid BM25 + dense is offered as the PRD's "or equivalent";
   production runs BM25-only (§3, `RAG_MODE`). A reranker is not wired in.
4. **Patient scoping on the Week 2 path.** `/respond` does not take a `patient_id` — it
   answers about the cached intake fixture. The UI's patient selector is therefore
   **cosmetic in Week 2 mode**. This is a real wart, and it resolves naturally when FHIR
   persistence lands (item 1).
5. **Engineering requirements** — OpenAPI 3.0 spec, `/ready` extension for Week 2
   dependencies (vector index, reranker reachability), per-worker tracing spans, dashboards
   and alerts, Postman/Bruno collection, backup/RPO/RTO, baseline CPU/memory profiles: not
   addressed.
6. **Audit log volume mount.** See §5.2 — the audit log does not survive redeploys.
7. **Lab-to-chart identity reconciliation.** `extract_lab_report` transcribes whichever patient
   the report names. Nothing checks that a report's patient matches the session's patient. In a
   real deployment that check is mandatory before a lab value is shown next to a chart.

---

## 8. Testing strategy

| Layer | What | Where |
|---|---|---|
| Unit / contract | Schema validators — required fields, rejection of malformed extractions, citation shape | `agent/gate/test_schema_valid.py`, `test_intake_extraction_schema.py` |
| Unit / behavior | Scope routing, verified-requires-citation invariant | `agent/gate/test_grounded_answer.py` |
| Retrieval | Citation completeness; BM25 ranks the right guideline over a stub corpus | `agent/gate/test_citation_present.py` |
| Live behavioral | Prompt injection, cross-patient leakage, invariants, refusals against real OpenEMR + Anthropic | `agent/evals/` (Week 1, pre-deploy only) |

**Every gate test documents the failure mode it guards.** Example: `test_in_scope_without_citations_is_not_verified` guards the exact Week 1 defect the grader flagged.

**Deliberately not tested:**
- **The VLM's extraction accuracy** — asserting Claude reads "Dermatochalasis" off page 4 would be a live, token-spending, nondeterministic test. The gate instead asserts that *whatever* the VLM returns must satisfy the schema. The schema is the contract; the model is not.
- **Real PDF + real embedding model in the gate** — the citation test uses a stub corpus to stay offline and sub-second.

---

## 9. Failure modes and recovery

| Failure | How to identify | Recovery |
|---|---|---|
| Extraction returns malformed JSON | `JSONDecodeError` from `extract_intake_form` | Re-run; if persistent, inspect the rendered page image — likely a scan-quality issue |
| Extraction violates schema | `ValidationError` naming the field | **Do not widen the schema to make it pass.** Check whether the field is genuinely absent from the document (see the multi-page finding, §1) |
| Retrieval returns nothing | `retrieve_evidence()` returns `[]` (top score ≤ 0) | Answer cannot be Verified — correct behavior. Check the corpus loaded: `RuntimeError: No guideline text found` means the guidelines directory is empty |
| Retrieval returns irrelevant chunks | Citations name a guideline unrelated to the question | Known — §5.1. The answer model flags them; the sources panel does not |
| BAA gate blocks a request | `baa_gate_blocked` event, HTTP 403 | Confirm `BAA_CONFIRMED=true` is set in the environment. Blocking on an unset value is intentional — PHI access stays off by default |
| Off-scope answer marked Verified | Would require `verified=True` with `scope="out_of_scope"` | **Structurally impossible** — derived in `model_post_init`. If ever observed, `model_post_init` has been bypassed |
| OpenEMR OAuth timeout | `Timed out contacting OpenEMR's token endpoint after 60.0s` | Free-tier cold start. Wake the instance and retry; does not affect `/respond`, which has no OpenEMR dependency |
| Audit log missing after redeploy | `audit_log_path_fallback` warning at startup | Known — §5.2 |

---

## 10. Correlation IDs

Every `/respond` request carries a correlation ID through Week 1's existing middleware and returns it in the response body. The request log records `question_length`, never the question text — free-text physician questions can restate chart specifics ("does his A1c look better than last visit"), so question text is treated as PHI-adjacent and kept out of logs entirely. This matches `/ask`.

**Gap:** the correlation ID is not yet propagated into the VLM call, the retrieval call, or per-step spans. A full multi-step trace is not reconstructable from the ID alone.

---

## 11. Authorization

Every PHI-touching endpoint (`/ask`, `/chat`, `/respond`) rejects unauthenticated requests
with **401** before doing any work. Order per request: **auth check → BAA gate → handler**.

The two controls are orthogonal and both must pass:

- **Auth** asks *"is this caller allowed to?"* — a valid UI session bearer token.
- **The BAA gate** asks *"is this deployment allowed to touch PHI at all?"* — an administrator's
  attestation. It is a fail-closed deployment control, not proof of HIPAA compliance.

Denials are audited with `outcome="denied"` and an explicit `denial_reason`, and logged as an
`auth_denied` event carrying the correlation ID.

**How this was found.** The Week 1 grader noted that tests did not cover the authorization
boundary on `/ask` and `/chat`. The actual defect was larger: those endpoints had no auth check
at all, and `/respond` was built mirroring their structure and inherited the gap. It surfaced
while reading this project's own audit trail, which recorded `week2-respond` requests with
`user=unauthenticated, outcome=blocked` — blocked only by the BAA gate, which is the wrong
control doing the right thing by accident. With `BAA_CONFIRMED=true` (as in production), an
unauthenticated caller would have received a full clinical answer.

The audit trail catching this is the argument for building one.
