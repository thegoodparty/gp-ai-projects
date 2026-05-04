"""claim_types.py — Claim type taxonomy and weight tier registry.

Weight tiers (currently two):
  blockable — Phase 2 not-OK verdict on a blockable claim triggers Block
  regular   — not-OK verdict appears as a workbook annotation; never triggers Block

To add a new tier or reclassify a claim type, edit CLAIM_TYPE_WEIGHT_TIER and/or
BLOCKABLE_TIERS. No other file needs to change.
"""
from __future__ import annotations

# ── Claim type constants ──────────────────────────────────────────────────────

FINANCIAL_FACT        = "financial_fact"
VOTE_OR_DECISION_FACT = "vote_or_decision_fact"
IDENTITY_FACT         = "identity_fact"
LEGAL_IDENTIFIER      = "legal_identifier"
NAMED_PERSON_OR_ROLE  = "named_person_or_role"
MEETING_LOGISTICS     = "meeting_logistics"
CONSTITUENT_PRIORITY  = "constituent_priority"
BACKGROUND_CONTEXT    = "background_context"

ALL_CLAIM_TYPES: frozenset[str] = frozenset({
    FINANCIAL_FACT,
    VOTE_OR_DECISION_FACT,
    IDENTITY_FACT,
    LEGAL_IDENTIFIER,
    NAMED_PERSON_OR_ROLE,
    MEETING_LOGISTICS,
    CONSTITUENT_PRIORITY,
    BACKGROUND_CONTEXT,
})

# ── Weight tier registry ──────────────────────────────────────────────────────
# This is the sole place where claim type → tier is defined.

CLAIM_TYPE_WEIGHT_TIER: dict[str, str] = {
    FINANCIAL_FACT:        "blockable",
    VOTE_OR_DECISION_FACT: "blockable",
    IDENTITY_FACT:         "blockable",
    LEGAL_IDENTIFIER:      "blockable",
    NAMED_PERSON_OR_ROLE:  "blockable",
    MEETING_LOGISTICS:     "blockable",
    CONSTITUENT_PRIORITY:  "regular",
    BACKGROUND_CONTEXT:    "regular",
}

BLOCKABLE_TIERS: frozenset[str] = frozenset({"blockable"})


def weight_tier(claim_type: str) -> str:
    """Return the weight tier for a claim type. Unknown types default to 'regular'."""
    return CLAIM_TYPE_WEIGHT_TIER.get(claim_type, "regular")


def is_blocking_candidate(
    claim_type: str,
    blockable_tiers: frozenset[str] = BLOCKABLE_TIERS,
) -> bool:
    return weight_tier(claim_type) in blockable_tiers
