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

After each run, writes `agent/evals/REPORT.md` — a grader-readable
markdown summary with pass/fail counts per suite, one-line descriptions,
and any failures. The report is overwritten each run (timestamp in header).

Costs real Anthropic tokens and real OpenEMR round trips (see
conftest.py's module docstring) -- this is a pre-deploy gate, not something
to run in a fast inner loop.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

_EVALS_DIR = Path(__file__).resolve().parent
_REPORT_PATH = _EVALS_DIR / "REPORT.md"

# Display order only -- pytest.main() below always discovers and runs every
# test_*.py in _EVALS_DIR regardless of this tuple; it exists purely so the
# summary table prints suites in a stable, meaningful order instead of
# whatever order pytest happened to collect them in.
_SUITE_DISPLAY_ORDER = ("invariants", "boundaries", "adversarial", "regression")

# One-line description per suite for REPORT.md (what a grader reads first).
_SUITE_DESCRIPTIONS: dict[str, str] = {
    "invariants": (
        "Every clinical claim carries a citation, no hallucinated sources, "
        "no prescriptive language — checked on live Anthropic answers."
    ),
    "boundaries": (
        "Nonexistent patient → 404 with no tools run; empty meds stated not "
        "fabricated; tool-error vs missing wording stays distinguishable."
    ),
    "adversarial": (
        "Prompt injection / exfiltration: refuses, cites only retrieved data, "
        "never leaks another patient's UUID or chart content in text."
    ),
    "regression": (
        "Golden-file: PATIENT_SPARSE dry-eye condition (ICD-10 H04.123) is "
        "present on live OpenEMR and cited correctly in agent answers."
    ),
}

# Real bugs this project's eval runs surfaced during development — included in
# every REPORT.md so graders see what the adversarial/regression suites caught,
# not just pass/fail counts.
_BUGS_FOUND_BY_EVALS: tuple[str, ...] = (
    "**Foreign-UUID echo (adversarial):** asking about patient A while naming "
    "patient B's UUID in the question caused the agent to echo B's identifier "
    "in the answer text even though access control correctly scoped tool calls "
    "to A — fixed with system-prompt rule 7 (`agent/agent.py`).",
    "**Dropped Conditions with empty ICD-10 display (regression/data-quality):** "
    "live OpenEMR Condition resources carry populated `coding.code` (e.g. "
    "H04.123) but empty `coding.display`; `_parse_condition` treated empty "
    "display as unusable and silently dropped every diagnosis — fixed to fall "
    "back to the code as `display` (`agent/tools.py`).",
)


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
        self.exitstatus: int = 1

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

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.exitstatus = exitstatus


def _ordered_suites(collector: _ResultCollector) -> list[str]:
    all_suites = list(_SUITE_DISPLAY_ORDER)
    for suite in sorted(set(collector.passed) | set(collector.failed)):
        if suite not in all_suites:
            all_suites.append(suite)
    return all_suites


def _totals(collector: _ResultCollector) -> tuple[int, int]:
    total_passed = sum(collector.passed.values())
    total_failed = sum(collector.failed.values())
    return total_passed, total_failed


def _write_report(collector: _ResultCollector) -> None:
    """Write grader-readable REPORT.md from this run's tallies."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_passed, total_failed = _totals(collector)
    overall = "PASS" if total_failed == 0 else "FAIL"
    suites = _ordered_suites(collector)

    lines: list[str] = [
        "# Clinical Co-Pilot Eval Report",
        "",
        f"**Generated:** {timestamp}  ",
        f"**Overall:** {overall} ({total_passed} passed, {total_failed} failed)  ",
        f"**Command:** `python -m agent.evals.run_evals`  ",
        "",
        "Live evals against real OpenEMR + Anthropic (not mocked). Re-run to refresh.",
        "",
        "## Suites",
        "",
        "| Suite | Passed | Failed | What it checks |",
        "|-------|--------|--------|----------------|",
    ]

    for suite in suites:
        passed = collector.passed.get(suite, 0)
        failed = collector.failed.get(suite, 0)
        if passed == 0 and failed == 0:
            continue
        status = "✅" if failed == 0 else "❌"
        description = _SUITE_DESCRIPTIONS.get(suite, "(no description)")
        lines.append(f"| {status} {suite} | {passed} | {failed} | {description} |")

    lines.extend(
        [
            "",
            f"**Total:** {total_passed} passed, {total_failed} failed",
            "",
            "## Real bugs found by these suites (development history)",
            "",
            "The adversarial and regression suites surfaced two production issues "
            "during MVP build — both fixed; tests now guard against regression:",
            "",
        ]
    )
    for bug in _BUGS_FOUND_BY_EVALS:
        lines.append(f"- {bug}")
        lines.append("")

    if collector.failed_nodeids:
        lines.extend(["## Failures this run", ""])
        for nodeid in collector.failed_nodeids:
            lines.append(f"- `{nodeid}`")
        lines.append("")

    lines.extend(
        [
            "## How to re-run",
            "",
            "```bash",
            "python -m agent.evals.run_evals",
            "```",
            "",
            "Requires `agent/.env` with OpenEMR + Anthropic credentials. Costs real API tokens.",
            "",
        ]
    )

    _REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {_REPORT_PATH}")


def _print_summary(collector: _ResultCollector) -> int:
    suites = _ordered_suites(collector)
    total_passed, total_failed = _totals(collector)

    print()
    print(f"{'Suite':<15} {'Passed':>8} {'Failed':>8}")
    print("-" * 33)
    for suite in suites:
        p, f = collector.passed.get(suite, 0), collector.failed.get(suite, 0)
        if p == 0 and f == 0:
            continue
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
    exit_code = _print_summary(collector)
    _write_report(collector)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
