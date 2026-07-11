"""Unit tests for General context verification handling (no live API)."""

from __future__ import annotations

from agent.schemas import DiagnosisRecord, SourcedRecord
from agent.verification import verify


def _daniel_style_records() -> list[DiagnosisRecord]:
    return [
        DiagnosisRecord(
            source_type="Condition",
            source_id="cond-1",
            display="Keratoconus",
            code="H18.603",
            clinical_status="active",
        ),
        DiagnosisRecord(
            source_type="Condition",
            source_id="cond-2",
            display="Dry eye syndrome",
            code="H04.123",
            clinical_status="active",
        ),
        DiagnosisRecord(
            source_type="Condition",
            source_id="cond-3",
            display="Dermatochalasis",
            code="H02.839",
            clinical_status="active",
        ),
    ]


def test_recap_combined_general_context_block_after_divider_passes() -> None:
    """Recap that wrongly groups General context after --- must not fail verification."""
    recap = """22-year-old male [Patient/p1].

Keratoconus [Condition/cond-1].
Dry eye syndrome [Condition/cond-2].
Dermatochalasis [Condition/cond-3].

---
General context: H04.123 typically refers to dry eye conditions.
General context: Keratoconus is a progressive corneal thinning disorder.
General context: H18.603 is the ICD-10 code for keratoconus."""

    records = _daniel_style_records()
    records.append(SourcedRecord(source_type="Patient", source_id="p1"))
    result = verify(recap, records)
    assert result.uncited_claims == []
    assert result.passed is True


def test_recap_combined_general_context_without_periods_passes() -> None:
    """Multiple General context clauses on one line (model formatting drift)."""
    recap = """Keratoconus [Condition/cond-1].
Dry eye syndrome [Condition/cond-2].
Dermatochalasis [Condition/cond-3].

---
General context: H04.123 typically refers to dry eye conditions General context: Keratoconus is a progressive corneal thinning disorder General context: H18.603 is the ICD-10 code for keratoconus"""

    result = verify(recap, _daniel_style_records())
    assert result.uncited_claims == []
    assert result.passed is True


def test_genuinely_uncited_clinical_claim_still_fails() -> None:
    """Verification must not exempt real chart claims missing citations."""
    answer = "The patient has keratoconus and dry eye syndrome."
    result = verify(answer, _daniel_style_records())
    assert result.passed is False
    assert len(result.uncited_claims) >= 1
    assert any("keratoconus" in claim.lower() for claim in result.uncited_claims)
