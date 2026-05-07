"""bundle_builder.py — Assembles EvidenceBundle per claim from item grounding data."""
from __future__ import annotations

from qa.engine.models import ClaimCandidate, EvidenceBundle, ModeledContext
from qa.evidence.grounding import GroundingResult, extract_relevant_span

_CITATION_SPAN_THRESHOLD = 0.75


def build_bundle(
    claim: ClaimCandidate,
    grounding: GroundingResult,
    modeled_context: ModeledContext | None,
    full_pdf_text: str = "",
) -> EvidenceBundle:
    """Build the structured evidence bundle for a single claim.

    Judge 1 (triage) uses matched_local_span as its primary source window.
    Judge 2 (escalation) uses full_pdf_text for the complete agenda document.
    Both judges receive citation_quote/score and modeled_context.
    full_item_passage (2000-char keyword window) is retained for diagnostics.

    matched_local_span priority:
      1. source_passage — verbatim text the generator declared it used (score 1.0).
      2. Citation span — generator's declared quote fuzzy-matched to PDF >= 0.75.
      3. Keyword span — extract_relevant_span over the item's PDF window.
      4. Empty string — no PDF available; prompt falls back to normalized_passage.
    """
    citation_data = grounding.citation_grounding.get(claim.source_field, {})
    citation_score = float(citation_data.get("score", 0.0))
    citation_span = citation_data.get("span", "")

    if grounding.source_passage:
        matched_span = grounding.source_passage
        span_source = "source_passage"
        citation_score = 1.0
    elif citation_score >= _CITATION_SPAN_THRESHOLD and citation_span:
        matched_span = citation_span
        span_source = "citation_match"
    elif grounding.pdf_passage:
        matched_span = extract_relevant_span(claim.claim_text, grounding.pdf_passage)
        span_source = "keyword_match"
    else:
        matched_span = ""
        span_source = ""

    return EvidenceBundle(
        matched_local_span=matched_span,
        matched_local_span_source=span_source,
        full_item_passage=grounding.pdf_passage,
        normalized_passage=grounding.norm_passage,
        citation_quote=citation_data.get("quote", ""),
        citation_grounding_score=citation_score,
        modeled_context=modeled_context,
        full_pdf_text=full_pdf_text,
    )
