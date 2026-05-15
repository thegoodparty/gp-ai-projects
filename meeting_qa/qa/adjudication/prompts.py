"""prompts.py — The three LLM prompts used by the QA engine.

  build_extraction_prompt   — Judge-based claim extraction (one call per agenda item)
  build_triage_prompt       — Judge 1: triage all non-skipped claims
  build_escalation_prompt   — Judge 2: deep review of blockable Phase 1 not-OK claims

Accuracy categories (shared by triage and escalation):
  Accurate / Directionally Consistent / Extrapolating / Unverifiable /
  Not in Source — Verified Elsewhere / Not in Source — Unresolved / Incorrect / Modeled
"""
from __future__ import annotations

from qa.engine.models import ClaimCandidate, DeterministicResult, EvidenceBundle
from qa.extraction.claim_types import ALL_CLAIM_TYPES

ACCURACY_CATEGORIES = [
    "Accurate",
    "Directionally Consistent",
    "Extrapolating",
    "Unverifiable",
    "Not in Source — Verified Elsewhere",
    "Not in Source — Unresolved",
    "Incorrect",
    "Modeled",
]

_ACCURACY_CATEGORY_GUIDE = """\
Category definitions (choose the LEAST SEVERE that is justified):
  Accurate                        — Claim is directly supported by the source passage
  Directionally Consistent        — Not verbatim, but a fair characterisation of the source
  Extrapolating                   — Goes beyond the source in a plausible but unverified direction
  Unverifiable                    — Cannot be confirmed or denied from the supplied evidence
  Not in Source — Verified Elsewhere — Not in this source, but cites a specific named external source
  Not in Source — Unresolved      — Not in source and cannot be verified; may be hallucinated
  Incorrect                       — Clearly contradicts or misrepresents the source
  Modeled                         — Based on modelled/statistical data (e.g. Haystaq voter scores)
"""

_CLAIM_TYPE_LIST = "\n".join(
    f"  {ct}" for ct in sorted(ALL_CLAIM_TYPES)
)


# ── Extraction prompt ─────────────────────────────────────────────────────────

def build_extraction_prompt(item_title: str, text_fields: dict[str, str]) -> str:
    """Build the extraction prompt for one agenda item.

    Returns a prompt whose response should be JSON:
      {"extracted_claims": [
          {
            "source_field": str,
            "claim_text": str,
            "claim_type": str,
            "why_material": str,
            "expected_source_type": "pdf" | "modeled_context" | "normalized",
            "should_skip": bool,
            "skip_reason": str
          }, ...
      ]}
    """
    labeled_text = "\n\n".join(
        f"[{field}]\n{text}" for field, text in text_fields.items() if text
    )

    return f"""You are extracting material factual claims from a section of an AI-generated governance briefing for an elected official.

AGENDA ITEM: {item_title}

CONTENT:
{labeled_text}

CLAIM TYPES:
{_CLAIM_TYPE_LIST}

INSTRUCTIONS:
Extract ONLY claims that could materially affect whether this briefing is safe to share with an elected official. Err on the side of fewer, more significant claims.

INCLUDE:
- Specific dates, times, scheduling facts
- Vote counts, approval/rejection facts, formal decisions
- Dollar amounts, percentages, fiscal figures
- Legal or agenda identifiers (ordinance numbers, resolution numbers, case IDs)
- Named officials, presenters, or titled roles stated as facts
- Constituent-priority claims presented as evidence (e.g. citing voter concern levels)
- Any other specific factual assertion whose inaccuracy could materially mislead the recipient

EXCLUDE (set should_skip: true):
- Coaching language and suggested questions (askThis, askThisInTheRoom, tryThis fields)
- Rhetorical framing and generic "why this matters" language
- Recommendations that contain no concrete factual assertion
- Background context that is interpretive rather than factual
- Stylistic language that would not affect safe sharing if slightly off

For each extracted claim:
  source_field          — exact field name from the content above (e.g. "whatIsHappening")
  claim_text            — the verbatim or near-verbatim claim sentence
  claim_type            — one of the claim types listed above
  why_material          — one sentence: why inaccuracy here would be a delivery risk
  expected_source_type  — "pdf" if you expect this in the agenda PDF, "modeled_context" if it
                          comes from constituent/Haystaq data, "normalized" otherwise
  should_skip           — false for included claims
  skip_reason           — empty string for included claims

Respond with ONLY valid JSON:
{{"extracted_claims": [...]}}"""


# ── Triage prompt (Judge 1) ───────────────────────────────────────────────────

def build_triage_prompt(claim: ClaimCandidate, bundle: EvidenceBundle) -> str:
    """Build the triage prompt for Judge 1.

    Response should be JSON:
      {"accuracy_category": str, "rationale": str, "flag_better_source": bool}
    """
    citation_block = _build_citation_block(
        bundle.citation_quote,
        bundle.citation_grounding_score,
        claim.source_field,
    )
    haystaq_block = _build_haystaq_block(bundle.modeled_context)

    if bundle.matched_local_span:
        source_text = bundle.matched_local_span.strip()
        if bundle.matched_local_span_source == "source_passage":
            source_label = "SOURCE PASSAGE (verbatim text the generator read from the PDF before writing this item — highest confidence)"
        elif bundle.matched_local_span_source == "citation_match":
            score_pct = f"{bundle.citation_grounding_score:.0%}"
            source_label = f"SOURCE PASSAGE (citation-anchored — located where the generator's declared quote matched the PDF at {score_pct} confidence)"
        else:
            source_label = "SOURCE PASSAGE (keyword-matched excerpt from the agenda PDF)"
    elif bundle.normalized_passage:
        source_text = bundle.normalized_passage.strip()
        source_label = "SOURCE PASSAGE (normalized JSON — no PDF excerpt found for this item; treat with lower confidence)"
    else:
        source_text = ""
        source_label = "SOURCE PASSAGE"

    return f"""You are auditing a single claim from an AI-generated governance briefing for an elected official.

CLAIM (from field: {claim.source_field}):
"{claim.claim_text}"

CLAIM TYPE: {claim.claim_type}
WEIGHT TIER: {claim.weight_tier}

{source_label}:
{source_text if source_text else "No source text available for this agenda item."}
{citation_block}{haystaq_block}
TASK: Assign exactly one accuracy category to this claim.

{_ACCURACY_CATEGORY_GUIDE}

Guiding principles:
- Your job is NOT to punish paraphrase or ordinary summarisation. Benign compression of
  source material is not a blocker.
- Choose the LEAST SEVERE category that is honestly justified.
- If the claim is a fair summary or reasonable inference from the source, prefer
  "Directionally Consistent" or "Extrapolating" over "Unverifiable".
- Use "Not in Source — Unresolved" only when a specific factual assertion cannot be
  confirmed from ANY of the supplied evidence and is suspicious, not merely absent.
- Reserve "Incorrect" for clear contradictions — not just gaps in the supplied evidence.
- If the claim references constituent priorities or voter concerns that match the
  Haystaq data above, use "Modeled".
- "Not in Source — Verified Elsewhere": use ONLY if the claim text itself cites a
  specific named source (e.g. "Per the 2024 Annual Report…").
- flag_better_source: true if a more specific source document should be cited.

Respond with ONLY valid JSON:
{{"accuracy_category": "<one of the 8 categories>", "rationale": "<1-2 sentences>", "flag_better_source": false}}"""


# ── Escalation prompt (Judge 2) ───────────────────────────────────────────────

def build_escalation_prompt(
    claim: ClaimCandidate,
    bundle: EvidenceBundle,
    phase1_verdict: str,
    phase1_rationale: str,
) -> str:
    """Build the escalation prompt for Judge 2.

    Judge 2 reviews the full evidence bundle and explicitly decides whether the
    Phase 1 concern survives broader context.

    Response should be JSON:
      {"accuracy_category": str, "rationale": str, "flag_better_source": bool}
    """
    citation_block = _build_citation_block(
        bundle.citation_quote,
        bundle.citation_grounding_score,
        claim.source_field,
    )
    haystaq_block = _build_haystaq_block(bundle.modeled_context)

    full_pdf = bundle.full_pdf_text.strip() if bundle.full_pdf_text else ""
    norm_source = bundle.normalized_passage.strip() if bundle.normalized_passage else ""

    if full_pdf:
        pdf_label = "FULL AGENDA PDF TEXT"
    else:
        pdf_label = "FULL AGENDA PDF TEXT (not available)"

    return f"""You are performing an escalation review of a claim that was flagged during initial triage.

CLAIM (from field: {claim.source_field}):
"{claim.claim_text}"

CLAIM TYPE: {claim.claim_type}

PHASE 1 TRIAGE VERDICT: {phase1_verdict}
PHASE 1 RATIONALE: {phase1_rationale}

{pdf_label}:
{full_pdf if full_pdf else "No PDF available."}

NORMALIZED SOURCE (what the pipeline extracted during briefing generation):
{norm_source if norm_source else "Not available."}
{citation_block}{haystaq_block}
TASK: Review the full evidence bundle and decide whether the Phase 1 concern survives.

{_ACCURACY_CATEGORY_GUIDE}

Your job as the escalation judge:
- Read the FULL evidence bundle, not just the initial triage excerpt.
- If the full passage resolves the Phase 1 concern (the claim is supported in broader
  context), assign a LESS SEVERE category than Phase 1 returned.
- Only confirm or escalate the Phase 1 verdict if the concern clearly survives full review.
- Apply the same "least severe justified label" principle as Phase 1.
- A claim that is a fair summary, reasonable inference, or clearly modelled on Haystaq
  data should NOT block delivery.

Respond with ONLY valid JSON:
{{"accuracy_category": "<one of the 8 categories>", "rationale": "<1-2 sentences>", "flag_better_source": false}}"""


# ── Deterministic verification prompt ────────────────────────────────────────

def build_det_verification_prompt(det_result: DeterministicResult, context: str) -> str:
    """Build a focused verification prompt for a soft-blocking deterministic result.

    The LLM decides whether the flagged concern holds up given source evidence.
    Response: {"verdict": "Confirmed" | "Cleared", "rationale": "<1-2 sentences>"}

    "Confirmed" → concern is real; route to Block as normal.
    "Cleared"   → concern is explained by context (e.g. stale date is in prior minutes,
                  arithmetic is within rounding tolerance, identity difference is formatting).
    """
    import json
    details_str = json.dumps(det_result.details, indent=2) if det_result.details else "none"
    return f"""You are reviewing a potential issue flagged by automated checks in an AI-generated governance briefing.

FLAGGED CHECK: {det_result.check_name}
REASON: {det_result.reason}
DETAILS:
{details_str}

SOURCE EVIDENCE:
{context if context else "No source evidence available."}

TASK: Decide whether this concern is valid given the source evidence.

  Confirmed — The concern is real: the flagged issue is not explained or resolved by the source.
              Example: a date reference is genuinely wrong for this agenda item.
  Cleared   — The concern is resolved: the source evidence shows the flagged content is correct
              or expected. Common cases:
              - A "stale" date appears in prior meeting minutes embedded in the agenda (not current content)
              - An identity difference is trivial formatting (e.g. "City Council" vs "City Council Meeting")
              - Arithmetic is within rounding tolerance given units used in the source

Choose "Cleared" only when you have specific evidence from the source that resolves the concern.
Default to "Confirmed" if the source does not address the flagged issue.

Respond with ONLY valid JSON:
{{"verdict": "Confirmed or Cleared", "rationale": "<1-2 sentences>"}}"""


# ── Shared helper blocks ──────────────────────────────────────────────────────

def _build_citation_block(quote: str, score: float, field: str) -> str:
    if not quote:
        return ""
    if score >= 0.90:
        confidence = "HIGH — found verbatim (or near-verbatim) in the source PDF"
    elif score >= 0.75:
        confidence = "MEDIUM — found with minor differences in the source PDF"
    elif score >= 0.60:
        confidence = f"LOW ({score:.0%} match) — may be paraphrased rather than verbatim"
    else:
        confidence = f"VERY LOW ({score:.0%} match) — likely synthesised rather than copied from PDF"

    if score >= 0.60:
        return f"""
PIPELINE CITATION (declared by the generator for the '{field}' field):
"{quote}"
Grounding confidence: {confidence}

Use this citation as a secondary anchor: if the claim accurately reflects this quote,
prefer "Accurate" or "Directionally Consistent" over "Unverifiable".
"""
    else:
        return f"""
PIPELINE CITATION (declared by generator — NOT reliably found in source PDF, {score:.0%} match):
"{quote}"
Treat this as context only. The quote may be synthesised. Do not use it to validate the claim.
"""


def _build_haystaq_block(modeled_context) -> str:
    if modeled_context is None or not modeled_context.available:
        return ""
    if not modeled_context.issues:
        return ""
    issue_lines = "\n".join(
        f"  - {iss.get('name', '')}: score {iss.get('score', '?')} "
        f"({iss.get('tier_label', iss.get('tier', '?'))})"
        for iss in modeled_context.issues
    )
    return f"""
CONSTITUENT DATA (Haystaq modelled voter scores — full list):
Voter count: {modeled_context.voter_count:,}
All issues by voter concern:
{issue_lines}

If the claim describes constituent priorities or voter concerns that correspond to
the above scores, use "Modeled" — not "Not in Source — Unresolved".
"""
