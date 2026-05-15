"""deterministic.py — All deterministic Block checks for the QA engine.

Returns list[DeterministicResult]. Blocking results short-circuit routing to Block.
Non-blocking results appear as annotations in the trace and summary.

Block triggers:
  - Meeting title / date / citySlug missing
  - Priority count mismatch
  - Stale future date reference
  - Meeting identity mismatch against normalized source
  - Arithmetic error in quantitative fields

Non-blocking annotations:
  - Haystaq data inconsistencies
  - Missing required detail fields
  - Banned words / editorial issues
"""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher

from qa.engine.models import DeterministicResult, ProjectInput

# ── Helpers ───────────────────────────────────────────────────────────────────

def _meeting_date(identity) -> date | None:
    try:
        return datetime.strptime(identity.date, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_time(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


# ── Date/temporal patterns (from temporal.py) ────────────────────────────────

_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)
_FUTURE_SIGNALS = re.compile(
    r"\b(will|would|shall|upcoming|scheduled|proposed|consider|vote on|"
    r"approve|review|hear|on monday|on tuesday|on wednesday|on thursday|"
    r"on friday|on saturday|on sunday)\b",
    re.IGNORECASE,
)
_TEMPORAL_FIELDS = ["whatIsHappening", "whatDecision", "actionItem", "recommendation"]

# ── Arithmetic pattern (from quantitative.py) ────────────────────────────────

_ARITHMETIC_RE = re.compile(
    r"([\d.]+)\s*%\s*(?:increase|decrease|growth|decline|rise|drop)"
    r".{0,60}?from\s+\$?([\d,.]+(?:\s*(?:million|billion|M|B))?)"
    r".{0,30}?to\s+\$?([\d,.]+(?:\s*(?:million|billion|M|B))?)",
    re.IGNORECASE,
)

_REQUIRED_DETAIL_FIELDS = [
    "whatIsHappening", "whatDecision", "whyItMatters",
    "recommendation", "actionItem", "askThis", "whoIsPresenting",
]

_BANNED_WORDS = ["delve", "leverage", "utilize"]

_DOLLAR_RE = re.compile(
    r"\$\s*[\d,]+(?:\.\d{1,2})?(?:\s*(?:million|billion|thousand|M|B|K))?",
    re.IGNORECASE,
)


def _normalize_dollar(s: str) -> float:
    s = s.lower().replace(",", "").replace("$", "").strip()
    for suffix, mult in [("million", 1e6), ("billion", 1e9), ("thousand", 1e3),
                          (" m", 1e6), (" b", 1e9), (" k", 1e3)]:
        if s.endswith(suffix.strip()):
            try:
                return float(s[: -len(suffix.strip())].strip()) * mult
            except ValueError:
                pass
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── Block checks ──────────────────────────────────────────────────────────────

def _check_title(project_input: ProjectInput, **_) -> DeterministicResult | None:
    if not project_input.identity.title.strip():
        return DeterministicResult(
            check_name="title_missing",
            blocks=True,
            reason="Meeting title is missing or empty",
        )
    return None


def _check_date(project_input: ProjectInput, **_) -> DeterministicResult | None:
    d = project_input.identity.date.strip()
    if not d or not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return DeterministicResult(
            check_name="date_missing",
            blocks=True,
            reason=f"Meeting date missing or malformed: {d!r}",
        )
    return None


def _check_city_slug(project_input: ProjectInput, **_) -> DeterministicResult | None:
    if not project_input.identity.city_slug.strip():
        return DeterministicResult(
            check_name="city_slug_missing",
            blocks=True,
            reason="citySlug is missing — meeting identity cannot be established",
        )
    return None


def _check_priority_count(project_input: ProjectInput, **_) -> DeterministicResult | None:
    declared = project_input.identity.declared_priority_count
    actual = len(project_input.items)
    if declared is not None and declared != actual:
        return DeterministicResult(
            check_name="priority_count_mismatch",
            blocks=True,
            reason=f"Priority item count declared ({declared}) does not match actual ({actual})",
        )
    return None


def _check_stale_future_dates(project_input: ProjectInput, **_) -> DeterministicResult | None:
    meeting_dt = _meeting_date(project_input.identity)
    if meeting_dt is None:
        return None

    stale: list[str] = []
    for item in project_input.items:
        for field in _TEMPORAL_FIELDS:
            text = item.text_fields.get(field, "")
            if not text:
                continue
            for match in _DATE_RE.finditer(text):
                date_str = match.group(0)
                for fmt in ("%B %d, %Y", "%B %d %Y"):
                    try:
                        parsed = datetime.strptime(date_str.strip(), fmt).date()
                        break
                    except ValueError:
                        continue
                else:
                    continue
                ctx_start = max(0, match.start() - 100)
                ctx_end = min(len(text), match.end() + 100)
                context = text[ctx_start:ctx_end]
                if parsed < meeting_dt and _FUTURE_SIGNALS.search(context):
                    stale.append(f"{item.slug}.{field}: '{date_str}'")

    if stale:
        return DeterministicResult(
            check_name="stale_future_reference",
            blocks=True,
            reason=f"{len(stale)} stale future date reference(s): {'; '.join(stale[:3])}",
            details={"stale_references": stale},
            needs_llm_verification=True,
        )
    return None


def _check_identity_mismatch(project_input: ProjectInput, normalized: dict, **_) -> DeterministicResult | None:
    if not normalized:
        return None
    nm = normalized.get("meeting", {})
    ident = project_input.identity
    mismatches: list[str] = []

    n_date = (nm.get("date") or "").strip()
    if ident.date and n_date and ident.date != n_date:
        mismatches.append(f"date: expected '{n_date}' got '{ident.date}'")

    b_time = _normalize_time(ident.extra.get("time", ""))
    n_time = _normalize_time(nm.get("time") or "")
    if b_time and n_time and b_time != n_time:
        mismatches.append(f"time: expected '{nm.get('time', '')}' got '{ident.extra.get('time', '')}'")

    b_body = (ident.extra.get("body") or ident.title or "").strip().lower()
    n_body = (nm.get("body") or "").strip().lower()
    if b_body and n_body and b_body != n_body:
        mismatches.append(f"body: expected '{nm.get('body', '')}' got '{ident.extra.get('body', '')}'")

    if mismatches:
        return DeterministicResult(
            check_name="identity_mismatch",
            blocks=True,
            reason=f"Meeting identity mismatch: {'; '.join(mismatches)}",
            details={"mismatches": mismatches},
            needs_llm_verification=True,
        )
    return None


def _check_arithmetic(project_input: ProjectInput, **_) -> DeterministicResult | None:
    errors: list[str] = []
    for item in project_input.items:
        full_text = " ".join(item.text_fields.values())
        for m in _ARITHMETIC_RE.finditer(full_text):
            pct_str, from_str, to_str = m.group(1), m.group(2), m.group(3)
            try:
                pct = float(pct_str)
                from_val = _normalize_dollar(from_str)
                to_val = _normalize_dollar(to_str)
                if from_val > 0:
                    actual_pct = (to_val - from_val) / from_val * 100
                    if abs(actual_pct - pct) > 5:
                        errors.append(
                            f"{item.slug}: {pct}% from {from_str} to {to_str} "
                            f"(computed: {actual_pct:.1f}%)"
                        )
            except (ValueError, ZeroDivisionError):
                pass

    if errors:
        return DeterministicResult(
            check_name="arithmetic_error",
            blocks=True,
            reason=f"{len(errors)} arithmetic error(s) in quantitative fields",
            details={"errors": errors},
            needs_llm_verification=True,
        )
    return None


# ── Annotation checks (non-blocking) ─────────────────────────────────────────

def _check_haystaq_consistency(project_input: ProjectInput, haystaq: dict | None, **_) -> DeterministicResult | None:
    if not haystaq or not project_input.modeled_context:
        return None
    mc = project_input.modeled_context
    raw_voters = haystaq.get("voter_count_with_scores")
    if raw_voters is not None and mc.voter_count > 0:
        if abs(int(raw_voters) - mc.voter_count) > mc.voter_count * 0.1:
            return DeterministicResult(
                check_name="haystaq_voter_count",
                blocks=False,
                reason=f"Haystaq voter count mismatch: raw={raw_voters}, briefing={mc.voter_count}",
            )
    return None


def _check_required_fields(project_input: ProjectInput, **_) -> DeterministicResult | None:
    missing: list[str] = []
    for item in project_input.items:
        for f in _REQUIRED_DETAIL_FIELDS:
            if not item.text_fields.get(f, "").strip():
                missing.append(f"{item.slug}.{f}")
    if missing:
        return DeterministicResult(
            check_name="required_fields_missing",
            blocks=False,
            reason=f"{len(missing)} required field(s) missing: {', '.join(missing[:3])}",
            details={"missing": missing},
        )
    return None


def _check_banned_words(project_input: ProjectInput, **_) -> DeterministicResult | None:
    found: list[str] = []
    for item in project_input.items:
        for field, text in item.text_fields.items():
            for word in _BANNED_WORDS:
                if re.search(r"\b" + word + r"\b", text, re.IGNORECASE):
                    found.append(f"{item.slug}.{field}: '{word}'")
    if found:
        return DeterministicResult(
            check_name="banned_words",
            blocks=False,
            reason=f"Banned word(s) found: {', '.join(found[:5])}",
            details={"occurrences": found},
        )
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

_BLOCK_CHECKS = [
    _check_title,
    _check_date,
    _check_city_slug,
    _check_priority_count,
    _check_stale_future_dates,
    _check_identity_mismatch,
    _check_arithmetic,
]

_ANNOTATION_CHECKS = [
    _check_haystaq_consistency,
    _check_required_fields,
    _check_banned_words,
]


def run_deterministic_checks(
    project_input: ProjectInput,
    normalized: dict,
    haystaq: dict | None,
) -> list[DeterministicResult]:
    """Run all deterministic checks. Returns list of DeterministicResult.

    Block results are included in full; annotation results (blocks=False)
    appear in the trace and summary but do not affect routing.
    """
    kwargs = dict(
        project_input=project_input,
        normalized=normalized,
        haystaq=haystaq,
    )
    results: list[DeterministicResult] = []
    for check in _BLOCK_CHECKS + _ANNOTATION_CHECKS:
        result = check(**kwargs)
        if result is not None:
            results.append(result)
    return results
