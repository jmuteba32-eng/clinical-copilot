# alerts.md — Clinical Co-Pilot Production Alerts

Traces to ARCHITECTURE.md section 4 (verification) and USERS.md section 2
(the physician's 90 seconds between rooms). Exactly three alerts, on
purpose: every alert added beyond ones a responder must act on immediately
trains them to skim pages instead of reading them, and this system has
exactly three failure modes that meet that bar — one patient-safety, two
availability.

## Classification

| # | Alert | Class |
|---|-------|-------|
| 1 | Verification failure rate > 5% / 15 min (`/ask`) | **Patient safety** |
| 2 | p95 `/ask` latency > 10s / 10 min | Availability / performance |
| 3 | `/ready` returning 503 for > 2 min | Availability |
| 4 | `/respond` verified rate < 95% / 15 min | **Patient safety** |
| 5 | Eval category regression > 5% | **Patient safety** (CI, not prod) |
| 6 | Extraction failure rate > 5% / 15 min | **Patient safety** — *dormant, see below* |

**Alert 1 takes precedence over 2 and 3.** If it fires at the same time as
either of the others, triage it first, in a separate incident, even if the
others are the more visible/noisy page. A slow or fully-down agent tells a
physician nothing wrong; an agent that's up, fast, and quietly fabricating
a citation tells her something wrong with total confidence. The failure
mode in alert 1 is strictly worse than "no answer" — see verification.py's
module docstring: a hallucinated citation "looks exactly as trustworthy as
a real citation until a physician clicks through and finds nothing there."
Alerts 2 and 3 degrade the product to "unavailable," which a physician
will notice and route around by opening the chart directly. Alert 1
degrades it to "wrong, and undetected," which she won't.

---

## 1. Verification failure rate > 5% over 15 minutes

**Class: patient safety. Page immediately, do not wait for business hours.**

**Signal.** The `verification_passed` boolean score Langfuse records on
every `/ask` trace (`main.py`'s `/ask` handler, `trace.score_trace(name=
"verification_passed", ...)`), or equivalently the structured
`verification_completed` log line's `passed` field. Rate = failed
traces / total traces with a `verification_passed` score, over a trailing
15-minute window.

**What it detects.** `verification.py` runs after the agent produces an
answer and before it reaches a physician, checking the answer text against
the records the tools actually retrieved for three things: hallucinated
sources (a `[ResourceType/id]` citation pointing at a record that was never
retrieved), uncited clinical claims, and prescriptive/diagnostic language
(rule 1/6 violations). A failure here means at least one of those checks
caught something on a REAL request — not a synthetic probe, an actual
question a real physician asked about a real patient. This is the last
line of defense verification.py's own docstring describes: if this alert
fires, that defense caught something the system prompt alone did not
prevent.

**Why this threshold.** 0% is not achievable as a hard SLO — the eval
suite in `agent/evals/` shows the live model occasionally drops a citation
on a legitimately-cited fact (e.g. a trailing summary sentence restating a
fact from an already-cited bullet) even in normal, non-adversarial use.
5% is calibrated as "clearly outside that baseline noise, not a single
unlucky sample" — a rate this suite's own repeated runs never sustained.
15 minutes is short enough to catch a real regression (a prompt change, a
model version change, a verification.py bug) within the same on-call shift
it was introduced, but long enough that a run of 2-3 unlucky samples out of
a slow-traffic window doesn't page on noise alone — this is a RATE alert,
not a single-failure alert, specifically to avoid paging on the kind of
intermittent, already-documented model variance the eval suite expects.

**Remediation.**
1. Pull the last 15 minutes of failed traces from Langfuse (filter by the
   `verification_passed=0` score) and read the `verification_completed`
   log lines for the same `correlation_id`s — `hallucinated_source_count`,
   `uncited_claim_count`, and `boundary_violation_count` tell you which of
   the three checks is firing, which narrows the cause fast.
2. If `hallucinated_source_count` is driving it: treat as the highest
   severity. Check whether a recent deploy changed `agent.py`'s system
   prompt, the Anthropic model/version, or `TOOL_SPECS`. Roll back the
   most recent change to any of those first; do not attempt a forward fix
   under a live page.
3. If `boundary_violation_count` is driving it: same rollback-first
   approach — this means the agent is describing prescriptive/diagnostic
   language rule 1/6 is supposed to forbid outright.
4. Regardless of cause: this alert does NOT mean take the service down
   (unlike alert 3). `/ask` already returns the verification block to the
   caller on a failure (see `main.py`) — a failed verification is visible,
   not silently passed through as a clean answer. The urgency is
   diagnosing and fixing the regression quickly, not stopping traffic.
5. Confirm the fix by re-running `python -m agent.evals.run_evals` — the
   `invariants` suite is exactly the automated form of this check.

---

## 2. p95 `/ask` latency > 10s over 10 minutes

**Class: availability / performance.**

**Signal.** The `http_request` structured log line filtered to
`path=="/ask"`, `latency_ms` field, p95 over a trailing 10-minute window
(also visible per-request in Langfuse as trace duration, and broken down
further into the `llm_call`/tool-call child spans `observability.py`
creates, for root-causing which layer is slow).

**What it detects.** USERS.md frames this product's entire reason to
exist as fitting inside a physician's ~90 seconds between patients (open
chart, read recap, ask a follow-up, walk in). A `p95 > 10s` means the
*typical* request — not a tail outlier — is eating a meaningful fraction of
that whole window on a single turn, before she's even read the answer or
asked a follow-up. This is very likely one of two causes this project has
already documented as real: OpenEMR itself (Railway free tier, no caching
layer, 10-40+ queries to assemble one patient's picture per AUDIT.md — see
also `openemr_client.py`'s configurable timeout and its comments on this),
or Anthropic API latency/degradation on the LLM call. It is deliberately
NOT the same signal as alert 3 (`/ready`) — `/ask` can be slow while
`/ready`'s lighter auth-only check still passes quickly.

**Why this threshold.** 10s is not "the moment it becomes noticeably
slow" — it's calibrated against the actual budget: 10s out of a ~90s
window still leaves the physician most of her window to read the answer
and ask a follow-up, so this alert is not tuned to page on every individual
slow OpenEMR round trip (this project already expects those — see
`OPENEMR_TIMEOUT_SECONDS`). It pages when the *typical* request (p95, not
p99/max) is consistently eating enough of the budget that the product's
core value proposition — fitting inside 90 seconds — is at real risk, not
just occasionally rough at the tail. 10 minutes, rather than a shorter
window, avoids paging on a single burst of concurrent requests colliding
against OpenEMR's lack of caching.

**Remediation.**
1. Check `/ready` first (see alert 3) — if it's also failing or slow, this
   is very likely the same OpenEMR-latency root cause, not a separate LLM
   issue; treat alert 3 as the more actionable one and don't duplicate work.
2. If `/ready` is healthy and fast, pull recent `llm_call` spans from
   Langfuse and compare their latency to the tool-call spans for the same
   traces — this tells you whether OpenEMR or Anthropic is the slow leg
   without guessing.
3. If it's Anthropic: check Anthropic's status page before assuming a
   local cause. Nothing in this codebase can fix upstream model latency;
   the honest move is to confirm it's upstream and wait it out or fail
   over to a different model tier if one is configured, not to restart
   this service (a restart does not touch either external dependency).
4. If it's OpenEMR and it is NOT already also tripping alert 3: this is
   likely a slow-but-not-down period (matches this project's own
   documented Railway free-tier behavior) rather than an outage. No action
   beyond monitoring is usually needed unless it worsens into alert 3.
5. This alert does not by itself justify pulling the service out of
   rotation — physicians still get an answer, just a slower one. Alert 3
   is the "stop routing traffic" trigger, not this one.

---

## 3. `/ready` returning 503 for more than 2 minutes

**Class: availability.**

**Signal.** `/ready`'s HTTP status code, sampled by whatever polls
readiness (load balancer / orchestrator health check), sustained 503 for a
continuous 2-minute window. Corroborate with the `readiness_check_failed`
structured log line's `failures` field, which names the exact cause (a
missing `ANTHROPIC_API_KEY` or an OpenEMR authentication failure — see
`main.py`'s `/ready` handler).

**What it detects.** `/ready` checks only the two things `answer()`
cannot function without at all: a configured Anthropic key, and a working
OpenEMR OAuth2 authentication (deliberately not a full FHIR data call —
see the handler's own docstring). A sustained 503 means this instance
cannot serve `/ask` for anyone, for any patient, full stop — this is a
harder failure than alert 2's "slow," because there is no answer at any
latency, sourced or not.

**Why this threshold.** `/ready` already uses the LONGER
`readiness_timeout_seconds` (not the tighter per-request
`OPENEMR_TIMEOUT_SECONDS` a live physician's tool call is bounded by —
see `openemr_client.py`), specifically so a readiness probe tolerates
OpenEMR's documented free-tier slow-cold-start behavior without flapping.
A failure that survives that already-generous timeout, repeated
continuously for 2 minutes (not one flaky probe), is not a transient
hiccup — it is a real, sustained inability to authenticate, and 2 minutes
is short because there is no partial-credit state here: every minute spent
undetected is a full outage window, not a degraded one.

**Remediation.**
1. Stop routing traffic to this instance (pull it from the load balancer /
   readiness-gated pool) immediately — this is exactly what `/ready`
   returning 503 is designed to trigger in a standard orchestrator, so in
   most deployments this should already be automatic; confirm it happened.
2. **Do NOT restart the process.** Read `readiness_check_failed`'s
   `failures` field first: if it says `ANTHROPIC_API_KEY is not set`, that
   is a config/secret problem a restart with the same broken config will
   not fix. If it says `OpenEMR authentication failed: ...`, that is an
   upstream dependency problem (OpenEMR down, or Railway's free-tier
   instance unreachable/rate-limited) — this service has no state a
   restart would clear, and restarting only adds a fresh cold-start delay
   on top of an outage this service did not cause and cannot fix by itself.
3. Check OpenEMR directly (its own health endpoint / Railway dashboard) to
   confirm whether the outage is upstream, before spending time inside
   this codebase. If OpenEMR is down, this alert's remediation IS "wait
   for OpenEMR," escalated to whoever owns that instance — not a fix
   inside the agent.
4. If `failures` names a config issue (missing/rotated API key, OAuth
   client `is_enabled` flag disabled — this project has hit that exact
   OpenEMR admin-side cause before), fix the config/secret, THEN restart
   — the restart is a consequence of the fix, never the fix itself.
5. Do not clear this alert on "503 stopped" alone without reading which
   failure caused it — a flapping 503/200 pattern from marginal OpenEMR
   latency is a different, milder problem (closer to alert 2) than a flat
   503 from a dead credential, and the two need different follow-up even
   though both trip this same threshold.

---

## 4. `/respond` verified rate < 95% over 15 minutes

**Class: patient safety. Page immediately, do not wait for business hours.**

**Signal.** The `verified` boolean score Langfuse records on every `/respond`
trace (`main.py`'s respond handler, `trace.score_trace(name="verified", ...)`).
Rate = traces scored 1 / all traces carrying that score, over a trailing
15-minute window. Off-scope refusals are excluded: they never open a root trace,
because the supervisor short-circuits before the graph runs.

**What it detects.** Alert 1's counterpart for the Week 2 document path. On
`/respond`, `verified` is DERIVED, never set: `GroundedAnswer.model_post_init`
computes it as `scope == "in_scope" AND len(citations) > 0 AND no fabricated
citation markers`. So a sustained drop means one of three things, all bad:
retrieval is returning nothing (evidence_retriever handing back an empty list),
the answer model is citing `[n]` markers pointing at chunks that were never
retrieved, or the graph is routing in-scope questions somewhere they should not
go.

**Why this threshold.** Unlike alert 1, this is not model-variance noise — the
invariant is structural, computed from counts, not judged by a heuristic. On a
healthy system this should sit at 100%. 95% is set below the noise floor of a
single unlucky sample in a low-traffic window, not as an acceptable failure rate.
**If this alert fires at all, something is broken, not unlucky.**

**Remediation.**
1. Pull the failing traces from Langfuse (`verified=0`) and read the
   `worker:*` child spans for the same `correlation_id`. The routing trace tells
   you which worker to look at without guessing.
2. If `worker:evidence_retriever` reports `chunks_retrieved: 0`: retrieval is
   the cause. Check `/ready`'s `vector_index` key — a missing or unreadable
   guideline corpus reports `not_ready`. Check for `rerank_failed` log lines: a
   Cohere outage is non-fatal by design and should NOT cause this, so if it
   correlates, `_rerank()`'s fallback is broken.
3. If chunks are being retrieved but `verified` is still 0: suspect fabricated
   citation markers. `hallucinated_markers()` in `agent/pipeline/answer.py`
   blocks Verified when the answer cites `[4]` and only three chunks exist.
   Check whether a recent deploy changed the answer prompt in `graph.py`.
4. This alert does NOT mean take the service down. An unverified answer is
   returned to the caller labelled Unverified with zero sources — the physician
   sees the failure. The urgency is diagnosing it, not stopping traffic.
5. Confirm the fix with `python -m pytest agent/gate` — the
   `factually_consistent` and `citation_present` categories are the automated
   form of this check.

---

## 5. Eval category regression > 5%

**Class: patient safety. CI alert, not a production page.**

**Signal.** The pass rate per boolean rubric category from `python -m pytest
agent/gate`, compared against the last green run on `main`. Five categories:
`schema_valid`, `citation_present`, `factually_consistent`, `safe_refusal`,
`no_phi_in_logs`.

**What it detects.** A change that breaks a guarantee the system already made.
The pre-push hook (`agent/gate/pre-push.sh`) already blocks any push where a
gate test fails, so in practice this alert exists to catch the case where the
hook was not installed — hooks live in `.git/hooks/` and do not travel with a
clone.

**Why this threshold.** The rubric specifies "more than 5% drop in any
category." In practice this gate is stricter: it fails on **any** failing test,
not a 5% regression, because at 54 cases a single failure is already a 1.9%
drop and every case guards a specific documented failure mode. A 5% budget on 54
cases would permit two silent regressions. **The 5% rule is the floor this alert
must meet; the gate does not use it.**

**Remediation.**
1. Read which category failed. The test names say what broke — they are named
   for the failure mode they guard, not for the function they call.
2. Do NOT widen a schema or loosen an assertion to make it pass. That is
   loosening a specification to release a failing batch. If the test is wrong,
   fix the test deliberately and say why in the commit.
3. If the hook was not installed, install it:
   `cp agent/gate/pre-push.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push`
4. Re-run `python -m pytest agent/gate -q`. 54 must pass before the change
   moves.

---

## 6. Extraction failure rate > 5% over 15 minutes — DORMANT

**Class: patient safety. Defined, not yet wired.**

**Signal.** Would be: the rate of `ValidationError` / `JSONDecodeError` raised by
`extract_intake_form()` and `extract_lab_report()` over a trailing 15-minute
window.

**Why it is dormant, stated plainly.** Extraction does not currently run in the
request path. It is CLI-driven (`python -m agent.ingestion.extract`), and
`/respond` reads a cached JSON fixture produced by an earlier run. **An alert on
a code path that never executes in production is theater**, and this document's
own preamble argues against alerts a responder cannot act on. This one is
documented so the requirement is visibly tracked, not silently dropped.

**What activates it.** The upload path (`W2_ARCHITECTURE.md` §7). Once a
document can be uploaded and extracted in-request, this alert wires to the
exception rate on that endpoint and moves to active.

**The harder problem this alert would NOT catch, and why that matters more.**
Extraction has been measured against degraded scans (`agent/ingestion/degrade.py`).
At photocopier quality it is exact. At 90 DPI with 2.5° skew it **fails
silently**: it dropped a `Borderline` platelet flag the lab had printed, invented
an RDW value not on the document, and turned a `2X` template placeholder into
`24` — fabricating a year. Every one of those passed the schema with **zero
validation errors**, and the errors reproduced identically across runs, so
resampling would report high confidence on a wrong chart.

So an exception-rate alert would have caught none of it. **The schema proves
shape, not truth.** The mitigation is the bounding-box overlay (§7): if the model
must return coordinates for every value, it either points at the pixels where
`13.6` lives, or it cannot — and being unable to produce a box *is* the
confidence signal. That is a build item, not an alert.

---

## Not added: RAG retrieval latency

The rubric names a RAG retrieval latency alert. **It is deliberately not here.**

`W2_COST_LATENCY.md` measures the deployed service: p50 10.16s, p95 11.58s, and
the Anthropic call is ~9.9s of it. BM25 over ~500 in-memory paragraph chunks is
~0.01–0.05s, and the index builds once per process via `lru_cache`. **Retrieval
is under 1% of request latency.**

An alert on it would page a responder about noise that cannot move the number a
physician experiences — precisely the failure this document's preamble names:
"every alert added beyond ones a responder must act on immediately trains them to
skim pages instead of reading them." Alert 2 already covers latency at the level
that matters, and `/ready`'s `vector_index` key covers the failure a retrieval
alert would actually catch (a missing corpus).

If retrieval ever becomes a meaningful share of latency — a much larger corpus, a
live reranker in the hot path — this decision should be revisited with data.
