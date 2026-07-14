"""GATE / no_phi_in_logs boolean rubric (offline).

The fifth rubric category. Traces, logs, and eval artifacts must not carry
patient identifiers, raw document text, or extracted clinical values.

Guards against three concrete leaks:
  1. A schema growing a PHI field that has no clinical use case (SSN, address,
     phone) -- which would then flow into every downstream trace.
  2. The Week 2 worker handoff logs (new surface, added with the graph) carrying
     patient state instead of just routing metadata.
  3. Free-text physician questions being logged verbatim -- they routinely
     restate chart specifics ("does his A1c look better than last visit"), so
     the question text is PHI-adjacent and only its LENGTH may be recorded.
"""
from __future__ import annotations

import logging

from agent.ingestion.schemas import IntakeFormExtraction
from agent.pipeline.graph import _log_handoff

_FORBIDDEN_FIELDS = ("ssn", "social_security", "address", "phone", "email", "insurance", "policyholder")


def test_intake_schema_has_no_unnecessary_phi_fields() -> None:
    """Minimum-necessary: the extraction schema must not model PHI with no
    clinical use case, because an extracted field flows into traces and logs."""
    fields = set(IntakeFormExtraction.model_fields)
    for name in fields:
        low = name.lower()
        for banned in _FORBIDDEN_FIELDS:
            assert banned not in low, f"schema field '{name}' models unnecessary PHI ({banned})"


def test_worker_handoff_log_carries_no_patient_state(caplog) -> None:
    """Handoff logs may carry routing metadata and a correlation id only."""
    state = {
        "correlation_id": "test-cid-123",
        "question": "does John Doe need surgery",
        "intake": {"patient_name": "John Doe", "date_of_birth": "03/09/2002"},
        "intake_summary": "Patient: John Doe, DOB 03/09/2002",
    }
    with caplog.at_level(logging.INFO, logger="agent.graph"):
        _log_handoff(state, "supervisor", "evidence_retriever", "no evidence yet")

    assert caplog.records, "handoff was not logged at all"
    for rec in caplog.records:
        blob = (rec.getMessage() + " " + str(getattr(rec, "__dict__", {}))).lower()
        assert "john doe" not in blob, "handoff log leaked the patient name"
        assert "03/09/2002" not in blob, "handoff log leaked the patient DOB"
        assert "surgery" not in blob, "handoff log leaked the question text"
        assert "test-cid-123" in blob, "handoff log lost the correlation id"


def test_handoff_record_returned_to_state_has_no_phi() -> None:
    """The handoffs list is returned in graph state and could be surfaced in a
    trace view -- it must stay routing metadata only."""
    state = {"correlation_id": "cid", "question": "does John Doe need surgery", "intake": {"patient_name": "John Doe"}}
    entries = _log_handoff(state, "supervisor", "answer", "ready to answer")
    for e in entries:
        assert set(e) == {"from", "to", "reason"}, f"handoff entry has unexpected keys: {set(e)}"
        assert "john doe" not in str(e).lower()
        assert "surgery" not in str(e).lower()
