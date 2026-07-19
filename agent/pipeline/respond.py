"""End-to-end responder: question -> scope -> retrieve -> LLM answer -> grounded.

This is the wiring that runs the full Week 2 pipeline for a single question:
  1. classify scope (off-scope short-circuits to a labeled refusal, no LLM call)
  2. retrieve guideline evidence for the question (hybrid RAG)
  3. ask the LLM to WRITE an answer grounded ONLY in the intake facts + retrieved
     evidence, with instructions never to invent facts
  4. run the result through answer_question so the verified/citation invariant
     from step 5a applies -- an answer is Verified only if in-scope AND cited

Patient intake is loaded from a cached JSON fixture (ingested once, referenced
after). When FHIR persistence lands, this loader swaps to reading from OpenEMR
without changing the rest of the pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from agent.pipeline.answer import Citation, GroundedAnswer, answer_question, classify_scope
from agent.rag.retrieve import retrieve_evidence

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_INTAKE_FIXTURE = Path(__file__).resolve().parent / "john_doe_intake.json"

# Writable, persistent copy on the Railway volume. Ingestion writes here (the
# app directory is read-only for appuser); the loader prefers it when present,
# so a freshly-ingested document survives redeploys and drives the pipeline.
# Writable location owned by appuser. (/data is a mounted volume but not writable
# by appuser without a Dockerfile chown; home dir is always writable. Tradeoff:
# ingested file does not survive a redeploy -- acceptable for the demo, where
# upload and query happen in one session. Documented as a known limitation.)
_INTAKE_LIVE = Path.home() / ".clinical-copilot" / "john_doe_intake.json"

# The OpenEMR Patient this ingested intake form belongs to. Verified against the
# live FHIR API: this patient carries Oxymetazoline (Upneeq) and an eyelid
# disorder coded H02.839 -- the same facts the intake form states, so the
# document and the chart are the same person.
#
# /respond is scoped to this patient and refuses any other. That refusal is the
# point: without it, selecting a different patient in the UI would still return
# THIS patient's document-derived answer, which is a cross-patient leak of
# exactly the kind Week 1's session scoping exists to prevent.
FIXTURE_PATIENT_ID = "a24a1d32-9241-4a74-bc9a-f4cf7e3a97a6"

_MODEL = "claude-sonnet-4-6"


def _load_intake() -> dict:
    src = _INTAKE_LIVE if _INTAKE_LIVE.is_file() else _INTAKE_FIXTURE
    return json.loads(src.read_text(encoding="utf-8-sig"))


def _intake_summary(intake: dict) -> str:
    """Compact, PHI-minimal summary the LLM can ground on."""
    meds = ", ".join(f"{m['name']} ({m.get('dose') or 'dose n/a'})" for m in intake.get("medications", [])) or "none recorded"
    allergies = ", ".join(a["allergen"] for a in intake.get("allergies", [])) or "none recorded"
    return (
        f"Patient: {intake.get('patient_name')}, DOB {intake.get('date_of_birth')}, "
        f"{intake.get('sex')}. Chief concern: {intake.get('chief_concern')} "
        f"(onset {intake.get('concern_onset')}). Symptoms: {intake.get('concern_symptoms')}. "
        f"Current medications: {meds}. Allergies: {allergies}."
    )


def has_documents_for(patient_id: str) -> bool:
    """Whether any document has been ingested for this patient.

    Today this is a single cached fixture, so the check is an equality test.
    When ingestion writes to OpenEMR, this becomes a real lookup and nothing
    else in the pipeline changes -- the seam is deliberate.
    """
    return patient_id == FIXTURE_PATIENT_ID


def respond(question: str, correlation_id: str = "-") -> GroundedAnswer:
    """Run the Week 2 supervisor/worker graph for one question.

    Orchestration lives in agent/pipeline/graph.py (LangGraph): the supervisor
    classifies scope and routes to the intake-extractor and evidence-retriever
    workers, logging every handoff. This function adapts the graph's final state
    into the GroundedAnswer contract, which is where the verified/citation
    invariant is enforced.
    """
    from agent.pipeline.graph import build_graph

    graph = build_graph()
    final = graph.invoke({"question": question, "correlation_id": correlation_id})

    handoffs = final.get("handoffs", [])

    if final.get("scope") == "out_of_scope":
        return answer_question(
            question, intake_summary="", evidence_citations=[], answer_text="", handoffs=handoffs
        )

    citations = [Citation(**c) for c in final.get("citations", [])]
    return answer_question(
        question=question,
        intake_summary=final.get("intake_summary", ""),
        evidence_citations=citations,
        answer_text=final.get("answer_text", ""),
        handoffs=handoffs,
    )


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "What does the guideline recommend for this patient's dermatochalasis?"
    ans = respond(q)
    print(f"SCOPE: {ans.scope}")
    print(f"VERIFIED: {ans.verified}")
    if ans.refusal_reason:
        print(f"REFUSAL: {ans.refusal_reason}")
    print(f"\nANSWER:\n{ans.answer_text}\n")
    if ans.citations:
        print("CITATIONS:")
        for c in ans.citations:
            print(f"  - {c.source_id} {c.page_or_section}")
