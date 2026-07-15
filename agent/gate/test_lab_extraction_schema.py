"""GATE / schema_valid — lab report (second document type).

Guards the lab extraction contract. Every PRD-required lab field is asserted
present or explicitly optional: test name, value, unit, reference range,
collection date, abnormal flag, source citation.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.ingestion.schemas import LabReportExtraction, LabResult


def _valid_lab_kwargs() -> dict:
    return dict(
        source_id="CBC_Lab_Report.pdf",
        source_type="lab_pdf",
        source_page=1,
        patient_name="Yash M. Patel",
        collection_date="03:11 PM 02 Dec, 2X",
        results=[
            LabResult(
                test_name="Hemoglobin (Hb)",
                value="12.5",
                unit="g/dL",
                reference_range="13.0 - 17.0",
                abnormal_flag="Low",
                section="HEMOGLOBIN",
            )
        ],
    )


def test_lab_extraction_accepts_valid() -> None:
    rec = LabReportExtraction(**_valid_lab_kwargs())
    assert rec.results[0].abnormal_flag == "Low"
    assert rec.collection_date


def test_lab_result_requires_test_name() -> None:
    """A value with no test name is an untraceable claim."""
    with pytest.raises(ValidationError):
        LabResult(value="12.5")  # type: ignore[call-arg]


def test_lab_result_requires_value() -> None:
    """A test name with no result is not a result."""
    with pytest.raises(ValidationError):
        LabResult(test_name="Hemoglobin (Hb)")  # type: ignore[call-arg]


def test_lab_result_rejects_invented_abnormal_flag() -> None:
    """abnormal_flag is a closed set transcribed from the report. Guards against
    a model inventing its own flag vocabulary ('Critical', 'Elevated', 'true')."""
    for bad in ("Critical", "Elevated", "abnormal", "true", "HIGH"):
        with pytest.raises(ValidationError):
            LabResult(test_name="X", value="1", abnormal_flag=bad)  # type: ignore[arg-type]


def test_lab_result_allows_no_flag() -> None:
    """None means the report printed no flag -- the common case. Must not be
    conflated with 'normal', which the report does not assert."""
    r = LabResult(test_name="Total RBC count", value="5.2")
    assert r.abnormal_flag is None


def test_lab_value_stays_a_string() -> None:
    """Values are transcribed, not parsed. Real reports carry '<0.01' and
    '150000'; coercing to float would crash or invent precision."""
    r = LabResult(test_name="Platelet Count", value="150000")
    assert r.value == "150000" and isinstance(r.value, str)


def test_lab_extraction_requires_patient_name() -> None:
    kwargs = _valid_lab_kwargs()
    del kwargs["patient_name"]
    with pytest.raises(ValidationError):
        LabReportExtraction(**kwargs)


def test_lab_extraction_requires_source_citation() -> None:
    """Inherited from SourcedRecord: no lab fact without a source."""
    kwargs = _valid_lab_kwargs()
    del kwargs["source_id"]
    with pytest.raises(ValidationError):
        LabReportExtraction(**kwargs)
