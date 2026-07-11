# USERS.md — Clinical Co-Pilot

Who this product is for, how they work, what they need from the agent, and what the shipped system actually does today.

---

## 1. The target user

### Who

**Dr. Maria Alvarez** is a primary care physician running a full clinic day — roughly twenty patients back-to-back in ambulatory practice. She is experienced, time-pressured, and legally responsible for every clinical decision she makes. She is not looking for a second opinion from software; she needs **her patient's chart assembled quickly, accurately, and in a form she can trust** before she opens the exam-room door.

### The constraint: ~90 seconds between patients

The workflow is brutally short:

| Time | What happens |
|------|----------------|
| 8:49 | Finishes with the prior patient, walks the hall |
| 8:50 | Opens Clinical Co-Pilot, selects the next patient |
| 8:51 | Reads the sourced recap, asks one or two follow-up questions in plain language |
| 8:52 | Enters the room already oriented — who this person is, what is on their problem list, what changed, what is missing from the record |

That window is **about ninety seconds**. Anything that takes minutes, requires clicking through six modules, or returns an answer she cannot verify is effectively useless — she will skip it and wing it, which is worse for everyone.

### What she needs

In those ninety seconds Dr. Alvarez needs to reload **one patient's** context:

- **Demographics** — enough to confirm she has the right chart (age, sex; she selects by name in the picker).
- **Active clinical picture** — diagnoses, medications, recent labs or imaging when they exist.
- **Gaps stated honestly** — if there are no medications on file, she needs to hear that, not a plausible guess.
- **Traceability** — every clinical claim tied to a source in the chart, so she can spot hallucinations before they influence care.
- **Follow-up in her own words** — "When did that start?", "What was their last A1c?", "Are they on anything for blood pressure?" — questions she cannot predict until she glances at the recap.

Her question differs per patient and per day. She often does not know exactly what she will ask until she is standing in the hallway reading the screen.

### What she cannot afford

| Failure mode | Why it is unacceptable |
|--------------|------------------------|
| **Slow** | OpenEMR on a cold or remote host can take tens of seconds to assemble one chart. If the tool misses her ninety-second window, she will not use it. |
| **Unsourced** | A confident sentence with no link to a real Condition, MedicationRequest, or Observation is indistinguishable from a hallucination. She cannot walk in trusting it. |
| **Fabricated** | Inventing a diagnosis, medication, lab value, or "normal" finding when the record is empty or a tool failed is a patient-safety incident, not a UX nit. |
| **Cross-patient leakage** | Mentioning another patient's name, UUID, or clinical detail — even in a refusal — is a privacy violation. |
| **Prescriptive AI** | The agent must never diagnose, prescribe, or tell her what to do. She is the clinician; the software is decision **support**, not decision **maker**. |

Clinical Co-Pilot is designed around these constraints first. Speed, sourcing, and honesty about missing data are not polish items; they are the product.

---

## 2. Core use cases (in detail)

Three use cases define why this product is an **agent** rather than a fixed dashboard.

### UC-1: Opening recap ("Previously on…")

**What the physician asks**

Typically nothing manual on the first turn — selecting a patient triggers an automatic opening request equivalent to: *"Summarize this patient's chart for me."* She may also type her own opener if she prefers.

**What the agent does**

1. Confirms the patient exists in OpenEMR (before any chart data is read).
2. Loads the chart through four read-only tools in parallel: `get_intake` (demographics), `get_diagnosis` (problem list), `get_treatment` (medications), `get_testing` (labs and imaging).
3. Synthesizes a **short, readable recap**: e.g. "22-year-old male" with diagnoses listed by name (Keratoconus, Dermatochalasis, Dry eye syndrome — not raw ICD codes when a display label is available), medications and results when present, and explicit statements when categories are empty.
4. Attaches a **[ResourceType/id] citation** after every clinical claim about this patient.
5. Runs **verification** on the answer before display — checking that cited sources were actually retrieved and that the language stays within decision-support boundaries.

**Why an agent, not a dashboard**

A dashboard shows the same widgets in the same layout for every patient. Dr. Alvarez needs a **narrative** that prioritizes what matters for *this* visit, states **blind spots** ("no lab results on file"), and does so in prose she can read in five seconds. Dashboards do not say "here is what I could not find"; agents can. The recap is assembled fresh from scattered FHIR resources each session, not from a static panel configuration.

**Shipped today:** Yes — automatic recap on patient select in the chat UI.

---

### UC-2: Multi-turn follow-up Q&A

**What the physician asks**

Unpredictable, conversational questions that depend on prior context, for example:

- "When did that start?"
- "What medications are they on?"
- "Any imaging since the last visit?"
- "You mentioned dry eye — is it bilateral?"

The second question only makes sense after the first answer. She should not have to re-paste the recap or re-specify the patient.

**What the agent does**

1. Accepts the **conversation history** (prior user and assistant turns) plus a new message, always scoped to the **same patient_id** for the session.
2. Uses history so pronouns and references ("that", "they", "the dry eye") resolve against what was already said.
3. Calls tools again when the follow-up needs chart data not yet retrieved; reuses cached chart assembly within the session TTL when appropriate so repeat questions stay within the latency budget.
4. Verifies every new answer against the **session's known record set** — citations from the opening recap remain valid on follow-up turns because verification checks against the assembled chart for this patient, not only tools invoked on the latest turn alone.

**Why an agent, not a dashboard**

You cannot pre-build a filter, chart, or form for a question the physician has not thought of yet. Multi-turn dialogue is the natural interface for "one thing led to another" clinical reasoning **without** giving the model authority to decide. The agent retrieves; she interprets.

**Shipped today:** Yes — `POST /chat` with `conversation_history`; chat UI maintains history in memory across turns.

---

### UC-3: Visit-to-visit comparison (future)

**What the physician will ask**

Windowed, comparative questions she chooses in the moment:

- "How has their pain score changed since March?"
- "What medications were added or stopped between these two visits?"
- "Show me imaging from the last two encounters."

She picks the visits and the measures; the software surfaces a **sourced delta**, not a pre-canned trend graph.

**What the agent will do (planned)**

Use encounter-scoped tools (e.g. `get_visit`) to pull structured data for the visits she names, compare only what was retrieved, and cite each side of the comparison. Gaps ("no encounter note for that date") will be stated, not filled.

**Why an agent, not a dashboard**

Comparison dashboards assume you know **which** visits and **which** metrics matter before you build the dashboard. In primary care the relevant window often emerges mid-conversation: "since they were hospitalized" or "since the referral." An agent can narrow the window at query time; a static dashboard cannot.

**Shipped today:** Not yet — architecture reserves this path; MVP ships UC-1 and UC-2 only.

---

## 3. How the physician interacts with the system today

Clinical Co-Pilot is deployed as a **web chat application** served at the agent root URL (e.g. `https://agent-production-b516.up.railway.app/`). The physician does not need Postman or patient UUIDs; the interface is built for hallway use on a laptop or tablet.

### Step 1: Sign in

A **login gate** appears first. The physician enters username and password (configured for the deployment). Nothing else is shown until authentication succeeds. Session state lives **in browser memory only** for the MVP — not in `localStorage` — so closing the tab ends the session.

### Step 2: Pick a patient by name

After login, a **patient picker** loads from `GET /patients`. Each row shows:

- **Name** (large, primary) — e.g. John Doe, Daniel Williams
- **Age and sex** (secondary) — e.g. 22y · Male

The internal FHIR patient UUID is **not** shown; physicians choose charts the way they think about them. A search box filters by name.

Selecting a patient **starts a chat session locked to that patient**. The header then shows who is logged in and which patient is active.

### Step 3: Opening recap (automatic)

On patient select, the UI automatically sends the opening summarization request. The physician sees:

- Her message (the recap request) in a **right-aligned bubble**
- The agent's answer in a **left-aligned bubble**, formatted for quick reading:
  - Plain-language clinical facts line by line
  - **Source affordances** — small link icons at the end of sentences; hover shows the real `Condition/id`, `MedicationRequest/id`, etc.
  - A **verification badge** under each agent message: **Verified · N sources** when structural checks pass, or **Unverified** when a citation does not match retrieved records or another rule fails

A loading indicator appears while OpenEMR and the model work; latency can be tens of seconds on a cold remote instance, so the UI signals wait explicitly.

### Step 4: Follow-up questions

A text box at the bottom accepts natural-language follow-ups. **Enter** or **Send** posts the next turn to `POST /chat` with:

- `patient_id` (unchanged for the session)
- `message` (the new question)
- `conversation_history` (all prior turns)

Replies render as additional bubbles; history accumulates so pronoun-style questions work. **Change patient** or **Sign out** clears the conversation and returns to the picker or login screen.

### What she sees vs. what the API exposes

| Physician-facing | Under the hood |
|------------------|----------------|
| Name in picker | Minimum necessary for selection only; clinical answers use age/sex, not name |
| Readable diagnosis labels | Mapped from ICD when OpenEMR FHIR omits problem-list titles; citations still point at real Condition resources |
| Verified / Unverified badge | `verification.py` — hallucinated citations, uncited claims, prescriptive language |
| Link icons | `[ResourceType/id]` citations in the answer text |

For integrators and automated tests, stateless **`POST /ask`** remains available (single question, no history). The **chat UI and `POST /chat`** are the primary physician workflow.

---

## 4. Authorization and session scoping

Authorization is enforced at the **backend**, not only by hiding UI elements.

### Physician login

The physician must authenticate before the picker or chat surface appears. Credentials are validated server-side; a session token is issued for subsequent API calls. This is MVP-grade login (environment-configured credentials); production would integrate with the clinic's identity provider.

### One patient per chat session

Every chat request includes an explicit **`patient_id`**. That value is the sole authority for:

- Whether the patient exists (`patient_exists` gate — runs **before** any tool or cache access)
- Which chart is loaded from OpenEMR
- Which FHIR resources tools are allowed to retrieve
- Which record IDs verification treats as valid citation targets

The client sends **conversation history**, but history does **not** carry patient scope. The server never infers "which patient" from free text.

### Refusing cross-patient requests

If the physician's message names **another patient's UUID** (for example, a pasted identifier or a prompt-injection attempt), the server responds **before** calling the model or tools with a fixed refusal:

> *"I can only address the patient scoped to this session. I cannot look up or discuss a different patient identifier in this conversation."*

Additionally:

- **Tool dispatch is hard-scoped** — even if the model requests a tool with a different `patient_id` in its tool-call JSON, the runtime executes against the session's trusted id only.
- **System prompt rule** — the model is instructed never to echo another patient's identifier in its answer text.

Access control is structural: the LLM is not the security boundary. OpenEMR's authenticated FHIR API and server-side patient scoping are.

### Minimum-necessary PHI in answers

The picker shows **name** because choosing the correct chart is a legitimate identification step. **Clinical recap and follow-up answers** intentionally use minimum-necessary fields from `get_intake` (age, sex) — not the patient's name in synthesized clinical text — consistent with limiting PHI surfacing in model context and logs.

---

## 5. Boundaries — what the agent will never do

These are product requirements enforced by **prompting, tool design, and a deterministic verification pass** — not by hoping the model behaves.

| The agent will **not** | What it does instead |
|------------------------|----------------------|
| **Diagnose** | Surfaces sourced problem-list entries and findings; physician forms diagnoses |
| **Prescribe or adjust doses** | Declines prescriptive requests; may cite current medications with sources |
| **Judge appropriateness** ("you should start…", "increase the dose", "discontinue…") | Boundary phrases are blocked in verification |
| **State unsourced clinical claims as fact** | Every claim about this patient requires a `[ResourceType/id]` citation or an allowed missing-data phrase |
| **Fabricate data** | If a field is absent from tool results, the agent says so — it does not invent plausible values |
| **Cross patients** | Refuses other-patient UUIDs; tools cannot fetch another chart; answers stay scoped |
| **Hide tool failures** | Retrieval errors are reported as failures, not as "no data on file" — conflating the two is treated as fabrication by omission |

The physician remains the expert. Clinical Co-Pilot is **decision support**: it assembles and cites the chart; she decides what it means and what to do next.

---

## 6. Missing and incomplete data

Real charts are sparse. The agent is explicit about gaps because **a confident silence is more dangerous than an honest "unknown."**

### Confirmed empty ("missing")

When a tool returns `missing=true` — OpenEMR authenticated successfully and the search returned **no records of that type** for this patient — the agent states that plainly, for example:

- *"No medications are on file for this patient."*
- *"No lab or imaging results are on file for this patient."*

It does **not** speculate why the data is absent (data entry lag, patient declined labs, etc.).

### Retrieval failure ("error")

When a tool returns `error` — network timeout, auth failure, malformed bundle, or other infrastructure problem — the agent reports that **retrieval failed**. It does **not** phrase a failed fetch as an empty chart, because that would imply someone verified the absence of data when the system never successfully looked.

Wording is kept distinguishable: **"on file" / "does not exist for this patient"** vs **"could not retrieve" / "retrieval failed"**.

### Partial charts

The agent may have diagnoses but no labs, or medications but no imaging. Each category is reported on its own merits. A hole in one category never authorizes guesswork in another.

### Display limits from OpenEMR FHIR

Some problem-list **titles** (e.g. "Keratoconus") exist in the OpenEMR UI but do not appear in FHIR `Condition.code.display`. The MVP uses a small **code-to-name display map** for demo patients so recaps read cleanly. The **citation still points at the real Condition resource**; verification checks the resource id, not the friendly label. When no label is available, showing the ICD code is acceptable; **silently dropping a diagnosis is not**.

### Verification when data is thin

If the agent correctly says "no diagnoses on file" with no citations, verification can pass — no fabricated sources were invented. If it claims a diagnosis without a retrieved Condition id, verification fails and the UI shows **Unverified**. That is intentional: the badge is doing its job.

---

## Summary

| Topic | Design stance |
|-------|----------------|
| User | Primary care physician, ~90 seconds between rooms |
| UC-1 Recap | Sourced, readable, gaps stated — **shipped** |
| UC-2 Follow-up | Multi-turn, same patient, verified each turn — **shipped** |
| UC-3 Compare | Visit-window deltas — **future** |
| Interface | Login → name picker → chat with recap, citations, verification badge |
| Authorization | Backend patient lock; refuse cross-patient ids; tools hard-scoped |
| Boundaries | No diagnose / prescribe / fabricate / cross-patient |
| Missing data | Say "not on file" or "could not retrieve" — never guess |

For architecture and implementation detail, see [ARCHITECTURE.md](ARCHITECTURE.md). For OpenEMR audit findings that motivate trust and performance choices, see [AUDIT.md](AUDIT.md).
