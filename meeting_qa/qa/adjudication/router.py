"""router.py — Phase 1 / Phase 2 adjudication orchestration.

Phase 1 — triage judge, parallel workers:
  All non-skipped claims adjudicated by the triage judge.

Phase 2 — escalation judge, sequential:
  Only blockable claims where Phase 1 returned a not-OK verdict.
  Uses the full agenda PDF text for broader context.
  Explicitly asks the escalation judge to overturn if broader context resolves the concern.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

from tqdm import tqdm

from qa.adjudication.judge_runner import dispatch
from qa.adjudication.prompts import (
    ACCURACY_CATEGORIES,
    build_escalation_prompt,
    build_triage_prompt,
)
from qa.engine.config import JudgeConfig, QARunConfig
from qa.engine.models import ClaimAdjudication, ClaimCandidate, EvidenceBundle, JudgeDecision

# Accuracy categories that are considered acceptable — Phase 2 not in this set → Block-eligible
OK_ACCURACY: frozenset[str] = frozenset({
    "Accurate",
    "Directionally Consistent",
    "Extrapolating",
    "Modeled",
})


def _validate_category(val: str) -> str:
    if val in ACCURACY_CATEGORIES:
        return val
    best = max(
        ACCURACY_CATEGORIES,
        key=lambda c: SequenceMatcher(None, val.lower(), c.lower()).ratio(),
    )
    return best if SequenceMatcher(None, val.lower(), best.lower()).ratio() > 0.7 else "Unverifiable"


def _call_judge(
    judge: JudgeConfig,
    prompt: str,
    max_tokens: int = 512,
) -> JudgeDecision | None:
    try:
        result = dispatch(judge.provider, judge.model, judge.api_key, prompt, max_tokens)
        cat = _validate_category(result.get("accuracy_category", ""))
        return JudgeDecision(
            judge_name=judge.name,
            accuracy_category=cat,
            rationale=result.get("rationale", ""),
            flag_better_source=bool(result.get("flag_better_source", False)),
        )
    except Exception as e:
        msg = str(e)
        if "503" in msg or "UNAVAILABLE" in msg or "overloaded" in msg.lower():
            return None
        raise


def run_adjudication(
    claims: list[ClaimCandidate],
    bundles: dict[int, EvidenceBundle],
    cfg: QARunConfig,
) -> list[ClaimAdjudication]:
    """Run Phase 1 and Phase 2 adjudication. Returns one ClaimAdjudication per claim."""
    triage = cfg.triage_judge
    escalation = cfg.escalation_judge

    # Initialise adjudications (skipped claims have no phase decisions)
    adjudications: list[ClaimAdjudication] = [
        ClaimAdjudication(claim=c, bundle=bundles.get(c.index, EvidenceBundle("", "", "")))
        for c in claims
    ]

    if not triage:
        print("  [router] No judge configured — skipping adjudication")
        return adjudications

    eligible = [i for i, c in enumerate(claims) if not c.should_skip]
    n_skipped = len(claims) - len(eligible)
    if n_skipped:
        print(f"  [router] {n_skipped}/{len(claims)} claims skipped by extractor")
    print(f"  [router] Phase 1: {triage.name} ({triage.model}) — {len(eligible)} claim(s)")

    # ── Phase 1: parallel triage ──────────────────────────────────────────────
    if cfg.run_phase1:
        def _phase1_task(i: int) -> tuple[int, JudgeDecision | None]:
            claim = claims[i]
            bundle = bundles.get(claim.index, EvidenceBundle("", "", ""))
            prompt = build_triage_prompt(claim, bundle)
            return i, _call_judge(triage, prompt, max_tokens=cfg.phase1_max_tokens)

        with ThreadPoolExecutor(max_workers=cfg.phase1_max_workers) as ex:
            futures = {ex.submit(_phase1_task, i): i for i in eligible}
            for future in tqdm(
                as_completed(futures),
                total=len(eligible),
                desc=f"  Claims [Phase 1: {triage.name}]",
                unit="claim",
            ):
                i = futures[future]
                try:
                    _, decision = future.result()
                    adjudications[i].phase1 = decision
                except Exception as e:
                    print(f"  [router] Phase 1 failed (claim {i}): {e}")

    # ── Phase 2: sequential escalation ───────────────────────────────────────
    if cfg.run_phase2 and escalation:
        phase2_candidates = [
            i for i in eligible
            if claims[i].blocking_candidate
            and adjudications[i].phase1 is not None
            and adjudications[i].phase1.accuracy_category not in OK_ACCURACY
        ]

        if phase2_candidates:
            print(
                f"  [router] Phase 2: {escalation.name} ({escalation.model})"
                f" — {len(phase2_candidates)} blockable not-OK claim(s)"
            )
            for i in tqdm(
                phase2_candidates,
                desc=f"  Claims [Phase 2: {escalation.name}]",
                unit="claim",
            ):
                claim = claims[i]
                bundle = bundles.get(claim.index, EvidenceBundle("", "", ""))
                p1 = adjudications[i].phase1
                prompt = build_escalation_prompt(
                    claim, bundle,
                    phase1_verdict=p1.accuracy_category,
                    phase1_rationale=p1.rationale,
                )
                decision = _call_judge(escalation, prompt, max_tokens=cfg.phase2_max_tokens)
                adjudications[i].phase2 = decision
        else:
            print("  [router] Phase 2: no blockable not-OK claims — skipped")

    return adjudications
