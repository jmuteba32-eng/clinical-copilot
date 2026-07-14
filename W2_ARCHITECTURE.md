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

**Current state: NOT IMPLEMENTED as a multi-agent graph.** See §7.

The Week 2 pipeline is composed of plain functions:

```
respond(question)                      agent/pipeline/respond.py
  ├─ classify_scope(question)          agent/pipeline/answer.py
  │    └─ out_of_scope -> SHORT-CIRCUIT: labeled refusal, no retrieval, no LLM call
  ├─ _load_intake()                    cached JSON fixture (see §7)
  ├─ retrieve_evidence(question)       agent/rag/retrieve.py
  ├─ Claude call                       writes answer grounded in intake + evidence
  └─ answer_question(...)              agent/pipeline/answer.py — applies the verified/citation invariant
```

This was a deliberate sequencing choice: build each capability as a testable plain function first, wrap in an orchestration framework last. The functions are written to be wrapped — `classify_scope`, `retrieve_evidence`, and the extraction step map 1:1 onto a supervisor and the two required workers (intake-extractor, evidence-retriever) with no rewrite. The wrapping is not done.

---

## 3. RAG design

**Files:** `agent/rag/retrieve.py`, corpus in `agent/rag/guidelines/`

**Corpus:** three ophthalmology guideline PDFs — dermatochalasis (the test patient's condition), dry eye, keratoconus.

**Chunking:** one chunk per page. Coarse, and the root cause of the limitation in §5.

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

### Rubric coverage — 21 cases, 4 of 5 categories

| Category | Status | File |
|---|---|---|
| `schema_valid` | ✅ 5 cases | `test_schema_valid.py` |
| `citation_present` | ✅ 2 cases | `test_citation_present.py` |
| `factually_consistent` | ✅ | `test_grounded_answer.py` |
| `safe_refusal` | ✅ | `test_grounded_answer.py` |
| `no_phi_in_logs` | ❌ **not implemented** | — |
| **Intake extraction schema** | ✅ 4 cases | `test_intake_extraction_schema.py` |

**21 cases, not the required 50.** See §7.

---

## 5. Known limitations

### 5.1 Retrieval returns irrelevant citations

For the query *"What does the guideline recommend for this patient's dermatochalasis?"*, BM25 returns:

```
[10.808] dermatochalasis_guideline.pdf p5   ← correct
[10.356] keratoconus_guideline.pdf p33      ← "APPENDIX 1. QUALITY OF OPHTHALMIC CARE" boilerplate
[10.341] dry_eye_guideline.pdf p31          ← the same boilerplate, different document
```

**Cause:** page-sized chunks. A full page of boilerplate offers dozens of common words ("patient", "care", "quality") to match, so long non-clinical pages score within 4% of the genuinely relevant page. A relative-score threshold (`_MIN_RELATIVE_SCORE = 0.4`) is in place but cannot separate hits this close together.

**Impact:** the answer model reliably identifies and discards the irrelevant chunks — deployed output says *"[2] and [3] are not relevant to this patient's condition"* — so answers stay correct. But the citations array still surfaces them, which means a physician-facing sources panel would list an unrelated disease's appendix. **This is a real defect, not cosmetic.**

**Documented negative result:** query-side stopword filtering was tried and **measured as worse** — the correct dermatochalasis page dropped from #1 to #2 and an unrelated dry-eye page took the top slot (scores collapsed 10.8 → 4.6). It was reverted. The code remains in `retrieve.py`, unwired, with a comment recording the result.

**Real fix:** paragraph-level chunking, or a reranker. Both Phase 2.

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
| *"2+2" and "Testing" display Verified with zero sources* | ✅ **Fixed structurally.** `GroundedAnswer.verified` is derived in `model_post_init` from `scope == "in_scope" and len(citations) > 0`. It cannot be set to True by a caller. A "Verified · 0 sources" answer is **not constructible.** |
| *Questions outside the patient chart should be rejected before the model runs* | ✅ **Fixed.** `classify_scope()` short-circuits off-scope questions to a labeled refusal with `refusal_reason` — no retrieval, no LLM call. Verified live in production. |
| *BAA flag should not be presented as proof of HIPAA compliance* | ✅ Language corrected throughout this document and the demo. |
| *Audit log should capture denial reason* | ❌ Not done. `refusal_reason` exists on the answer object but is not yet written to the audit trail. |
| *Eval report predates the final code — no current run proves it passes* | ✅ **Fixed structurally.** The gate runs on every push; stale evidence cannot accumulate silently. |
| *Tests don't cover the HTTP authorization boundary on /ask and /chat* | ❌ Not done. |

**The theme of that feedback — "make sure your evidence matches your claims" — is the organizing principle of this document.** Where a requirement is unmet, it is listed as unmet.

---

## 7. Not implemented (required by the PRD)

Stated plainly rather than buried:

1. **Supervisor + 2 workers.** Required core deliverable. The pipeline is plain functions; there is no LangGraph/Agents-SDK graph and no logged handoffs. The functions map 1:1 onto the required roles and are structured to be wrapped.
2. **FHIR persistence.** Extracted intake facts are **not** written back to OpenEMR as FHIR resources, and the source document is not stored in OpenEMR. `respond()` reads a cached JSON fixture (`agent/pipeline/john_doe_intake.json`) produced by one real extraction run. The loader is a deliberate seam: swapping it for an OpenEMR read changes nothing downstream.
3. **Lab PDF (second document type).** Only the intake form is wired. A lab report fixture exists but has no schema or extraction path.
4. **Visual PDF bounding-box overlay.** Required by the citation contract. Not built. `source_page` exists as the anchor it would hang from.
5. **50-case golden set.** 21 cases exist.
6. **`no_phi_in_logs` rubric.** The 5th category is unimplemented.
7. **Cohere rerank.** See §3.
8. **Click-to-source UI.** `/respond` returns citation JSON; nothing renders it.
9. **Engineering requirements** — OpenAPI 3.0 spec, `/ready` extension for Week 2 dependencies, per-worker tracing spans, dashboards/alerts, Postman collection, backup/RPO/RTO, baseline CPU/memory profiles: not addressed.

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
- **The HTTP authorization boundary** — a known gap carried over from Week 1 (§6).

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
