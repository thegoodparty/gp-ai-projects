"""conftest.py — Shared fixtures for QA pipeline tests."""
from __future__ import annotations

import pytest

from qa.engine.models import (
    ClaimCandidate,
    DeterministicResult,
    EvidenceBundle,
    IdentityContext,
    ItemContext,
    ModeledContext,
    ProjectInput,
)
from qa.extraction.claim_types import BLOCKABLE_TIERS


@pytest.fixture
def sample_identity() -> IdentityContext:
    return IdentityContext(
        title="Chapel Hill Town Council Meeting",
        date="2025-06-15",
        city_slug="chapel-hill-NC",
        declared_priority_count=2,
        extra={"time": "7:00 PM", "body": "Town Council"},
    )


@pytest.fixture
def sample_modeled_context() -> ModeledContext:
    return ModeledContext(
        available=True,
        voter_count=12000,
        issues=[
            {"name": "Housing", "score": 78, "tier": "High"},
            {"name": "Public Safety", "score": 65, "tier": "High"},
        ],
        raw={"available": True},
    )


@pytest.fixture
def sample_items() -> list[ItemContext]:
    return [
        ItemContext(
            slug="rezoning-downtown",
            title="Downtown Rezoning Proposal",
            text_fields={
                "whatIsHappening": "The council will vote on a $2.5M rezoning contract.",
                "whatDecision": "Approval of Ordinance 2025-14.",
                "whyItMatters": "This affects 500 housing units in the downtown core.",
                "askThis": "Have you reviewed the traffic impact study?",
            },
            source_citations={
                "whatIsHappening": "The council will vote on a $2.5M rezoning contract",
            },
        ),
        ItemContext(
            slug="budget-amendment",
            title="FY2025 Budget Amendment",
            text_fields={
                "whatIsHappening": "Staff proposes a 15% increase from $10M to $11.5M.",
                "whatDecision": "Vote on budget amendment resolution.",
            },
            source_citations={},
        ),
    ]


@pytest.fixture
def sample_project_input(sample_identity, sample_items, sample_modeled_context) -> ProjectInput:
    return ProjectInput(
        document_id="chapel-hill-NC_2025-06-15",
        document_type="meeting_briefing",
        identity=sample_identity,
        items=sample_items,
        modeled_context=sample_modeled_context,
        raw={},
    )


@pytest.fixture
def financial_claim(sample_items) -> ClaimCandidate:
    return ClaimCandidate(
        item_slug="rezoning-downtown",
        item_title="Downtown Rezoning Proposal",
        source_field="whatIsHappening",
        claim_text="The council will vote on a $2.5M rezoning contract.",
        claim_type="financial_fact",
        weight_tier="blockable",
        blocking_candidate=True,
        why_material="Dollar amount — incorrect figure would directly mislead the EO",
        expected_source_type="pdf",
        index=0,
    )


@pytest.fixture
def background_claim(sample_items) -> ClaimCandidate:
    return ClaimCandidate(
        item_slug="rezoning-downtown",
        item_title="Downtown Rezoning Proposal",
        source_field="whyItMatters",
        claim_text="This affects 500 housing units in the downtown core.",
        claim_type="background_context",
        weight_tier="regular",
        blocking_candidate=False,
        why_material="Provides context but not a material delivery risk",
        expected_source_type="pdf",
        index=1,
    )


@pytest.fixture
def sample_bundle() -> EvidenceBundle:
    return EvidenceBundle(
        matched_local_span="The council will vote on a $2.5M rezoning contract for the downtown area.",
        full_item_passage="Downtown Rezoning Proposal. The council will vote on a $2.5M rezoning contract. "
                          "Ordinance 2025-14 was drafted by the planning department.",
        normalized_passage="Staff recommends approval of rezoning. Estimated cost: $2.5M.",
        citation_quote="The council will vote on a $2.5M rezoning contract",
        citation_grounding_score=0.92,
    )


@pytest.fixture
def ok_deterministic() -> list[DeterministicResult]:
    return []


@pytest.fixture
def blocking_deterministic() -> list[DeterministicResult]:
    return [
        DeterministicResult(
            check_name="date_missing",
            blocks=True,
            reason="Meeting date missing or malformed: ''",
        )
    ]
