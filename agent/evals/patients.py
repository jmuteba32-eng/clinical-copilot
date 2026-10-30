"""Real synthetic patient identifiers this eval suite targets on the live
OpenEMR instance.

Every fact in the comments below was verified directly against the live
FHIR API while building this suite (2026-07-09) -- not assumed from the
constant names or from the original request that specified these IDs.
Two details in that original request turned out not to match what is
actually on the live instance; both are called out explicitly rather than
silently "corrected", since a mismatch between assumed and real data is
exactly the kind of gap this whole project exists to surface, not paper over.
"""

from __future__ import annotations

# Verified live data: ONE real medication (Oxymetazoline Drops / Upneeq,
# active -- MedicationRequest id a2389242-0b1e-411e-97a3-d183c5bdd640) and
# two duplicate Condition entries coded H02.839 ("unspecified disorder of
# eyelid, unspecified eyelid"). No Observation/DiagnosticReport (lab/imaging)
# records.
#
# CORRECTION vs. the original description this suite was requested with:
# that description said this patient had "no meds" and a "dry eye syndrome"
# condition. Neither holds on the live instance as of this writing -- this
# patient has a real medication, and its Condition is coded as an eyelid
# disorder, not dry eye. See PATIENT_SPARSE below for the patient that
# actually carries the dry-eye-coded condition.
PATIENT_WITH_DATA = "a250a64d-95b9-4e2b-bdfd-9ab3fc887ad1"   # John Doe: H02.839 + Upneeq (active)

# Verified live data: ZERO medications, ZERO labs/imaging, and two duplicate
# Condition entries coded H04.123 ("dry eye syndrome of bilateral lacrimal
# glands") -- this is the patient with the real dry-eye diagnosis (see
# test_regression.py), despite the original request associating that
# diagnosis with PATIENT_WITH_DATA instead.
PATIENT_SPARSE = "a250a8c3-00f1-4050-a83d-466299f6abc1"      # Jane Smith: H04.123 dry eye, no meds

# Does not exist on this OpenEMR instance at all -- confirmed via
# `tools.patient_exists`, not merely assumed from the all-zeros shape of
# the UUID.
NONEXISTENT = "00000000-0000-0000-0000-000000000000"
