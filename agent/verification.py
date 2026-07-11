"""Structural enforcement of "every clinical claim traces to a source ID."

`agent.py`'s system prompt ASKS Claude to cite a `[ResourceType/id]` after
every clinical claim (see its rule 2) and to never diagnose/prescribe (rule
1/6). Asking is not enforcing: a model can drop a citation, or cite an ID it
never actually retrieved (a HALLUCINATED SOURCE -- the most dangerous
failure mode, because it *looks* exactly as trustworthy as a real citation
until a physician clicks through and finds nothing there), or slip into
prescriptive language despite being told not to. None of that is caught by
the prompt itself.

This module is the second, structural layer ARCHITECTURE.md's "Verification
before display" (section 4) calls for: it runs after `ClinicalCoPilot.answer()`
produces text and before that text reaches a physician, and it checks the
*text itself* against the *records actually retrieved* -- not against
Claude's own account of what it did. Every check here is plain string/regex
logic, never a second model call asked to grade the first: a checker that
itself relies on model judgment would inherit exactly the failure modes it
exists to catch.

Deliberately checked here, in order of severity:
1. Hallucinated sources -- a `[ResourceType/id]` citation that does not
   match any record `answer()` actually returned. Fabricating a citation is
   strictly worse than fabricating an uncited claim, because it carries a
   false promise of verifiability.
2. Uncited claims -- sentences that read as clinical assertions but carry
   no citation at all.
3. Boundary violations -- prescriptive/diagnostic language rule 1 and rule
   6 of the system prompt forbid outright (see `_BOUNDARY_VIOLATION_PHRASES`).

See the module-level docstrings on the two heuristic checks
(`_looks_like_clinical_claim`, `_is_permitted_uncited`) for an honest
accounting of what they do and do not catch -- neither is, or can be, a
complete natural-language understanding of "is this sentence a clinical
claim". The hallucinated-source and boundary-violation checks, by contrast,
are exact string/set matches with no such caveat.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, Field

from agent.cache import CachedPatientChart
from agent.schemas import SourcedRecord

# --------------------------------------------------------------------------
# Citation extraction
# --------------------------------------------------------------------------
#
# Mirrors the exact format the system prompt instructs Claude to use (see
# agent.py's _HARD_RULES, rule 2): "[ResourceType/id]", e.g.
# "[Condition/abc-123]", optionally chained ("[Condition/abc-123][Encounter/xyz-789]").
# The ID character class intentionally excludes '.' and matches this
# project's actual source_ids (see tools.py's FHIR ID parsing, which are
# opaque alphanumeric/hyphen strings, e.g. OpenEMR's UUIDs) -- not the full
# range FHIR's spec technically permits (which also allows '.'). A '.' would
# be ambiguous against sentence-ending punctuation immediately after a
# citation anyway, so excluding it here is a deliberate, not accidental,
# narrowing.

_CITATION_RE: Final[re.Pattern[str]] = re.compile(r"\[([A-Za-z]+)/([A-Za-z0-9\-]{1,64})\]")


def _extract_citations(answer_text: str) -> list[str]:
    """Every `[ResourceType/id]` citation in the text, as `"ResourceType/id"` strings.

    Returns the full type-qualified key (not just the bare id) because a
    bare id is ambiguous across resource types -- `Condition/1` and
    `Observation/1` are different records, and collapsing them to `"1"`
    would let a citation to a real Observation wrongly "verify" a claim
    that actually cited a Condition that was never retrieved.
    """
    return [f"{resource_type}/{record_id}" for resource_type, record_id in _CITATION_RE.findall(answer_text)]


# --------------------------------------------------------------------------
# Sentence splitting + uncited-claim heuristic
# --------------------------------------------------------------------------
#
# HONEST LIMITATION (see module docstring): sentence splitting on
# punctuation is not a real NLP sentence boundary detector. It will
# mis-split abbreviations containing periods (e.g. "Dr. Smith", "q.i.d.")
# and will not split a run-on sentence that packs a cited claim and an
# uncited one together with "and"/";" -- that combined sentence is treated
# as a single unit and only flagged as uncited if it contains no citation
# anywhere in it at all.

_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")

# Rule 8 (agent.py): explanatory background the physician should not read as a
# chart-specific cited claim. Recognized wherever it appears — combined at
# the bottom after "---", multiple clauses on one line, or the canonical
# one-line-per-note format.
_LEADING_DIVIDER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*---\s*")
_STANDALONE_DIVIDER_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*---\s*$", re.MULTILINE)
_DIVIDER_BEFORE_GENERAL_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*---\s*(?=General context:)",
    re.IGNORECASE,
)

# Phrasings the system prompt's own rules instruct Claude to use for the
# cases where NOT citing is correct, not a gap: rule 3 (missing data), rule
# 4 (tool error), rule 6 (declining to diagnose/prescribe/dose/judge), rule
# 8 (general background, not a claim about this patient), and the agent's
# own max-iteration/truncation fallback text (agent.py's `_extract_final_text`
# and the iteration-cap message in `answer()`). Matched loosely (substrings
# of the actual fixed phrasing) since Claude paraphrases rather than
# repeating the rule text verbatim -- this is necessarily a heuristic
# allowlist, not a guarantee every legitimately uncited sentence matches one
# of these patterns (see `_is_permitted_uncited`).
_PERMITTED_UNCITED_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"on file for this patient",
        r"does not exist for this patient",
        # Broad on purpose (no required verb between "no" and "on file"/
        # "recorded"/"documented"): a live run surfaced "show no records on
        # file" for missing data, which the earlier, verb-anchored version
        # of this pattern did not match. HONEST LIMITATION (see module
        # docstring): loosening this trades in the other direction -- a
        # sentence that happens to contain "no" and one of these words for
        # unrelated reasons within 60 characters would now be wrongly
        # exempted from the citation check instead of flagged. Accepted
        # rather than continuing to special-case every new grammatical
        # variant Claude produces for the same underlying, legitimate
        # rule-3 fact; a real over-exemption here would still have to
        # coincidentally read like an absence statement to slip through.
        r"no .{0,60}(on file|recorded|documented)",
        r"could not retrieve",
        r"retrieval failed",
        r"unable to retrieve",
        r"(cannot|unable to|can'?t) provide",
        r"physician'?s decision",
        r"physician (makes|decides|should decide)",
        r"is (a decision for|up to) (the )?physician",
        r"unable to reach a final answer",
        r"try a narrower or simpler question",
        r"this response was truncated",
        r"cannot (diagnose|prescribe|recommend a dose|judge)",
        # session.py's fixed other-patient refusal (main.py /chat gate) —
        # procedural, not a clinical claim about this patient.
        r"only address the patient scoped to this session",
        r"cannot look up or discuss a different patient identifier",
    )
)

# A sentence with none of these clinical-content signals is treated as
# connective/procedural prose (greetings, summaries, "Here's what I
# found:") rather than a claim needing a citation. HONEST LIMITATION: this
# list is necessarily incomplete -- e.g. a claim phrased entirely in terms
# a keyword list here doesn't cover (say, a vital sign quoted as a bare
# number with no unit word nearby) can slip through as "not clinical" and
# be missed. It is deliberately broad rather than narrow, because a missed
# uncited claim is a worse failure than an over-eager false positive on a
# procedural sentence -- the latter just adds one extra line to
# `uncited_claims` for a physician to glance at and dismiss.
_CLINICAL_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"diagnos\w*|condition\w*|medicat\w*|drug\w*|dos(e|age)\w*|"
    r"lab\w*|test\w*|result\w*|treatment\w*|symptom\w*|allerg\w*|"
    r"immuniz\w*|vital\w*|glucose|a1c|procedure\w*|surger\w*|prescri\w*|"
    r"therapy|diseases?|disorders?|syndrome\w*|infection\w*|"
    r"encounter\w*|visit\w*|blood pressure|heart rate|temperature|"
    r"weight|height|bmi|elevated|abnormal|active|chronic|acute|"
    r"patient (has|is|was|presents|reports)"
    r")\b",
    re.IGNORECASE,
)


def _split_sentences(answer_text: str) -> list[str]:
    normalized = answer_text.replace("\n", " ")
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(normalized) if s.strip()]


def _is_general_context_unit(text: str) -> bool:
    """True when ``text`` is rule-8 explanatory background, not a chart claim."""
    stripped = _LEADING_DIVIDER_RE.sub("", text.strip())
    return bool(re.match(r"^general context:", stripped, re.IGNORECASE))


def _expand_verification_units(answer_text: str) -> list[str]:
    """Split answer text into units for the uncited-claim checker.

    Separates each ``General context:`` clause even when the model combined
    them after a ``---`` divider (a recap formatting failure this module
    must tolerate without falsely flagging legitimate background). Non-general
    chunks are still sentence-split so follow-up paragraph prose is checked.
    """
    text = _STANDALONE_DIVIDER_LINE_RE.sub("\n", answer_text)
    text = _DIVIDER_BEFORE_GENERAL_CONTEXT_RE.sub(" ", text)
    units: list[str] = []
    for chunk in re.split(r"(?i)(?=General context:)", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if _is_general_context_unit(chunk):
            units.append(chunk)
            continue
        units.extend(_split_sentences(chunk))
    return units


def _is_permitted_uncited(sentence: str) -> bool:
    if _is_general_context_unit(sentence):
        return True
    return any(pattern.search(sentence) for pattern in _PERMITTED_UNCITED_PATTERNS)


def _looks_like_clinical_claim(sentence: str) -> bool:
    """Heuristic only -- see the module docstring and `_CLINICAL_KEYWORD_RE` comment above."""
    return bool(_CLINICAL_KEYWORD_RE.search(sentence))


# --------------------------------------------------------------------------
# Deterministic clinical-constraint (boundary) checks
# --------------------------------------------------------------------------
#
# These are NOT a heuristic: an exact (case-insensitive) substring match
# against phrases the system prompt's rule 1/6 forbid outright. The agent
# surfaces sourced data; the physician decides. Any of these phrases in the
# output is a structural boundary violation regardless of what a model
# "judged" was appropriate to say -- this check exists specifically because
# rule 1/6 alone is a prompt, not an enforcement mechanism.

_BOUNDARY_VIOLATION_PHRASES: Final[tuple[str, ...]] = (
    "you should prescribe",
    "increase the dose",
    "the diagnosis is wrong",
    "i recommend starting",
    "discontinue",
)


def _check_boundary_violations(answer_text: str) -> list[str]:
    lowered = answer_text.lower()
    return [phrase for phrase in _BOUNDARY_VIOLATION_PHRASES if phrase in lowered]


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------


class VerificationResult(BaseModel):
    """Outcome of checking one agent answer against the records it actually retrieved.

    `passed` is `False` if ANY of the three checks below found something --
    there is no partial-credit notion of "mostly verified" here, because
    the whole point is that a physician reading a failed result should not
    have to guess which parts are trustworthy.
    """

    passed: bool
    hallucinated_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Citations of the form 'ResourceType/id' that appeared in the answer text but do not match any "
            "record actually retrieved by a tool call this turn. Non-empty here means the model cited a source "
            "that does not exist -- the single most dangerous failure mode this module exists to catch."
        ),
    )
    uncited_claims: list[str] = Field(
        default_factory=list,
        description=(
            "Sentences that read as clinical assertions but carry no [ResourceType/id] citation at all. "
            "Heuristic -- see agent/verification.py's module docstring for what this does and does not catch."
        ),
    )
    boundary_violations: list[str] = Field(
        default_factory=list,
        description=(
            "Exact prescriptive/diagnostic phrases (e.g. 'increase the dose') found in the answer text. The "
            "agent surfaces sourced data; the physician decides -- any match here is a hard rule violation, "
            "not a judgment call. Not one of the four fields requested in the original spec, but included "
            "because requirement 4 (main.py showing 'what failed') needs somewhere to put this check's result; "
            "omitting it would make a boundary-violation failure look identical to a clean pass except for "
            "`passed=False`, with no way to tell a physician (or a log line) which of the three checks failed."
        ),
    )
    checked_claims: int = Field(
        default=0,
        description=(
            "Total sentences classified as clinical claims (cited + uncited) -- i.e. how much of the answer "
            "this module actually evaluated for citation, as opposed to skipped as non-clinical/permitted-uncited prose."
        ),
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def records_for_verification(
    turn_records: list[SourcedRecord],
    session_chart: CachedPatientChart | None,
) -> list[SourcedRecord]:
    """Build the record set citations are checked against for one turn.

    Opening turns usually populate ``turn_records`` via tool calls (or
    prefetch). Follow-up turns often answer from conversation context without
    re-running tools, leaving ``turn_records`` empty even though the answer
    still cites [Condition/id] values from the opening recap.

    A citation is valid only if it appears in records actually retrieved for
    this patient during the session — the assembled first-visit chart in
    ``session_chart`` (cache/prefetch), unioned with anything freshly
    returned this turn. Citations to resources never in that union     still fail.
    """
    by_key: dict[tuple[str, str], SourcedRecord] = {}
    if session_chart is not None:
        for record in session_chart.all_records():
            by_key[(record.source_type, record.source_id)] = record
    for record in turn_records:
        by_key[(record.source_type, record.source_id)] = record
    return list(by_key.values())


def verify(answer_text: str, retrieved_records: list[SourcedRecord]) -> VerificationResult:
    """Check one agent answer's citations and language against known records.

    Callers should pass the output of :func:`records_for_verification` so
    follow-up turns validate citations against the session chart, not only
    records freshly returned on this turn. Does not call OpenEMR or Anthropic.
    """
    known_sources = {f"{record.source_type}/{record.source_id}" for record in retrieved_records}

    cited = _extract_citations(answer_text)
    hallucinated_sources = sorted({citation for citation in cited if citation not in known_sources})

    checked_claims = 0
    uncited_claims: list[str] = []
    for sentence in _expand_verification_units(answer_text):
        if _CITATION_RE.search(sentence):
            checked_claims += 1
            continue
        if _is_permitted_uncited(sentence):
            continue
        if not _looks_like_clinical_claim(sentence):
            continue
        checked_claims += 1
        uncited_claims.append(sentence)

    boundary_violations = _check_boundary_violations(answer_text)

    passed = not hallucinated_sources and not uncited_claims and not boundary_violations
    return VerificationResult(
        passed=passed,
        hallucinated_sources=hallucinated_sources,
        uncited_claims=uncited_claims,
        boundary_violations=boundary_violations,
        checked_claims=checked_claims,
    )
