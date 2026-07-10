"""Shared fixtures for the eval suites in `agent/evals/`.

These evals hit the REAL OpenEMR instance and the REAL Anthropic API -- they
are not unit tests running against a mock transport (that style already
exists in this project's development-time smoke scripts). They exist to
check BOUNDARIES, INVARIANTS, and ADVERSARIAL resistance against the actual
deployed behavior of `ClinicalCoPilot.answer()`, which cannot be verified
against a scripted mock: whether the agent actually refuses a prompt
injection, actually cites a real source, actually never fabricates are all
facts about the live model's behavior, not about this codebase's plumbing.

This suite costs real Anthropic tokens and real OpenEMR round trips on
every run. It is a pre-deploy/CI-gate suite, not a fast unit-test loop --
see `run_evals.py`.

Fixtures are deliberately function-scoped (a fresh `OpenEMRClient` and
`ClinicalCoPilot` per test) rather than session/module-scoped. A shared
client would save one OAuth round trip per test, but sharing an event-loop-
bound async resource across pytest-asyncio scopes has version-specific
sharp edges (mismatched fixture/loop scopes raise at collection time) --
not worth trading away for a suite that already spends most of its
wall-clock time waiting on OpenEMR/Anthropic latency, not on auth handshakes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from dotenv import load_dotenv

# Same rationale as main.py: resolve relative to this file, not the CWD, so
# `pytest agent/evals` from the repo root and `python -m agent.evals.run_evals`
# both find `agent/.env` regardless of where the process was launched from.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# This suite's live OpenEMR instance (Railway free tier) is documented
# elsewhere in this project as genuinely slow, not just on cold start --
# live runs while building this suite saw the OAuth token request alone
# occasionally exceed the 30s value `agent/.env` sets for the live service.
# A physician's live /ask request cannot afford to wait past that (main.py
# deliberately does not use the longer /ready-only timeout for it either),
# but an eval run can. Set unconditionally (not `setdefault`) specifically
# to override whatever `.env` already put in the environment above --
# evals have a different latency tolerance than the live service and
# should not silently inherit its tighter budget.
os.environ["OPENEMR_TIMEOUT_SECONDS"] = "60"

from agent.agent import ClinicalCoPilot  # noqa: E402
from agent.openemr_client import OpenEMRClient  # noqa: E402


@pytest_asyncio.fixture
async def openemr_client() -> AsyncIterator[OpenEMRClient]:
    async with OpenEMRClient() as client:
        yield client


@pytest_asyncio.fixture
async def copilot(openemr_client: OpenEMRClient) -> ClinicalCoPilot:
    return ClinicalCoPilot(openemr_client)
