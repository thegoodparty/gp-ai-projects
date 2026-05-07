"""test_extraction.py — Claim extraction and claim type tests.

Tests that:
  - weight_tier and is_blocking_candidate return correct values per type
  - The extractor correctly maps LLM output to ClaimCandidate objects
  - Unknown claim types default to 'regular' (not blocking)
  - Haystaq-backed constituent claims are regular weight (not blockable)
  - Advisory fields (askThis etc.) can be marked should_skip by the extractor
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from qa.extraction.claim_types import (
    BACKGROUND_CONTEXT,
    BLOCKABLE_TIERS,
    CONSTITUENT_PRIORITY,
    FINANCIAL_FACT,
    MEETING_LOGISTICS,
    NAMED_PERSON_OR_ROLE,
    VOTE_OR_DECISION_FACT,
    is_blocking_candidate,
    weight_tier,
)


# ── claim_types ───────────────────────────────────────────────────────────────

def test_financial_fact_is_blockable():
    assert weight_tier(FINANCIAL_FACT) == "blockable"
    assert is_blocking_candidate(FINANCIAL_FACT) is True


def test_vote_fact_is_blockable():
    assert is_blocking_candidate(VOTE_OR_DECISION_FACT) is True


def test_constituent_priority_is_regular():
    assert weight_tier(CONSTITUENT_PRIORITY) == "regular"
    assert is_blocking_candidate(CONSTITUENT_PRIORITY) is False


def test_background_context_is_regular():
    assert is_blocking_candidate(BACKGROUND_CONTEXT) is False


def test_unknown_claim_type_defaults_to_regular():
    assert weight_tier("made_up_type") == "regular"
    assert is_blocking_candidate("made_up_type") is False


def test_named_person_is_blockable():
    assert is_blocking_candidate(NAMED_PERSON_OR_ROLE) is True


def test_meeting_logistics_is_blockable():
    assert is_blocking_candidate(MEETING_LOGISTICS) is True


# ── claim_extractor ───────────────────────────────────────────────────────────

def test_extractor_maps_llm_output_to_candidates(sample_items, sample_modeled_context):
    from qa.engine.config import JudgeConfig, QARunConfig
    from qa.extraction.claim_extractor import extract_claims
    from qa.evidence.grounding import GroundingResult

    grounding = GroundingResult(pdf_available=False, pdf_passage="", norm_passage="")
    cfg = QARunConfig(
        judges=[JudgeConfig("claude", "anthropic", "claude-sonnet-4-6", "fake-key")]
    )

    fake_response = {
        "extracted_claims": [
            {
                "source_field": "whatIsHappening",
                "claim_text": "The council will vote on a $2.5M rezoning contract.",
                "claim_type": "financial_fact",
                "why_material": "Dollar amount could mislead",
                "expected_source_type": "pdf",
                "should_skip": False,
                "skip_reason": "",
            },
            {
                "source_field": "askThis",
                "claim_text": "Have you reviewed the traffic impact study?",
                "claim_type": "background_context",
                "why_material": "",
                "expected_source_type": "pdf",
                "should_skip": True,
                "skip_reason": "Coaching question — not a factual claim",
            },
        ]
    }

    with patch("qa.extraction.claim_extractor.dispatch", return_value=fake_response):
        claims = extract_claims(sample_items[0], grounding, sample_modeled_context, cfg)

    assert len(claims) == 2

    financial = claims[0]
    assert financial.claim_type == "financial_fact"
    assert financial.weight_tier == "blockable"
    assert financial.blocking_candidate is True
    assert financial.should_skip is False

    advisory = claims[1]
    assert advisory.should_skip is True
    assert advisory.skip_reason == "Coaching question — not a factual claim"
    assert advisory.blocking_candidate is False  # background_context is regular


def test_extractor_handles_unknown_claim_type(sample_items):
    from qa.engine.config import JudgeConfig, QARunConfig
    from qa.extraction.claim_extractor import extract_claims
    from qa.evidence.grounding import GroundingResult

    grounding = GroundingResult(pdf_available=False, pdf_passage="", norm_passage="")
    cfg = QARunConfig(
        judges=[JudgeConfig("claude", "anthropic", "claude-sonnet-4-6", "fake-key")]
    )

    fake_response = {
        "extracted_claims": [
            {
                "source_field": "whatIsHappening",
                "claim_text": "Some claim.",
                "claim_type": "completely_unknown_type",
                "why_material": "reason",
                "expected_source_type": "pdf",
                "should_skip": False,
                "skip_reason": "",
            }
        ]
    }

    with patch("qa.extraction.claim_extractor.dispatch", return_value=fake_response):
        claims = extract_claims(sample_items[0], grounding, None, cfg)

    assert len(claims) == 1
    # Unknown type defaults to background_context
    assert claims[0].claim_type == "background_context"
    assert claims[0].blocking_candidate is False


def test_extractor_returns_empty_on_failure(sample_items):
    from qa.engine.config import JudgeConfig, QARunConfig
    from qa.extraction.claim_extractor import extract_claims
    from qa.evidence.grounding import GroundingResult

    grounding = GroundingResult(pdf_available=False, pdf_passage="", norm_passage="")
    cfg = QARunConfig(
        judges=[JudgeConfig("claude", "anthropic", "claude-sonnet-4-6", "fake-key")]
    )

    with patch("qa.extraction.claim_extractor.dispatch", side_effect=Exception("API error")):
        claims = extract_claims(sample_items[0], grounding, None, cfg)

    assert claims == []


def test_haystaq_constituent_claim_not_blockable():
    """Constituent-priority claims backed by Haystaq should never Block."""
    assert is_blocking_candidate(CONSTITUENT_PRIORITY, BLOCKABLE_TIERS) is False
