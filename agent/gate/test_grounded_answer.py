"""GATE / factually_consistent + safe_refusal boolean rubrics (offline).

Proves the two grader-feedback guarantees hold by construction:
- safe_refusal: off-scope questions ("2+2", "who is LeBron", "Testing") route to
  an out-of-scope refusal and are NEVER marked verified.
- factually_consistent: an in-scope answer is verified ONLY when it carries at
  least one citation; an answer with no sources cannot claim Verified.

Guards against the exact Week 1 defect the grader flagged: "Verified - 0 sources".
"""
from __future__ import annotations

import pytest

from agent.pipeline.answer import (
    Citation,
    GroundedAnswer,
    answer_question,
    classify_scope,
)

_GUIDELINE_CITE = Citation(
    source_type="guideline",
    source_id="dermatochalasis_guideline.pdf",
    page_or_section="page 5",
    field_or_chunk_id="dermatochalasis_guideline.pdf#p5",
    quote_or_value="repair indications for dermatochalasis",
)


@pytest.mark.parametrize("q", ["2+2", "what is 3 x 4", "who is LeBron James", "what is the weather today", "Testing"])
def test_safe_refusal_off_scope_is_never_verified(q: str) -> None:
    ans = answer_question(q, intake_summary="", evidence_citations=[], answer_text="")
    assert ans.scope == "out_of_scope"
    assert ans.verified is False
    assert ans.refusal_reason and "out_of_scope" in ans.refusal_reason


@pytest.mark.parametrize("q", [
    "What is the patient's chief concern?",
    "What medications is he taking?",
    "What does the dermatochalasis guideline recommend?",
])
def test_in_scope_questions_classified_in_scope(q: str) -> None:
    assert classify_scope(q) == "in_scope"


def test_verified_requires_a_citation() -> None:
    """In-scope + at least one citation => verified True."""
    ans = answer_question(
        "What does the guideline recommend for this patient?",
        intake_summary="John Doe, dermatochalasis",
        evidence_citations=[_GUIDELINE_CITE],
        answer_text="Per the guideline, surgical repair is indicated when...",
    )
    assert ans.scope == "in_scope"
    assert ans.verified is True
    assert len(ans.citations) == 1


def test_in_scope_without_citations_is_not_verified() -> None:
    """The exact Week 1 defect: in-scope but zero sources must NOT be Verified."""
    ans = GroundedAnswer(scope="in_scope", answer_text="something", citations=[])
    assert ans.verified is False
