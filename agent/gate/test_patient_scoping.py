"""GATE / safe_refusal -- patient scoping on the Week 2 endpoint.

Guards against a cross-patient leak: /respond answers from an ingested intake
document, and without this check, selecting a different patient in the UI would
still return the ingested patient's document-derived answer.
"""
from __future__ import annotations

from agent.pipeline.respond import FIXTURE_PATIENT_ID, has_documents_for


def test_documents_exist_for_the_ingested_patient() -> None:
    assert has_documents_for(FIXTURE_PATIENT_ID) is True


def test_no_documents_for_a_different_patient() -> None:
    """The dry-eye patient from the Week 1 eval set has no ingested documents."""
    assert has_documents_for("a238944b-11b5-40bb-9fd1-e5fd6c57e523") is False


def test_no_documents_for_a_nonexistent_patient() -> None:
    assert has_documents_for("00000000-0000-0000-0000-000000000000") is False


def test_no_documents_for_empty_patient_id() -> None:
    assert has_documents_for("") is False
