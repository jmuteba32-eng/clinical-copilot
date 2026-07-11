"""Session-scoped security helpers for multi-turn /chat.

The patient_id for a conversation is fixed at the HTTP boundary: every
turn's request carries the same opaque FHIR id, and that value — never
anything the model or the client-supplied history suggests — is what
gates OpenEMR access and tool dispatch.

This module holds the deterministic, pre-LLM checks that enforce that
contract on the physician's *current* message before any tools run.
"""

from __future__ import annotations

import re
from typing import Final

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Fixed refusal string (not model-generated) when the current message names
# another patient's identifier. Returned with verification.passed=True
# because no clinical claims were made and no tools ran.
OTHER_PATIENT_REFUSAL_MESSAGE: Final[str] = (
    "I can only address the patient scoped to this session. "
    "I cannot look up or discuss a different patient identifier in this conversation."
)


def foreign_patient_uuids_in_text(text: str, trusted_patient_id: str) -> list[str]:
    """UUID-shaped tokens in `text` that are not this session's patient_id.

    Case-insensitive comparison: OpenEMR ids are UUIDs and may appear in
    mixed case from copy/paste. Returns sorted unique foreign ids for logging.
    """
    trusted_lower = trusted_patient_id.lower()
    foreign = {match for match in _UUID_RE.findall(text) if match.lower() != trusted_lower}
    return sorted(foreign)
