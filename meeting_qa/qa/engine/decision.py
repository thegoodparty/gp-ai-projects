"""decision.py — Routing logic. The single authoritative source for Block / OK decisions.

Rules:
  Block — first triggered deterministic Block check, OR any blockable claim where
           Phase 2 judge returns a not-OK accuracy category.
  OK    — all deterministic checks pass and no blockable claim fails Phase 2.

To adjust routing (add a new Block rule, change the OK_ACCURACY set, etc.),
edit this file only. No other file needs to change.
"""
from __future__ import annotations

from qa.engine.models import ClaimAdjudication, DeterministicResult, RouteDecision

# Accuracy categories that are considered acceptable for delivery.
# Phase 2 verdict NOT in this set on a blockable claim → Block.
OK_ACCURACY: frozenset[str] = frozenset({
    "Accurate",
    "Directionally Consistent",
    "Extrapolating",
    "Modeled",
})


def route_decision(
    deterministic_results: list[DeterministicResult],
    adjudications: list[ClaimAdjudication],
    blockable_tiers: frozenset[str],
) -> RouteDecision:
    """Evaluate all results and return the final route decision.

    Deterministic Block rules are checked first (first match wins).
    Claim adjudication rules are checked only if all deterministic rules pass.
    """
    # 1. Deterministic Block checks
    # Soft-blocking results (needs_llm_verification=True) are skipped when the LLM
    # verdict is "Cleared"; they remain visible in the workbook as annotations.
    for result in deterministic_results:
        if result.blocks:
            if result.needs_llm_verification and result.llm_verdict == "Cleared":
                continue
            return RouteDecision(
                final_status="Block",
                reason_code=result.check_name,
                human_reason=result.reason,
                triggered_by="deterministic",
                deterministic_results=deterministic_results,
                all_adjudications=adjudications,
            )

    # 2. Claim adjudication Block checks
    # A claim triggers Block iff: blocking_candidate AND Phase 2 ran AND Phase 2 is not OK.
    # Claims where Phase 2 was not triggered (Phase 1 was OK) do not block.
    blocking_adjudications = [
        adj for adj in adjudications
        if adj.claim.blocking_candidate
        and adj.phase2 is not None
        and adj.phase2.accuracy_category not in OK_ACCURACY
    ]

    if blocking_adjudications:
        cats: dict[str, int] = {}
        for adj in blocking_adjudications:
            c = adj.phase2.accuracy_category  # type: ignore[union-attr]
            cats[c] = cats.get(c, 0) + 1
        detail = ", ".join(f"{n}× {c}" for c, n in cats.items())
        return RouteDecision(
            final_status="Block",
            reason_code="claim_adjudication",
            human_reason=(
                f"{len(blocking_adjudications)} blockable claim(s) not supported after full review: {detail}"
            ),
            triggered_by="claim_adjudication",
            blocking_adjudications=blocking_adjudications,
            all_adjudications=adjudications,
            deterministic_results=deterministic_results,
        )

    return RouteDecision(
        final_status="OK",
        reason_code="all_checks_passed",
        human_reason="No blocking issues found",
        triggered_by="",
        all_adjudications=adjudications,
        deterministic_results=deterministic_results,
    )
