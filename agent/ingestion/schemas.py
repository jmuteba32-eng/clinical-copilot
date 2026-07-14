"""Week 2 document-ingestion schemas (intake form).

SEPARATE from Week 1's agent/schemas.py on purpose: these model facts
EXTRACTED FROM AN UPLOADED DOCUMENT via a vision model, not facts read from
structured OpenEMR/FHIR resources. They inherit Week 1's SourcedRecord so the
"every record carries a source" guarantee is preserved -- but for a document,
the source is the file + page it was read from, not a FHIR resource id.

Scope is deliberately the PRD-required clinical subset -- demographics (minus
SSN and contact details, which have no clinical use here and are PHI we do not
need), chief concern, current medications, allergies, family history. The
uploaded form contains much more (insurance, pharmacy, consent, a 43-item
review-of-systems checklist); extracting only the clinically-relevant subset
is an intentional minimum-necessary-PHI decision, documented in
W2_ARCHITECTURE.md, not an oversight.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from agent.schemas import SourcedRecord


class MedicationEntry(BaseModel):
    """One current medication as written on the intake form."""

    name: str = Field(..., description="Medication name exactly as written on the form.")
    dose: Optional[str] = Field(
        default=None,
        description="Dose/strength as written. Optional: the form may leave it blank.",
    )


class AllergyEntry(BaseModel):
    """One allergy + its reaction. 'None' on the form is represented explicitly
    as a single entry with allergen='None' -- we do NOT silently emit an empty
    list, because 'the patient stated no allergies' and 'we never asked' are
    clinically different and must stay distinguishable downstream."""

    allergen: str = Field(..., description="Allergen as written (may be 'None').")
    reaction: Optional[str] = Field(default=None, description="Reaction as written. Optional.")


class FamilyHistoryEntry(BaseModel):
    """One relative's family-history row from the intake table."""

    relative: str = Field(..., description="Relative, e.g. 'Mother', 'Father', 'Sibling'.")
    condition: Optional[str] = Field(
        default=None,
        description="Condition listed for this relative, or None if the row is blank/negative.",
    )
    living: Optional[bool] = Field(
        default=None,
        description="True if marked Living=Y, False if Living=N, None if unmarked.",
    )
    deceased_age: Optional[int] = Field(
        default=None, description="Age at death if provided; otherwise None."
    )


class IntakeFormExtraction(SourcedRecord):
    """Strict schema the VLM output MUST validate against before it is trusted.

    The schema is the source of truth -- raw model output that does not conform
    is rejected, not coerced. source_id/source_type/source_date come from
    SourcedRecord; source_page is added here because a document citation must
    point to a page, not just a file (this is also the anchor the required
    bounding-box overlay will hang off later).
    """

    source_page: int = Field(
        ...,
        ge=1,
        description="1-based page number of the source document this record was extracted from.",
    )

    # Demographics (minimum-necessary: no SSN, no address, no contact numbers).
    patient_name: str = Field(..., description="Patient full name as written.")
    date_of_birth: Optional[str] = Field(default=None, description="DOB as written (not normalized).")
    sex: Optional[str] = Field(default=None, description="Sex/gender as marked.")
    race_ethnicity: Optional[str] = Field(default=None, description="Ethnicity/Race as written.")
    height: Optional[str] = Field(default=None, description="Height as written.")
    weight: Optional[str] = Field(default=None, description="Weight as written.")

    # Clinical core.
    chief_concern: str = Field(..., description="Primary health concern as written.")
    concern_onset: Optional[str] = Field(default=None, description="When the issue began, as written.")
    concern_symptoms: Optional[str] = Field(default=None, description="Current symptoms as written.")

    medications: list[MedicationEntry] = Field(
        default_factory=list,
        description="Current medications. Empty list only if none were asked/recorded.",
    )
    allergies: list[AllergyEntry] = Field(
        default_factory=list,
        description="Allergies. A single allergen='None' entry means explicitly none.",
    )
    family_history: list[FamilyHistoryEntry] = Field(
        default_factory=list,
        description="Family-history rows.",
    )
