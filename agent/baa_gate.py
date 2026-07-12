"""BAA compliance gate — administrator attestation before PHI retrieval.

IMPORTANT: ``BAA_CONFIRMED`` is NOT a Business Associate Agreement. A BAA is a
signed legal contract between a covered entity and a business associate (e.g. the
LLM provider) governing PHI handling. This module only enforces a runtime
policy: patient data must not be retrieved from OpenEMR and sent to the LLM
until an administrator has set ``BAA_CONFIRMED=true`` to attest that such an
agreement exists. Physicians using the chat UI cannot change this flag.
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

_ENV_BAA_CONFIRMED: Final[str] = "BAA_CONFIRMED"
_TRUTHY_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

BAA_REFUSAL_MESSAGE: Final[str] = (
    "PHI retrieval is disabled: no Business Associate Agreement is confirmed for the "
    "configured LLM provider. A BAA must be signed between your healthcare organization "
    "and the involved data/AI providers before patient data can be accessed. Contact your "
    "organization's compliance office or legal counsel to confirm the agreement is in place."
)


def baa_confirmed() -> bool:
    """Return whether an administrator has attested a BAA is in place.

    Defaults to ``False`` when unset or unreadable — PHI retrieval stays off.
    """
    try:
        raw = os.environ.get(_ENV_BAA_CONFIRMED, "false")
        if raw is None:
            return False
        return raw.strip().lower() in _TRUTHY_VALUES
    except Exception:
        logger.warning("baa_gate_read_failed", extra={"event": "baa_gate_read_failed"}, exc_info=True)
        return False
