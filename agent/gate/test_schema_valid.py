"""GATE / schema_valid boolean rubric.

This is the FAST, OFFLINE half of the Week 2 eval gate -- the part that runs
in GitLab CI on every merge request and BLOCKS it on regression. It makes no
network calls, hits no live OpenEMR, spends no Anthropic tokens: it exercises
the schema contract itself, in-process, in milliseconds. The live behavioral
suite in agent/evals/ stays separate and runs pre-deploy, not here (see
W2_ARCHITECTURE.md).

Rubric category: schema_valid.
The property under test is the project's core trust guarantee (schemas.py):
a record cannot exist without a source citation. Raw VLM/LLM output must be
forced through these schemas -- the schema is the source of truth, not what
the model happens to return. If this guarantee ever regresses (someone makes
source_id optional, or a new extraction path bypasses the schema), this test
goes red and the MR is blocked.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.schemas import IntakeRecord, SourcedRecord


def test_sourced_record_rejects_missing_source_id() -> None:
    """A record with no source_id must NOT be constructible.

    Guards against: the traceability guarantee being weakened (source_id made
    optional, or a default slipped in), which would let the agent emit a claim
    that points back to nothing.
    """
    with pytest.raises(ValidationError):
        SourcedRecord(source_type="Condition")  # type: ignore[call-arg]


def test_sourced_record_rejects_missing_source_type() -> None:
    """source_type is equally mandatory: a bare resource id is ambiguous."""
    with pytest.raises(ValidationError):
        SourcedRecord(source_id="Condition/123")  # type: ignore[call-arg]


def test_sourced_record_accepts_valid_citation() -> None:
    """The positive case: a fully-cited record IS valid. Proves the test above
    fails for the right reason (missing field), not because the model is broken."""
    rec = SourcedRecord(source_id="Condition/123", source_type="Condition")
    assert rec.source_id == "Condition/123"
    assert rec.source_type == "Condition"
    assert rec.source_date is None  # optional, correctly defaulted


def test_intake_record_requires_patient_identity() -> None:
    """A patient-context record with no patient_id is unusable (every tool call
    in a session is scoped to it). Guards the IntakeRecord contract specifically."""
    with pytest.raises(ValidationError):
        IntakeRecord(source_id="Patient/1", source_type="Patient")  # type: ignore[call-arg]


def test_intake_record_valid_when_complete() -> None:
    rec = IntakeRecord(
        source_id="Patient/1",
        source_type="Patient",
        patient_id="Patient/1",
    )
    assert rec.patient_id == "Patient/1"
    assert rec.age is None and rec.sex is None  # optional fields default cleanly
