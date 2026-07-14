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
_MODEL = "claude-sonnet-4-6"


def _load_intake() -> dict:
    return json.loads(_INTAKE_FIXTURE.read_text(encoding="utf-8-sig"))


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


def respond(question: str) -> GroundedAnswer:
    """Run the full pipeline for one question about the cached patient."""
    scope = classify_scope(question)
    if scope == "out_of_scope":
        return answer_question(question, intake_summary="", evidence_citations=[], answer_text="")

    intake = _load_intake()
    summary = _intake_summary(intake)

    # Retrieval query expansion: the physician's question often omits the
    # condition ("do you recommend surgery?"), leaving keyword retrieval nothing
    # clinical to match on. The patient's own extracted chief concern and
    # symptoms are appended so every retrieval is grounded in what this patient
    # actually has, not just how the question happened to be phrased.
    retrieval_query = " ".join(
        p for p in (question, intake.get("chief_concern"), intake.get("concern_symptoms")) if p
    )
    hits = retrieve_evidence(retrieval_query, k=3)
    evidence_block = "\n\n".join(
        f"[{i+1}] {h.chunk.guideline_file} p{h.chunk.page}: {h.chunk.text[:600]}"
        for i, h in enumerate(hits)
    )
    citations = [Citation(**h.citation()) for h in hits]

    prompt = (
        "You are a clinical co-pilot preparing a physician for a follow-up visit. "
        "Answer the question using ONLY the patient intake summary and the guideline "
        "evidence below. Do not invent facts. If the evidence does not support an "
        "answer, say so plainly. Cite guideline claims by their [n] marker.\n\n"
        f"PATIENT INTAKE SUMMARY:\n{summary}\n\n"
        f"GUIDELINE EVIDENCE:\n{evidence_block}\n\n"
        f"PHYSICIAN QUESTION: {question}"
    )

    client = Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    answer_text = "".join(b.text for b in resp.content if b.type == "text").strip()

    return answer_question(
        question=question,
        intake_summary=summary,
        evidence_citations=citations,
        answer_text=answer_text,
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
