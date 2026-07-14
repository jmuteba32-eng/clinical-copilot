"""GATE / schema_valid — Week 2 intake-form extraction schema (offline, no VLM call)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.ingestion.schemas import (
    AllergyEntry,
    IntakeFormExtraction,
    MedicationEntry,
)


def _valid_intake_kwargs() -> dict:
    return dict(
        source_id="John_Doe-Intake-Form.pdf",
        source_type="intake_form",
        source_page=4,
        patient_name="John Doe",
        chief_concern="Dermatochalasis",
        medications=[MedicationEntry(name="Oxymetazoline Drops (Upneeq)", dose="3")],
        allergies=[AllergyEntry(allergen="None")],
    )


def test_intake_extraction_accepts_valid() -> None:
    rec = IntakeFormExtraction(**_valid_intake_kwargs())
    assert rec.chief_concern == "Dermatochalasis"
    assert rec.medications[0].name.startswith("Oxymetazoline")
    assert rec.source_page == 4


def test_intake_extraction_rejects_missing_chief_concern() -> None:
    """A clinical extraction with no chief concern is unusable -- must reject.

    Guards against the VLM omitting the one field the whole scenario turns on.
    """
    kwargs = _valid_intake_kwargs()
    del kwargs["chief_concern"]
    with pytest.raises(ValidationError):
        IntakeFormExtraction(**kwargs)


def test_intake_extraction_rejects_missing_source_page() -> None:
    """Document citation must carry a page. Guards the provenance the bbox
    overlay depends on."""
    kwargs = _valid_intake_kwargs()
    del kwargs["source_page"]
    with pytest.raises(ValidationError):
        IntakeFormExtraction(**kwargs)


def test_intake_extraction_rejects_page_zero() -> None:
    """Pages are 1-based; page 0 is a bug, not a valid citation."""
    kwargs = _valid_intake_kwargs()
    kwargs["source_page"] = 0
    with pytest.raises(ValidationError):
        IntakeFormExtraction(**kwargs)
