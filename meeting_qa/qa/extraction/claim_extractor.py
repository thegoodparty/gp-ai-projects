"""claim_extractor.py — LLM-based claim extraction.

Extracts only material factual claims from an agenda item's text fields.
Each extracted claim is classified by type; weight_tier and blocking_candidate
are derived from the type via claim_types.py (the modularity hook).

The LLM decides WHAT to extract; claim_types.py decides HOW it routes.
"""
from __future__ import annotations

from qa.adjudication.judge_runner import dispatch
from qa.adjudication.prompts import build_extraction_prompt
from qa.engine.config import JudgeConfig, QARunConfig
from qa.engine.models import ClaimCandidate, ItemContext, ModeledContext
from qa.extraction.claim_types import ALL_CLAIM_TYPES, is_blocking_candidate, weight_tier
from qa.evidence.grounding import GroundingResult


def extract_claims(
    item: ItemContext,
    grounding: GroundingResult,
    modeled_context: ModeledContext | None,
    cfg: QARunConfig,
) -> list[ClaimCandidate]:
    """Extract material claims from one agenda item using the triage judge.

    Returns an empty list if no judge is configured or the call fails.
    Skipped claims (should_skip=True) are included in the list so they appear
    in the workbook with their skip_reason.
    """
    judge = cfg.triage_judge
    if not judge:
        return []

    prompt = build_extraction_prompt(item.title, item.text_fields)
    if not prompt:
        print(f"  [extractor] {item.slug}: no content — skipping extraction")
        return []
    try:
        result = dispatch(
            judge.provider, judge.model, judge.api_key, prompt,
            max_tokens=2048,
        )
    except Exception as e:
        print(f"  [extractor] {item.slug}: extraction failed — {e}")
        return []

    raw_claims = result.get("extracted_claims", [])
    if not isinstance(raw_claims, list):
        print(f"  [extractor] {item.slug}: unexpected extraction response format")
        return []

    valid_fields = set(item.text_fields.keys())
    candidates: list[ClaimCandidate] = []
    for rc in raw_claims:
        if not isinstance(rc, dict):
            continue
        source_field = rc.get("source_field", "")
        if source_field and valid_fields and source_field not in valid_fields:
            continue
        claim_type = rc.get("claim_type", "background_context")
        if claim_type not in ALL_CLAIM_TYPES:
            claim_type = "background_context"

        tier = weight_tier(claim_type)
        blocking = is_blocking_candidate(claim_type, cfg.blockable_tiers)

        candidates.append(ClaimCandidate(
            item_slug=item.slug,
            item_title=item.title,
            source_field=source_field,
            claim_text=rc.get("claim_text", "").strip(),
            claim_type=claim_type,
            weight_tier=tier,
            blocking_candidate=blocking,
            why_material=rc.get("why_material", ""),
            expected_source_type=rc.get("expected_source_type", "pdf"),
            should_skip=bool(rc.get("should_skip", False)),
            skip_reason=rc.get("skip_reason", ""),
        ))

    n_skip = sum(1 for c in candidates if c.should_skip)
    print(f"  [extractor] {item.slug}: {len(candidates)} claim(s) extracted ({n_skip} skipped by extractor)")
    return candidates
