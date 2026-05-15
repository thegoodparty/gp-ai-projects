"""test_evidence.py — Evidence bundle assembly tests.

Tests that:
  - EvidenceBundle fields are populated from GroundingResult
  - Citation grounding uses the correct source_field
  - Haystaq modeled_context is passed through to the bundle
  - Fallback to empty strings when PDF unavailable
"""
from __future__ import annotations

import pytest

from qa.engine.models import ModeledContext
from qa.evidence.bundle_builder import build_bundle
from qa.evidence.grounding import GroundingResult, build_item_grounding, extract_relevant_span


# ── extract_relevant_span ─────────────────────────────────────────────────────

def test_span_extraction_finds_relevant_sentence():
    passage = (
        "The council discussed parking fees. "
        "Staff proposes a $2.5M allocation for road repairs. "
        "The vote will occur next Tuesday."
    )
    claim = "Staff proposes a $2.5M allocation."
    span = extract_relevant_span(claim, passage, n_sentences=1)
    assert "$2.5M" in span or "allocation" in span


def test_span_extraction_fallback_on_no_overlap():
    passage = "This sentence has no overlap. Neither does this one."
    claim = "completely unrelated xyz qrs"
    span = extract_relevant_span(claim, passage)
    assert len(span) > 0  # should return fallback, not crash


# ── build_bundle ──────────────────────────────────────────────────────────────

def test_bundle_uses_correct_citation_for_field(financial_claim):
    grounding = GroundingResult(
        pdf_available=True,
        pdf_passage="The council will vote on a $2.5M rezoning contract.",
        norm_passage="Staff recommends approval.",
        citation_grounding={
            "whatIsHappening": {"quote": "council will vote on $2.5M", "score": 0.92, "span": "..."},
            "whatDecision": {"quote": "Ordinance 2025-14", "score": 0.85, "span": "..."},
        },
    )
    bundle = build_bundle(financial_claim, grounding, modeled_context=None)
    # financial_claim.source_field == "whatIsHappening"
    assert bundle.citation_quote == "council will vote on $2.5M"
    assert bundle.citation_grounding_score == 0.92


def test_bundle_uses_different_field_citation(financial_claim):
    financial_claim.source_field = "whatDecision"
    grounding = GroundingResult(
        pdf_available=True,
        pdf_passage="Ordinance 2025-14 passed.",
        norm_passage="",
        citation_grounding={
            "whatDecision": {"quote": "Ordinance 2025-14", "score": 0.88, "span": "Ordinance 2025-14"},
        },
    )
    bundle = build_bundle(financial_claim, grounding, modeled_context=None)
    assert bundle.citation_quote == "Ordinance 2025-14"
    assert bundle.citation_grounding_score == 0.88


def test_bundle_no_citation_when_field_absent(background_claim):
    grounding = GroundingResult(
        pdf_available=True,
        pdf_passage="Some PDF text.",
        norm_passage="Normalized text.",
        citation_grounding={},  # no citations
    )
    bundle = build_bundle(background_claim, grounding, modeled_context=None)
    assert bundle.citation_quote == ""
    assert bundle.citation_grounding_score == 0.0


def test_bundle_passes_modeled_context(financial_claim, sample_modeled_context):
    grounding = GroundingResult(
        pdf_available=False,
        pdf_passage="",
        norm_passage="",
    )
    bundle = build_bundle(financial_claim, grounding, modeled_context=sample_modeled_context)
    assert bundle.modeled_context is sample_modeled_context
    assert bundle.modeled_context.voter_count == 12000


def test_bundle_empty_when_no_pdf(financial_claim):
    grounding = GroundingResult(
        pdf_available=False,
        pdf_passage="",
        norm_passage="Staff recommends approval.",
    )
    bundle = build_bundle(financial_claim, grounding, modeled_context=None)
    assert bundle.matched_local_span == ""
    assert bundle.full_item_passage == ""
    assert bundle.normalized_passage == "Staff recommends approval."


# ── build_item_grounding ──────────────────────────────────────────────────────

def test_build_item_grounding_no_pdf(sample_items):
    item = sample_items[0]
    result = build_item_grounding(item, norm_item=None, pdf_text=None)
    assert result.pdf_available is False
    assert result.pdf_passage == ""
    assert result.norm_passage == ""
    # Citations present but with score 0.0 (no PDF to match against)
    assert "whatIsHappening" in result.citation_grounding
    assert result.citation_grounding["whatIsHappening"]["score"] == 0.0


def test_build_item_grounding_with_norm(sample_items):
    item = sample_items[0]
    norm_item = {
        "title": "Downtown Rezoning Proposal",
        "description": "Staff recommends approval of rezoning ordinance.",
        "staff_recommendation": "Approve.",
        "fiscal_amounts": [],
    }
    result = build_item_grounding(item, norm_item=norm_item, pdf_text=None)
    assert "approval of rezoning" in result.norm_passage
    assert "Approve" in result.norm_passage
