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

**Reranking: Cohere Rerank (`rerank-v3.5`), enabled in production via `RERANK=on`.**

The retriever casts a wide net and the reranker picks precisely: hybrid/BM25 scores the whole
corpus, the top **10** candidates go to Cohere, and it returns the final **3**. That split is
the point — reranking the same 3 chunks the retriever already chose would change almost
nothing, so `_CANDIDATE_K = 10` is what makes the reranker worth its cost.

**Non-fatal by design.** If the key is missing, the model name is wrong, or Cohere is
unreachable, `_rerank()` logs (`rerank_skipped` / `rerank_failed`) and returns the unreranked
hits rather than raising. A reranker is a quality improvement on top of retrieval that already
works; it must never become a single point of failure that takes a clinical answer down. This
was verified accidentally during development: a missing key produced `rerank_skipped` and the
pipeline still returned three correct, cited dermatochalasis chunks with `verified=True`.

When reranking is on, Cohere's relevance scale replaces the retriever's scores, so
`_MIN_RELATIVE_SCORE` no longer applies — the reranker *is* the relevance judgement.

**Measured effect** on *"do you recommend surgery for John Doe?"* (query-expanded):

| Rank | BM25 only (scores ~17) | Reranked (Cohere 0–1) |
|---|---|---|
| 1 | p6 — functional surgery indications | 0.409 — p6, functional surgery indications |
| 2 | p5 — 20–64% visual field loss stat | **0.299 — p5, "a common functional indication ... is dermatochalasis"** |
| 3 | p5 — "a common functional indication..." | 0.269 — p5, review objectives |

The reranker promoted the chunk that most directly answers the question and dropped the
statistic. Both orderings cite the correct guideline; the reranker is a refinement, not a
rescue.

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

### Rerank environment (`RERANK`, `COHERE_API_KEY`, `COHERE_RERANK_MODEL`)

| Variable | Default | Production | Purpose |
|---|---|---|---|
| `RERANK` | `off` | `on` | Enables Cohere Rerank after the wide candidate pool is scored |
| `COHERE_API_KEY` | — | set | Cohere API key. **If absent, rerank is skipped** (`rerank_skipped`); retrieval still works |
| `COHERE_RERANK_MODEL` | `rerank-v3.5` | `rerank-v3.5` | Rerank model id |

`agent/rag/retrieve.py` loads `agent/.env` relative to the module path (same convention as
`main.py` and `respond.py`), so the CLI and non-FastAPI callers see these variables without
depending on CWD.

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
corpus sizes or for conditions whose guideline vocabulary overlaps more heavily. Cohere rerank
is enabled in production (§3); ranking quality at scale remains unvalidated.

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

## 7. PRD requirements: status

Week 1 left several PRD items unbuilt; Week 2 closed most of them. Stated plainly,
done and not-done together, rather than buried.

**Implemented in Week 2:**

1. **OpenEMR write-back (Step 2).** Extracted intake facts and the source document are
   written back to the patient's OpenEMR record. FHIR write scopes are rejected by
   OpenEMR 7.0.3 (`user/Observation.write` -> `invalid_scope`), so persistence uses the
   authenticated REST document API, which the PRD explicitly allows ("appropriate FHIR
   resources **or** OpenEMR records"). Design and constraint: section 14.
2. **Visual PDF bounding-box overlay (Step 3).** The citation contract's click-to-source
   overlay is built. Each guideline chunk's coordinates are captured at index time, flow
   through the citation to the API, and the UI renders the cited passage boxed on the
   source page. Design: section 15.
3. **Engineering requirements.** OpenAPI 3.1 spec (`agent/openapi.json`, contract-tested in
   the gate), Bruno collection (`api-collection/`), `/ready` extended for Week 2
   dependencies (vector index, reranker, document storage), and backup/RPO/RTO (section 12)
   are addressed. Per-worker tracing spans and dashboards/alerts remain Phase-2.
4. **Audit-log pseudonymization (Step 5).** Patient IDs in the access audit trail are
   pseudonymized to a deterministic one-way token (see the Step 5 note at the end of this
   doc). The audit log's volume mount is still open (section 5.2): records still do not
   survive a redeploy.

**Still not implemented:**

5. **Patient scoping on the Week 2 document path.** The write-back (item 1) stores
   documents per patient, but the *read* path that answers questions is still pinned to one
   patient: `respond()` takes no `patient_id`, `_INTAKE_LIVE` is a single fixed file, and
   `has_documents_for()` is hardcoded to the John Doe fixture UUID. Full per-patient
   document isolation on the answer path is Phase-2. The UI patient selector is therefore
   still effectively cosmetic for the document path.
6. **Lab-to-chart identity reconciliation.** `extract_lab_report` transcribes whichever
   patient the report names. Nothing checks that a report's patient matches the session's
   patient. In a real deployment that check is mandatory before a lab value is shown next
   to a chart. (Unchanged from Week 1.)

---

## 8. Testing strategy

| Layer | What | Where |
|---|---|---|
| Unit / contract | Schema validators — required fields, rejection of malformed extractions, citation shape | `agent/gate/test_schema_valid.py`, `test_intake_extraction_schema.py` |
| Unit / behavior | Scope routing, verified-requires-citation invariant | `agent/gate/test_grounded_answer.py` |
| Retrieval | Citation completeness; BM25 ranks the right guideline over a stub corpus | `agent/gate/test_citation_present.py` |
| PHI / audit | Audit trail pseudonymizes patient_id; raw UUID never reaches the file; token is deterministic | `agent/gate/test_audit_scrubs_patient_id.py` |
| Provenance | Guideline chunks resolve to an in-bounds bounding box; absent text yields no box | `agent/gate/test_bbox_capture.py` |
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

---

## 12. Backup and recovery

**Principle: everything that matters is either in git or derived from something in git.**
There is no primary datastore whose loss would be unrecoverable — with one exception,
named honestly in §12.3.

### 12.1 What is backed up, and how

| Artifact | Where it lives | Backup mechanism | RPO | RTO |
|---|---|---|---|---|
| **Source documents** (intake form, lab report, degraded fixtures) | `agent/ingestion/test_docs/` | **git** — committed, pushed to GitLab | **0** (committed) | ~2 min (clone) |
| **Guideline corpus** | `agent/rag/guidelines/` | **git** | **0** | ~2 min |
| **Extracted facts** (`john_doe_intake.json`) | `agent/pipeline/` | **git** — and reproducible: re-run `python -m agent.ingestion.extract` | **0** | ~2 min, or ~15s to re-extract |
| **Eval golden set** (54 cases) | `agent/gate/*.py` | **git** — it *is* code | **0** | ~2 min |
| **Vector index / BM25 index** | in-process memory | **none needed** — derived data, rebuilt from the committed corpus at first request | n/a | ~5s (cold build) |
| **OpenEMR patient data** | Railway MySQL volume | **none** — see §12.3 | ∞ | unbounded |
| **Audit log** | container filesystem | **none** — see §12.3 | ∞ | unrecoverable |
| **Derived FHIR records** | — | **not implemented** (§7) — nothing is written back to OpenEMR, so there is nothing to back up |

### 12.2 The golden set is reproducible from the repo alone

The rubric calls this out specifically, so: the 54-case golden set is **Python source
files**, not database rows. `agent/gate/test_golden_set.py` holds its cases as literal
lists in code. Clone the repo, run `python -m pytest agent/gate`, and the entire eval
suite reproduces with no external dependency — no database, no network, no API keys,
no fixtures to restore.

That is deliberate. An eval set living only in a database with no recovery path is one
outage away from being unable to prove your system still works.

### 12.3 Known gaps — no recovery path

**The audit log does not survive a redeploy.** Every deploy logs:

```
[WARN] audit_log_path_fallback preferred_path="/data/audit_log.jsonl"
       active_path="/home/appuser/.clinical-copilot/clinical-copilot-audit.jsonl"
```

A Railway volume **is** mounted at `/data`, but the container's non-root `appuser`
cannot write to it, so the audit trail falls back to the container filesystem — which
is destroyed on every deploy. **RPO is effectively infinite: every redeploy loses the
entire audit history.**

For a system whose stated purpose includes making access to PHI traceable, an audit
trail that vanishes on deploy is not an audit trail. This is the single most important
gap in this section. The fix is a `chown` on the volume mount in the Dockerfile, or
running the audit writer as a user that owns `/data`.

**OpenEMR's MySQL data has no backup.** It sits on a Railway volume with no snapshot
schedule and no export. If that volume is lost, the synthetic patient records are gone.
Acceptable only because the data is synthetic and re-seedable; unacceptable for anything
real.

### 12.4 Manual recovery procedure

**If the deployed agent is lost entirely:**

```bash
git clone https://labs.gauntletai.com/jonathanmuteba/clinical-copilot
cd clinical-copilot/agent
# set ANTHROPIC_API_KEY, BAA_CONFIRMED, RAG_MODE, RERANK, COHERE_API_KEY
railway up --service agent
```
Everything the agent needs — documents, corpus, extracted facts, eval set — arrives with
the clone. **RTO ≈ 10 minutes**, dominated by the Docker build.

**If extracted facts are corrupted but source documents survive:**

```bash
python -c "from agent.ingestion.extract import extract_intake_form; from pathlib import Path; \
r = extract_intake_form(r'agent/ingestion/test_docs/John_Doe-Intake-Form.pdf'); \
Path('agent/pipeline/john_doe_intake.json').write_text(r.model_dump_json(indent=2), encoding='utf-8')"
```
**RTO ≈ 15 seconds.** One vision call. The source document is the system of record; the
extracted JSON is a cache, and a cache with a documented rebuild path is not a backup
problem.

**If the guideline corpus is lost:** the three PDFs are in git. If git is lost, they are
public clinical practice guidelines and are re-obtainable — but the *chunking* is
deterministic, so retrieval behavior reproduces exactly once the same PDFs are restored.

**If OpenEMR is lost:** re-seed synthetic patients. Note that `agent/evals/patients.py`
documents the exact expected facts per patient (verified against the live FHIR API), so
a re-seed can be validated rather than assumed. The Week 2 document path does not depend
on OpenEMR at all — `/ready` reports `degraded`, not `not_ready`, and the ingested
patient keeps answering.

### 12.5 Why RPO is 0 for everything that matters

Because git is the backup. Committing is the backup event, it happens many times a day,
and the pre-push hook means nothing reaches the remote without the eval gate passing
first. **The recovery point is the last commit, and the last commit is always green.**

## 13. Incident: OpenEMR outage (2026-07-16)

**Status at submission: OpenEMR is down. The Week 2 document path is unaffected and
serving.** `/ready` reports `degraded`, not `not_ready`, and names the dependency.

This section is written while the incident is open, not after a clean recovery.

### 13.1 What is affected

| | |
|---|---|
| **Working** | `POST /respond` document path — supervisor → intake_extractor → evidence_retriever → answer, hybrid retrieval, Cohere rerank, cited answers, routing trace, safe refusals, auth, audit trail |
| **Down** | OpenEMR itself; the `/respond` chart-fallback path; Week 1's `/ask` and `/chat`; the UI patient picker |

The document path never touches OpenEMR, which is why it kept answering throughout.

### 13.2 What happened

The OpenEMR service was deployed from the stock `openemr/openemr:7.0.3` image with
**no volume mounted**. It had been running untouched since initial setup.

OpenEMR's container entrypoint (`docker/release/openemr.sh`) decides whether to run
first-boot setup by reading a single marker:

```bash
is_configured() {
    php -r "if (is_file('${SQLCONF_FILE}')) { require '${SQLCONF_FILE}';
            echo isset(\$config) && \$config ? 1 : 0; } else { echo 0; }"
}
```

`sites/default/sqlconf.php` is written **into the container filesystem** during setup.
With no volume, that file — and every other artifact setup produces — existed only in
the running container.

Replacing the container to add a menu entry destroyed all of it. `auto_configure.php`
then ran against an already-populated database and died:

```
ERROR IN OPENEMR INSTALL: Unable to execute SQL: CREATE TABLE ccda_field_mapping ...
due to: Table 'ccda_field_mapping' already exists
```

**The trigger was a redeploy. The defect was that a redeploy could do this at all.**
This container was one restart away from this failure from the day it was created —
by anyone, for any reason, including a Railway-initiated restart.

### 13.3 The attempted fix, and why it made things worse

Reasoning from the entrypoint source: if `is_configured()` only reads `$config`, then
supplying `sqlconf.php` with the real credentials and `$config = 1` should make the
container skip setup and connect to the existing database.

That was done via a custom start command that wrote the file before exec'ing
`./openemr.sh`. The container started cleanly and logged success.

**Then every page returned 500 and every OAuth call returned 404.**

The reasoning was wrong in a way the source made visible and I did not read carefully
enough. `MANUAL_SETUP` / `$config` do not gate *only* the database step. They gate the
whole setup block (`openemr.sh` lines 745, 819, 869), which also generates:

- OpenEMR's **encryption keys** (`sites/default/documents/logs_and_misc/methods/`)
- the **OAuth2 signing certificates**
- site scaffolding and file permissions

All of those also lived only in the container filesystem. Telling the entrypoint
"already configured" skipped the work that creates them — and then the entrypoint
did this:

```
Setup scripts removed, we should be ready to go now!
GET /interface/login/login.php   500
POST /oauth2/default/token       404
```

**`cleanup_setup_scripts()` deletes `auto_configure.php` once `is_configured()` returns
1** — a sensible security measure, since a live setup script is an attack surface. But
because the marker was fabricated, the container removed the only tool that could have
repaired it. It now believes it is configured, is not, and cannot fix itself.

**The failed fix is more instructive than a clean recovery would have been.** The
container was never "a database connection away" from working. It was carrying
substantial generated state — keys, certificates, permissions — none of it durable, and
none of it visible until it was gone.

### 13.4 Root cause

**Not the redeploy. Not the menu change.**

> **Configuration and cryptographic state required to serve requests was stored in an
> ephemeral container filesystem, with no volume and no reconstruction path.**

The `openemr` service has no volume. `agent` and `mysql` both do. That asymmetry was
visible in `railway status` for the entire project and was not read as a risk.

**This is the same defect as §12.3.** The audit log writes to a container path because
`appuser` cannot write to the mounted volume, so it does not survive a redeploy. Same
class: **state that matters, living somewhere ephemeral.** One was found by writing a
backup section; the other by triggering it.

### 13.5 What was not lost

**The database.** MySQL is a separate Railway service with its own volume. It was never
touched. The failure mode proves it: `auto_configure.php` connected successfully and
failed *because the tables already exist*.

Patient records, conditions, medications are intact and unreachable — not gone.

### 13.6 Recovery plan

Not attempted before submission. It is ~90 minutes of deterministic work and was not
worth doing tired against a deadline, given the Week 2 path is unaffected.

1. **Point `MYSQL_DATABASE` at a fresh database name.** Auto-config runs clean and
   generates its own keys and certificates.
2. **Mount a volume at `/var/www/localhost/htdocs/openemr/sites`** — the actual fix.
   Without it, the next redeploy repeats this.
3. **Re-create the three synthetic patients** through the OpenEMR UI, matching
   `agent/evals/patients.py`, which documents each patient's expected conditions and
   medications verified against the live FHIR API. That file makes the re-seed
   verifiable rather than approximate.
4. **Update the UUID constants** — new patients get new FHIR ids:
   - `FIXTURE_PATIENT_ID` (`agent/pipeline/respond.py`)
   - `PATIENT_WITH_DATA` / `PATIENT_SPARSE` (`agent/evals/patients.py`)
   - `api-collection/environments/*.bru`
   - `agent/gate/test_patient_scoping.py`
   - this document
5. **Re-run** `python -m pytest agent/gate` and the live suite.

The old database is retained, not dropped. Recovering its rows into the new schema is
possible later; it is not on the critical path, because the data is synthetic.

### 13.7 What this incident validates

**The `degraded` state in `/ready` (§added same day) is the reason this is an incident
and not an outage.**

Before that change, `/ready` returned a binary verdict — 503 `not_ready` if OpenEMR
authentication failed. During this incident it would have reported the entire service
dead. It is not:

```json
{
  "status": "degraded",
  "checks": {
    "anthropic": "ok",
    "openemr": "unavailable",
    "document_storage": "ok",
    "vector_index": "ok",
    "reranker": "ok"
  }
}
```

John Doe's document path returns `verified: true` with three cited dermatochalasis
chunks, right now, with OpenEMR down. **A binary probe would have lied about a service
that is answering correctly.**

The hard/soft dependency split was written hours before this incident on the argument
that the document path does not need OpenEMR. That argument was tested the same day and
held.

### 13.8 What I would do differently

1. **Read `railway status` as a risk inventory.** "`openemr` has no volume" was on screen
   for the whole project. A service with no volume is a service that cannot survive a
   restart — that should have been a §12 finding before it was a §13 incident.
2. **Never redeploy a container that has never been redeployed** without first asking what
   state it accumulated since creation. Uptime is not evidence of durability; it can be
   evidence that nobody has tested durability.
3. **Read the whole gate, not the condition I was looking for.** `is_configured()` gates
   far more than the database step. I found the line that supported the fix I wanted and
   stopped reading. The rest of the block was three screens away and would have shown that
   `$config = 1` skips key generation and triggers deletion of the repair tool.
4. **Sequence risky infrastructure work behind everything else, and stop at the first
   surprise.** The six documented rubric gaps were closed first, which is why they survive
   this. The embedding attempt should have stopped at the first failed deploy rather than
   the fourth.

## 14. OpenEMR write-back (Week 2, Step 2)

**What it does.** After a document is ingested and extracted, the source PDF and a
companion facts file are written back to the patient's OpenEMR record, so a derived
fact is traceable to a stored artifact in the EMR, not just to a value in the agent.

**Why REST, not FHIR.** The natural home for a derived observation is a FHIR
`Observation` resource. OpenEMR 7.0.3 rejects FHIR write scopes at registration /
token time: requesting `user/Observation.write` yields `invalid_scope`. Rather than
fake FHIR persistence, the writer uses OpenEMR's authenticated REST document API
(`/apis/default/api/patient/{pid}/document`), writing to the patient's Medical Record
category. The PRD allows this explicitly: persistence to "appropriate FHIR resources
**or** OpenEMR records." This is a platform constraint, documented, not a shortcut.

**Design.** `agent/openemr_writer.py` is a *separate* client from the read-only
`OpenEMRClient`, on purpose: writes use a distinct OAuth client registered with
`user/document.write` scope, keeping the read client's narrow, audited, read-only
contract intact. The writer maps the session's FHIR UUID to the numeric `pid` the
document endpoint needs via the patient-list API.

**Failure contract: best-effort, non-fatal.** Write-back runs after extraction has
already succeeded and the answer path is already satisfiable from the extracted JSON.
A write-back failure (OpenEMR down, scope revoked, pid not found) is logged
(`openemr_write_failed`) and swallowed -- it never fails the ingest request. The source
of truth for answering is the extracted facts; the EMR write is a durability and
traceability bonus, so it must not become a single point of failure for ingestion.

**Verification.** FHIR write being unavailable, write success is confirmed by a direct
`documents`-table query rather than the REST document *list* endpoint, which returns a
SQL error on this 7.0.3 build (a known platform bug, not ours). The write path itself
returns 200 and the file appears under the patient's Medical Record in the UI.

---

## 15. Citation bounding-box overlay (Week 2, Step 3)

**What it does.** The citation contract requires click-to-source provenance. Beyond the
expandable quote, a physician can click "View in source" on a guideline citation and see
the cited passage **boxed on the rendered page** of the source PDF -- coordinate-level
provenance, not just a page number.

**Where coordinates come from.** The guideline PDFs (unlike VLM-extracted intake/lab
forms) have real text layers, so coordinates are captured deterministically at index
time, not guessed. When `_load_chunks()` builds a chunk from a page, `_locate_bbox()`
runs PyMuPDF `search_for()` on that page and stores the resulting rect on the chunk.

**The anchoring subtlety.** Chunk text is whitespace-normalized (`_split_page_text`
collapses runs of whitespace), so the full chunk string does not `search_for`-match the
PDF's original layout. The locator anchors on the chunk's first ~8 words -- unique enough
on a page, short enough to survive the original line breaks -- and takes the union of the
matched line rects so a wrapped passage is boxed whole. On the real corpus this resolves
a box for 100% of chunks (528/528). A chunk that straddles a page break and does not
match yields `bbox=None`; the UI then opens to the page and draws no box. Never a wrong
box.

**The data path.** `Chunk.bbox` -> `EvidenceHit.citation()` dict -> pipeline `Citation`
model -> `RespondCitation` (API) -> JSON. Each `**unpack` hop (graph, respond, main)
only carries the field because every model on the path now declares `page` and `bbox`;
Pydantic drops undeclared keys, so all three models were extended together.

**Serving and rendering.** A new auth-gated endpoint, `GET /guideline-page?file=&page=`,
renders one guideline page to PNG at a fixed 150 DPI. It serves **only** the non-PHI
published guideline corpus -- never patient documents, which would need patient-scoping
and audit treatment the guideline path does not. `file` is whitelisted against the real
corpus filenames (not used to build a path), so it cannot traverse out of the guidelines
directory. The front-end fetches the image with the session Bearer token (an `<img src>`
cannot send the header, so it fetches to a blob URL), then positions the highlight as
**percentages** of the page's point dimensions -- derived from the rendered image's
natural size and the known 150 DPI, so the box tracks however the image is scaled to fit
the panel and aligns on Letter or A4 alike.

**CI.** `agent/gate/test_bbox_capture.py` fails the build if chunk-to-box resolution
regresses below 90%, if any box is degenerate or out of bounds, or if absent text yields
a box instead of `None`.

---

## Audit trail: patient-ID pseudonymization (Week 2, Step 5)

The access audit trail (`agent/audit_trail.py`) records which patient each
event concerned -- that is the HIPAA purpose of an access log. But the raw
FHIR Patient UUID is itself an identifier, and an append-only JSONL file of
raw UUIDs is a PHI store in its own right. These two facts pull against each
other: drop the patient entirely and the log can no longer answer "who
accessed this patient's record?"; keep the raw UUID and the audit artifact
becomes something we would rather not persist to disk.

Resolution: the trail persists a deterministic one-way token
(`phash_<first 16 hex of SHA-256>`) in place of the raw UUID. Because the
hash is deterministic, the same patient always maps to the same token, so
"every access to patient X" stays answerable by grouping on the token -- the
audit log keeps its purpose. Because it is one-way, the real UUID never
lands on disk and the token cannot be reversed to it.

The scrub is applied at the single write boundary (`AuditTrail.append_sync`),
so every audited event -- and the `/audit` read API that surfaces them -- is
pseudonymized without touching the live request path: scoping, FHIR calls,
and the `/respond` pipeline all still use the real UUID internally. A gate
test (`agent/gate/test_audit_scrubs_patient_id.py`) fails the build if a raw
patient UUID ever reaches the audit file, and asserts the token is
deterministic, so the "groupable" property cannot silently regress.

Scope note: logs and traces are covered separately by the
`no_phi_in_logs` gate (worker-handoff logs carry only routing metadata and a
correlation id; free-text questions are recorded by length only). This note
covers the access audit artifact specifically.
