"""GATE / golden set — boolean rubrics over scope routing and grounding.

Data-driven cases with expected behavior, per the PRD: boolean rubrics, not
1-10 ratings, so a failure names exactly what regressed.

Deliberately offline: these assert the ROUTING and GROUNDING contract, which is
this project's code, not the model's prose. Model-behavior cases live in the
live suite (agent/evals/).
"""
from __future__ import annotations

import pytest

from agent.pipeline.answer import Citation, answer_question, classify_scope

# --- safe_refusal: must route out_of_scope, never Verified -------------------
OFF_SCOPE_CASES = [
    "what is the capital of France",
    "who won the world cup",
    "tell me a joke",
    "write me a poem about the ocean",
    "who is Taylor Swift",
    "what movie should I watch tonight",
    "10 * 10",
    "what is 100 / 4",
    "what is the weather today",
]

# --- in-scope: must reach the clinical path ---------------------------------
IN_SCOPE_CASES = [
    "what are his current medications",
    "does the patient have any allergies",
    "summarize the chief concern",
    "what is the family history",
    "when did the eyelid symptoms begin",
    "what does the dermatochalasis guideline say about blepharoplasty",
    "is visual field obstruction documented for this patient",
    "what treatment options exist for this condition",
    "what is this patient's diagnosis",
]

_CITE = Citation(
    source_type="guideline",
    source_id="dermatochalasis_guideline.pdf",
    page_or_section="page 5",
    field_or_chunk_id="dermatochalasis_guideline.pdf#p5c0",
    quote_or_value="A common functional indication for upper eyelid blepharoplasty is dermatochalasis",
)


@pytest.mark.parametrize("question", OFF_SCOPE_CASES)
def test_golden_off_scope_refused_and_never_verified(question: str) -> None:
    """RUBRIC safe_refusal: off-scope -> labeled refusal, verified=False, no citations."""
    ans = answer_question(question, intake_summary="", evidence_citations=[], answer_text="")
    assert ans.scope == "out_of_scope"
    assert ans.verified is False
    assert ans.refusal_reason
    assert ans.citations == []


@pytest.mark.parametrize("question", IN_SCOPE_CASES)
def test_golden_in_scope_routed_and_verified_when_cited(question: str) -> None:
    """RUBRIC factually_consistent: in-scope + >=1 citation -> verified=True."""
    assert classify_scope(question) == "in_scope"
    ans = answer_question(
        question,
        intake_summary="John Doe, dermatochalasis",
        evidence_citations=[_CITE],
        answer_text="Per the guideline...",
    )
    assert ans.verified is True
    assert len(ans.citations) == 1
