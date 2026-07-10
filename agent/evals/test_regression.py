"""REGRESSION: a golden-file style check pinned to a specific, verified fact
about this project's live synthetic test data.

CORRECTION vs. the original spec this suite was requested against (see
patients.py for full detail): the real dry-eye-coded condition
(ICD-10 H04.123, "dry eye syndrome of bilateral lacrimal glands") belongs to
PATIENT_SPARSE, not PATIENT_WITH_DATA. This module targets whichever patient
actually carries that diagnosis on the live instance, confirmed directly
against the FHIR API rather than assumed -- and pins to the ICD-10 CODE
(a stable clinical fact) rather than a specific Condition resource id, since
this live instance's synthetic data has two duplicate Condition entries for
this diagnosis and nothing guarantees FHIR search results return them in a
stable order across runs.
"""

from __future__ import annotations

from agent.agent import ClinicalCoPilot
from agent.evals.patients import PATIENT_SPARSE
from agent.openemr_client import OpenEMRClient
from agent.tools import get_diagnosis
from agent.verification import _extract_citations

_DRY_EYE_ICD10_CODE = "H04.123"


async def test_dry_eye_condition_is_still_present_and_coded_correctly(openemr_client: OpenEMRClient) -> None:
    """Golden fact about the live dataset itself, independent of the LLM.

    If this fails, the live synthetic data has changed and
    `test_answer_cites_the_real_dry_eye_condition_id` below is no longer
    testing what it claims to -- fix the fixture data (or this module's
    target patient/code), don't widen the assertion just to make it pass.
    """
    result = await get_diagnosis(openemr_client, PATIENT_SPARSE)
    assert not result.missing
    assert result.error is None
    matches = [r for r in result.records if r.code == _DRY_EYE_ICD10_CODE]
    assert matches, f"expected a Condition coded {_DRY_EYE_ICD10_CODE}, got codes={[r.code for r in result.records]}"


async def test_answer_cites_the_real_dry_eye_condition_id(copilot: ClinicalCoPilot, openemr_client: OpenEMRClient) -> None:
    """The regression check this eval suite was actually asked for: the
    agent's answer must cite the REAL Condition id for the dry-eye
    diagnosis -- not a fabricated one, not a different patient's, and
    exactly one of the ids `get_diagnosis` itself returns for this patient
    right now (fetched fresh in this test, not hardcoded, since the live
    instance has two duplicate resources for this diagnosis with no
    guaranteed ordering).
    """
    diagnosis = await get_diagnosis(openemr_client, PATIENT_SPARSE)
    dry_eye_ids = {r.source_id for r in diagnosis.records if r.code == _DRY_EYE_ICD10_CODE}
    assert dry_eye_ids, "no dry-eye-coded Condition found -- see test_dry_eye_condition_is_still_present_and_coded_correctly"

    result = await copilot.answer(PATIENT_SPARSE, "What diagnoses does this patient have?")
    cited = set(_extract_citations(result["answer"]))

    matched = {f"Condition/{rid}" for rid in dry_eye_ids} & cited
    assert matched, (
        f"expected the answer to cite one of {sorted(dry_eye_ids)} as Condition/<id>, "
        f"but citations found were {sorted(cited)}. Answer: {result['answer']!r}"
    )
