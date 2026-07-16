"""GATE / factually_consistent -- fabricated citation markers.

An answer that cites [4] when only three chunks were retrieved is claiming
evidence that does not exist. Guards the same defect class Week 1's verify()
catches for [ResourceType/id] citations.
"""
from __future__ import annotations

from agent.pipeline.answer import Citation, GroundedAnswer, hallucinated_markers

_CITES = [
    Citation(source_type="guideline", source_id="dermatochalasis_guideline.pdf", page_or_section="page 5"),
    Citation(source_type="guideline", source_id="dermatochalasis_guideline.pdf", page_or_section="page 6"),
]


def test_valid_markers_are_not_hallucinated() -> None:
    assert hallucinated_markers("Per [1] and [2], blepharoplasty is indicated.", _CITES) == []


def test_marker_beyond_citation_count_is_hallucinated() -> None:
    assert hallucinated_markers("Per [1] and [3], surgery is indicated.", _CITES) == [3]


def test_marker_with_no_citations_at_all_is_hallucinated() -> None:
    assert hallucinated_markers("Per [1], surgery is indicated.", []) == [1]


def test_answer_with_fabricated_marker_is_not_verified() -> None:
    """The invariant: a fabricated citation marker blocks Verified."""
    ans = GroundedAnswer(
        scope="in_scope",
        answer_text="Per [1] and [9], surgery is indicated.",
        citations=_CITES,
    )
    assert ans.verified is False


def test_answer_with_valid_markers_is_verified() -> None:
    ans = GroundedAnswer(
        scope="in_scope",
        answer_text="Per [1] and [2], surgery is indicated.",
        citations=_CITES,
    )
    assert ans.verified is True
