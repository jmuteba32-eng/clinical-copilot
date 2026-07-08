# USERS.md — Clinical Co-Pilot

## 1. The User
Dr. Alvarez — Primary Care Physician, 20-patient day. ~90 seconds between rooms to reload the next patient's context: who they are, why they're here, what changed since last time, what matters today. She is the expert; she needs her patient's history assembled and trustworthy, fast.

Why this user: bounded data need (one patient, recent changes), crisp latency (seconds not minutes), and a strong "why an agent" story — her question differs per patient and she can't know it until she's in the room.

## 2. Workflow — the 90 seconds
8:49 finishes prior patient; 8:50 opens Co-Pilot on next chart, gets a sourced recap; 8:51 asks a follow-up in plain language; 8:52 walks in already knowing the story.

## 3. Use Cases
UC-1 Opening Recap ("Previously on…"): synthesized summary, every claim traceable to a source, gaps stated. Why an agent: assembled fresh from scattered records; a dashboard can't narrate or state blind spots.
UC-2 Follow-up Q&A (multi-turn): unpredictable, patient-specific questions; the second depends on the first. Why an agent: can't pre-build a box for a question you didn't anticipate.
UC-3 Visit-to-Visit Comparison: physician picks the window; agent surfaces the sourced delta (pain, meds, mobility, imaging). Why an agent: the doctor chooses which visits/measures in the moment.

## 4. Boundaries (what the agent must NOT do)
Never diagnose or prescribe (surfaces sourced patterns; physician decides). Never state an unsourced claim as fact. Never fabricate around missing data (says "I couldn't retrieve X" / "no records after May 2"). Never show a patient the user isn't authorized to see — access enforced before data reaches the model.

## 5. Traceability
UC-1 recap -> get_intake, get_testing, get_diagnosis, get_treatment (parallel). UC-2 -> same + get_visit. UC-3 -> get_visit across visits. All -> verification (claim -> source ID) + access control at the data layer.
