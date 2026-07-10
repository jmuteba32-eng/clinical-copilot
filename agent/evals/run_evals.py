"""Runs every eval suite in `agent/evals/` and prints a pass/fail summary.

Run as:

    python -m agent.evals.run_evals

from the repo root (or `python -m agent.evals.run_evals` from inside a
container via `openemr-cmd`-style tooling -- anywhere the `agent` package
is importable). This is a thin wrapper around `pytest.main()`, not a
reimplementation of a test runner: it registers a small in-process plugin
to tally pass/fail per suite FILE (not per pytest.ini's own notion of a
"suite", which pytest doesn't have) and renders that as a table, then
delegates everything else -- discovery, fixtures, assertions -- to pytest
itself.

Costs real Anthropic tokens and real OpenEMR round trips (see
conftest.py's module docstring) -- this is a pre-deploy gate, not something
to run in a fast inner loop.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest

_EVALS_DIR = Path(__file__).resolve().parent

# Display order only -- pytest.main() below always discovers and runs every
# test_*.py in _EVALS_DIR regardless of this tuple; it exists purely so the
# summary table prints suites in a stable, meaningful order instead of
# whatever order pytest happened to collect them in.
_SUITE_DISPLAY_ORDER = ("invariants", "boundaries", "adversarial", "regression")


def _suite_name(fspath: object) -> str:
    """`test_invariants.py` -> `"invariants"`."""
    stem = Path(str(fspath)).stem
    return stem.removeprefix("test_")


class _ResultCollector:
    """pytest plugin: tallies pass/fail per suite via a standard hook.

    Registered directly as a `plugins=[...]` object to `pytest.main()`
    rather than a separate installed plugin package, since this tally only
    needs to live for the duration of one `run_evals.py` invocation.
    """

    def __init__(self) -> None:
        self.passed: dict[str, int] = defaultdict(int)
        self.failed: dict[str, int] = defaultdict(int)
        self.failed_nodeids: list[str] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # Each test emits three reports (setup/call/teardown); only "call"
        # reflects whether the test body itself passed, which is the only
        # one that should count toward the table below. A setup/teardown
        # failure (e.g. a fixture erroring) still needs to be surfaced,
        # though -- counted as a failure here too, since silently dropping
        # it would under-report real failures as if the suite were clean.
        if report.when == "call":
            suite = _suite_name(report.fspath)
            if report.passed:
                self.passed[suite] += 1
            elif report.failed:
                self.failed[suite] += 1
                self.failed_nodeids.append(report.nodeid)
        elif report.when in ("setup", "teardown") and report.failed:
            suite = _suite_name(report.fspath)
            self.failed[suite] += 1
            self.failed_nodeids.append(f"{report.nodeid} ({report.when})")


def _print_summary(collector: _ResultCollector) -> int:
    all_suites = list(_SUITE_DISPLAY_ORDER)
    for suite in sorted(set(collector.passed) | set(collector.failed)):
        if suite not in all_suites:
            all_suites.append(suite)

    print()
    print(f"{'Suite':<15} {'Passed':>8} {'Failed':>8}")
    print("-" * 33)
    total_passed = total_failed = 0
    for suite in all_suites:
        p, f = collector.passed.get(suite, 0), collector.failed.get(suite, 0)
        if p == 0 and f == 0:
            continue
        total_passed += p
        total_failed += f
        print(f"{suite:<15} {p:>8} {f:>8}")
    print("-" * 33)
    print(f"{'TOTAL':<15} {total_passed:>8} {total_failed:>8}")

    if collector.failed_nodeids:
        print("\nFailed:")
        for nodeid in collector.failed_nodeids:
            print(f"  - {nodeid}")

    return 0 if total_failed == 0 else 1


def main() -> int:
    collector = _ResultCollector()
    pytest.main([str(_EVALS_DIR), "-v", "--tb=short"], plugins=[collector])
    return _print_summary(collector)


if __name__ == "__main__":
    sys.exit(main())
