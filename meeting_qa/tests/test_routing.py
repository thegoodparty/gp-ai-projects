"""test_routing.py — Routing logic tests.

Tests that:
  - Deterministic Block rules trigger correctly
  - Blockable Phase 2 not-OK → Block
  - Regular-weight not-OK → OK (annotation only)
  - Blockable Phase 2 OK → OK even if Phase 1 was not-OK
  - Missing Phase 2 on blockable claim → OK (Phase 2 must run and fail to block)
"""
from __future__ import annotations

import pytest

from qa.engine.decision import OK_ACCURACY, route_decision
from qa.engine.models import (
    ClaimAdjudication,
    ClaimCandidate,
    DeterministicResult,
    EvidenceBundle,
    JudgeDecision,
)
from qa.extraction.claim_types import BLOCKABLE_TIERS


def _adj(claim, phase1_cat=None, phase2_cat=None) -> ClaimAdjudication:
    bundle = EvidenceBundle("span", "full", "norm")
    p1 = JudgeDecision("judge1", phase1_cat, "rationale") if phase1_cat else None
    p2 = JudgeDecision("judge2", phase2_cat, "rationale") if phase2_cat else None
    return ClaimAdjudication(claim=claim, bundle=bundle, phase1=p1, phase2=p2)


# ── Deterministic Block ───────────────────────────────────────────────────────

def test_deterministic_block_wins(ok_deterministic, blocking_deterministic, financial_claim):
    route = route_decision(blocking_deterministic, [], BLOCKABLE_TIERS)
    assert route.final_status == "Block"
    assert route.triggered_by == "deterministic"


def test_deterministic_ok_passes(ok_deterministic, financial_claim):
    adj = _adj(financial_claim, "Accurate", None)
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "OK"


# ── Claim adjudication routing ────────────────────────────────────────────────

def test_blockable_phase2_notok_triggers_block(financial_claim):
    adj = _adj(financial_claim, phase1_cat="Not in Source — Unresolved", phase2_cat="Not in Source — Unresolved")
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "Block"
    assert route.triggered_by == "claim_adjudication"
    assert adj in route.blocking_adjudications


def test_blockable_phase2_ok_does_not_block(financial_claim):
    """Phase 1 flagged, but Phase 2 resolved it — should be OK."""
    adj = _adj(financial_claim, phase1_cat="Not in Source — Unresolved", phase2_cat="Directionally Consistent")
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "OK"


def test_blockable_phase2_not_run_does_not_block(financial_claim):
    """Phase 1 flagged but Phase 2 was not triggered (no phase2=None) — should not Block."""
    adj = _adj(financial_claim, phase1_cat="Unverifiable", phase2_cat=None)
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "OK"


def test_regular_weight_notok_does_not_block(background_claim):
    """Regular-weight claim with bad Phase 1 verdict should never Block."""
    adj = _adj(background_claim, phase1_cat="Not in Source — Unresolved", phase2_cat=None)
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "OK"


def test_regular_weight_phase2_notok_does_not_block(background_claim):
    """Even if Phase 2 runs on a regular-weight claim, it cannot Block."""
    adj = _adj(background_claim, phase1_cat="Incorrect", phase2_cat="Incorrect")
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "OK"


# ── OK_ACCURACY coverage ──────────────────────────────────────────────────────

@pytest.mark.parametrize("cat", list(OK_ACCURACY))
def test_ok_accuracy_categories_do_not_block(financial_claim, cat):
    adj = _adj(financial_claim, phase1_cat=cat, phase2_cat=cat)
    route = route_decision([], [adj], BLOCKABLE_TIERS)
    assert route.final_status == "OK"


# ── Deterministic checks ──────────────────────────────────────────────────────

def test_run_deterministic_title_missing(sample_project_input):
    from qa.checks.deterministic import run_deterministic_checks
    sample_project_input.identity.title = ""
    results = run_deterministic_checks(sample_project_input, {}, None)
    blocks = [r for r in results if r.blocks]
    assert any(r.check_name == "title_missing" for r in blocks)


def test_run_deterministic_date_malformed(sample_project_input):
    from qa.checks.deterministic import run_deterministic_checks
    sample_project_input.identity.date = "not-a-date"
    results = run_deterministic_checks(sample_project_input, {}, None)
    blocks = [r for r in results if r.blocks]
    assert any(r.check_name == "date_missing" for r in blocks)


def test_run_deterministic_priority_count_mismatch(sample_project_input):
    from qa.checks.deterministic import run_deterministic_checks
    sample_project_input.identity.declared_priority_count = 99
    results = run_deterministic_checks(sample_project_input, {}, None)
    blocks = [r for r in results if r.blocks]
    assert any(r.check_name == "priority_count_mismatch" for r in blocks)


def test_run_deterministic_clean_briefing_passes(sample_project_input):
    from qa.checks.deterministic import run_deterministic_checks
    results = run_deterministic_checks(sample_project_input, {}, None)
    blocks = [r for r in results if r.blocks]
    assert len(blocks) == 0
