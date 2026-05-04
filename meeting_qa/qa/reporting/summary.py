"""summary.py — Human-readable QA summary markdown.

Design: only show what caused the routing decision and annotations worth reviewing.
A clean (OK) briefing produces a short report — banner + brief ID.
"""
from __future__ import annotations

from pathlib import Path

from qa.engine.models import ProjectInput, RouteDecision
from qa.adjudication.router import OK_ACCURACY

_TIER_BADGE = {
    "Block": "🔴 BLOCK — Do not deliver",
    "OK":    "🟢 OK — Clear for delivery",
}


def write_summary(
    route: RouteDecision,
    project_input: ProjectInput,
    path: Path,
) -> None:
    path.write_text(generate(route, project_input), encoding="utf-8")


def generate(route: RouteDecision, project_input: ProjectInput) -> str:
    ident = project_input.identity
    bid = project_input.document_id
    n_items = len(project_input.items)
    badge = _TIER_BADGE.get(route.final_status, route.final_status)

    lines: list[str] = [
        f"# QA Report: {ident.title or bid}",
        "",
        f"## {badge}",
        "",
    ]

    if route.final_status == "Block":
        lines.append(f"**Reason:** {route.human_reason}")
        lines.append("")

    lines += [
        f"**Brief ID:** `{bid}` | **Items:** {n_items}",
        "",
        "---",
        "",
    ]

    # ── Deterministic block details ───────────────────────────────────────────
    blocking_det = [r for r in route.deterministic_results if r.blocks]
    if blocking_det:
        lines += ["## Deterministic Block", ""]
        for r in blocking_det:
            lines.append(f"- `{r.check_name}`: {r.reason}")
        lines.append("")

    # ── Deterministic annotations (non-blocking) ──────────────────────────────
    annotation_det = [r for r in route.deterministic_results if not r.blocks]
    if annotation_det:
        lines += ["## Attention Items (non-blocking)", ""]
        for r in annotation_det:
            lines.append(f"- `{r.check_name}`: {r.reason}")
        lines.append("")

    # ── Blocking claims (Phase 2 not-OK) ─────────────────────────────────────
    if route.blocking_adjudications:
        lines += ["## Blocking Claims", ""]
        for adj in route.blocking_adjudications:
            claim = adj.claim
            p2 = adj.phase2
            lines.append(f"- **[{claim.claim_type}]** *{claim.item_title[:60]}*")
            lines.append(f"  - Phase 1: {adj.phase1.accuracy_category if adj.phase1 else '—'}"
                         f" → Phase 2: {p2.accuracy_category if p2 else '—'}")
            lines.append(f"  > {claim.claim_text[:120]}{'…' if len(claim.claim_text) > 120 else ''}")
        lines.append("")

    # ── Regular-weight concerns (Phase 1 not-OK, annotation only) ────────────
    regular_concerns = [
        adj for adj in route.all_adjudications
        if not adj.claim.blocking_candidate
        and not adj.claim.should_skip
        and adj.phase1 is not None
        and adj.phase1.accuracy_category not in OK_ACCURACY
    ]
    if regular_concerns:
        lines += ["## Accuracy Annotations (regular weight — not blocking)", ""]
        for adj in regular_concerns:
            claim = adj.claim
            cat = adj.phase1.accuracy_category
            lines.append(f"- **[{claim.claim_type}]** `{claim.source_field}` — *{claim.item_title[:50]}*")
            lines.append(f"  - Category: **{cat}**")
            lines.append(f"  > {claim.claim_text[:100]}{'…' if len(claim.claim_text) > 100 else ''}")
        lines.append("")

    return "\n".join(lines) + "\n"
