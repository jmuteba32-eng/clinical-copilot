"""Background keep-warm loop for OpenEMR on sleep-prone hosts (e.g. Railway free tier).

OpenEMR containers that idle out take tens of seconds to wake on the first
real request. A lightweight periodic ping keeps the container and OAuth token
warm so physician-facing `/ask` and `/chat` calls do not pay that cold-start
tax on the patient-existence gate.
"""

from __future__ import annotations

import asyncio
import logging
import os

from agent.openemr_client import OpenEMRClient

logger = logging.getLogger(__name__)

_ENV_KEEP_WARM_ENABLED = "KEEP_WARM_ENABLED"
_INTERVAL_SECONDS = 4 * 60


def keep_warm_enabled() -> bool:
    """Return whether the background keep-warm loop should run (default: true)."""
    raw = os.environ.get(_ENV_KEEP_WARM_ENABLED, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


async def run_keep_warm_loop(client: OpenEMRClient) -> None:
    """Ping OpenEMR's token endpoint on a fixed interval until cancelled.

    Never raises: failures are logged as warnings and the loop continues so
    a transient outage on the warm-up ping cannot take down the agent process.
    """
    while True:
        try:
            await client.ensure_authenticated()
            logger.debug("keep_warm_ping_ok", extra={"event": "keep_warm_ping_ok"})
        except Exception:
            logger.warning(
                "keep_warm_ping_failed",
                extra={"event": "keep_warm_ping_failed"},
                exc_info=True,
            )
        await asyncio.sleep(_INTERVAL_SECONDS)
