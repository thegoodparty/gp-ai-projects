"""runner.py — Per-document QA orchestrator.

Trace: input → grounding → extraction → bundles → adjudication → routing → reporting.
Each stage is skipped cleanly if disabled in QARunConfig.

Entry point:
    from qa.engine.runner import run_qa
    result = run_qa(project_input, spec, normalized, haystaq, pdf_bytes, cfg, output_dir)
"""
from __future__ import annotations

from pathlib import Path

from qa.checks.deterministic import run_deterministic_checks
from qa.engine.config import QARunConfig
from qa.engine.decision import route_decision
from qa.engine.models import ClaimAdjudication, DeterministicResult, EvidenceBundle, ProjectInput
from qa.evidence.bundle_builder import build_bundle
from qa.evidence.grounding import (
    build_item_grounding,
    extract_pdf_text,
    find_norm_item,
)
from qa.engine.models import ModeledContext
from qa.inputs.project_spec import QAProjectSpec


def _build_det_context(
    det_result: DeterministicResult,
    item_groundings: dict,
    project_input: ProjectInput,
) -> str:
    """Build evidence context string for LLM verification of a soft-blocking det result."""
    parts: list[str] = []

    if det_result.check_name == "identity_mismatch":
        ident = project_input.identity
        parts.append(
            f"MEETING IDENTITY IN BRIEFING:\n"
            f"  Title: {ident.title}\n"
            f"  Date: {ident.date}\n"
            f"  Time: {ident.extra.get('time', '')}\n"
            f"  Body: {ident.extra.get('body', '')}"
        )

    # Determine which item slugs are relevant
    affected_slugs: set[str] = set()
    if det_result.check_name == "stale_future_reference":
        for ref in det_result.details.get("stale_references", []):
            slug = ref.split(".")[0]
            affected_slugs.add(slug)
    elif det_result.check_name == "arithmetic_error":
        for err in det_result.details.get("errors", []):
            slug = err.split(":")[0].strip()
            affected_slugs.add(slug)
    if not affected_slugs:
        affected_slugs = set(item_groundings.keys())

    for slug in sorted(affected_slugs):
        grounding = item_groundings.get(slug)
        if not grounding:
            continue
        if grounding.source_passage:
            parts.append(f"\nITEM [{slug}] SOURCE PASSAGE (verbatim from generator):\n{grounding.source_passage[:1200]}")
        elif grounding.pdf_passage:
            parts.append(f"\nITEM [{slug}] PDF PASSAGE:\n{grounding.pdf_passage[:1000]}")
        elif grounding.norm_passage:
            parts.append(f"\nITEM [{slug}] NORMALIZED:\n{grounding.norm_passage[:600]}")

    return "\n".join(parts)


def _enrich_modeled_context(ctx: ModeledContext | None, haystaq: dict) -> ModeledContext:
    """Replace briefing-truncated topIssues with the full issue list from the haystaq JSON."""
    existing = ctx or ModeledContext(available=False)
    return ModeledContext(
        available=True,
        voter_count=haystaq.get("voter_count_with_scores") or existing.voter_count,
        issues=haystaq.get("issues", existing.issues),
        raw=haystaq,
    )


def run_qa(
    project_input: ProjectInput,
    spec: QAProjectSpec,
    normalized: dict,
    haystaq: dict | None,
    pdf_bytes: bytes | None,
    cfg: QARunConfig,
    output_dir: Path,
) -> dict:
    """Run the full QA pipeline for one document.

    Returns a result dict with at least:
        status          — "ok" | "error"
        document_id     — project_input.document_id
        delivery_status — "OK" | "Block"
        reason          — human-readable reason for the route decision
        summary         — path to markdown summary (if emitted)
        workbook        — path to xlsx review log (if emitted)
        trace           — path to JSON trace (if emitted)
    """
    stem = project_input.document_id
    run_output_dir = output_dir / stem
    run_output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"QA: {stem}")
    print(f"{'='*60}")

    # ── 1. Deterministic checks ───────────────────────────────────────────────
    det_results = []
    if cfg.run_deterministic:
        det_results = run_deterministic_checks(project_input, normalized, haystaq)
        n_block = sum(1 for r in det_results if r.blocks)
        print(f"  Deterministic: {len(det_results)} check(s), {n_block} blocking")

    # ── 1b. Enrich modeled_context with full haystaq issue list ──────────────
    if haystaq and haystaq.get("issues"):
        project_input.modeled_context = _enrich_modeled_context(
            project_input.modeled_context, haystaq
        )
        n_issues = len(project_input.modeled_context.issues)
        print(f"  Haystaq: {n_issues} issue scores loaded")

    # ── 2. PDF text extraction ────────────────────────────────────────────────
    pdf_text: str | None = None
    if pdf_bytes:
        try:
            pdf_text, _ = extract_pdf_text(pdf_bytes)
            print(f"  PDF: {len(pdf_text):,} chars extracted")
        except Exception as e:
            print(f"  PDF: extraction failed — {e}")

    # ── 3. Grounding per item ─────────────────────────────────────────────────
    norm_items = normalized.get("agenda", {}).get("items", [])
    item_groundings = {}
    for item in project_input.items:
        norm_item = find_norm_item(item.title, norm_items)
        item_groundings[item.slug] = build_item_grounding(item, norm_item, pdf_text)

    # ── 3.5. LLM verification of soft-blocking deterministic results ──────────
    soft_blocking = [r for r in det_results if r.blocks and r.needs_llm_verification]
    if soft_blocking and cfg.run_phase1 and cfg.triage_judge:
        from qa.adjudication.judge_runner import dispatch
        from qa.adjudication.prompts import build_det_verification_prompt
        judge = cfg.triage_judge
        for det_result in soft_blocking:
            context = _build_det_context(det_result, item_groundings, project_input)
            prompt = build_det_verification_prompt(det_result, context)
            try:
                result = dispatch(
                    judge.provider, judge.model, judge.api_key, prompt, max_tokens=512,
                )
                det_result.llm_verdict = result.get("verdict", "")
                det_result.llm_rationale = result.get("rationale", "")
                print(f"  [det-verify] {det_result.check_name}: {det_result.llm_verdict or 'no verdict'}")
            except Exception as e:
                print(f"  [det-verify] {det_result.check_name}: LLM verification failed — {e}")

    # ── 4. Claim extraction ───────────────────────────────────────────────────
    all_claims = []
    if cfg.run_extraction and cfg.triage_judge:
        from qa.extraction.claim_extractor import extract_claims
        for item in project_input.items:
            grounding = item_groundings[item.slug]
            claims = extract_claims(item, grounding, project_input.modeled_context, cfg)
            for claim in claims:
                claim.index = len(all_claims)
                all_claims.append(claim)
        print(f"  Extraction: {len(all_claims)} total claim(s)")
    else:
        if not cfg.run_extraction:
            print("  Extraction: skipped (run_extraction=False)")

    # ── 5. Evidence bundles ───────────────────────────────────────────────────
    bundles: dict[int, EvidenceBundle] = {}
    for claim in all_claims:
        grounding = item_groundings[claim.item_slug]
        bundles[claim.index] = build_bundle(
            claim, grounding, project_input.modeled_context, full_pdf_text=pdf_text or ""
        )

    # ── 6. Adjudication ───────────────────────────────────────────────────────
    adjudications: list[ClaimAdjudication] = []
    if cfg.run_phase1 and all_claims and cfg.triage_judge:
        from qa.adjudication.router import run_adjudication
        adjudications = run_adjudication(all_claims, bundles, cfg)
    else:
        adjudications = [
            ClaimAdjudication(claim=c, bundle=bundles.get(c.index, EvidenceBundle("", "", "")))
            for c in all_claims
        ]

    # ── 7. Route ──────────────────────────────────────────────────────────────
    route = route_decision(det_results, adjudications, cfg.blockable_tiers)
    print(f"  Route: {route.final_status} — {route.human_reason}")

    # ── 8. Reporting ──────────────────────────────────────────────────────────
    output_paths: dict[str, str] = {}

    if cfg.emit_summary:
        from qa.reporting.summary import write_summary
        md_path = run_output_dir / f"{stem}_qa_summary.md"
        write_summary(route, project_input, md_path)
        output_paths["summary"] = str(md_path)

    if cfg.emit_workbook and (adjudications or det_results):
        from qa.reporting.review_log import write_workbook
        xlsx_path = run_output_dir / f"{stem}_review_log.xlsx"
        write_workbook(adjudications, project_input, xlsx_path, det_results=det_results)
        output_paths["workbook"] = str(xlsx_path)

    if cfg.emit_trace:
        from qa.reporting.review_log import write_trace
        trace_path = run_output_dir / f"{stem}_trace.json"
        write_trace(route, project_input, trace_path)
        output_paths["trace"] = str(trace_path)

    return {
        "status": "ok",
        "document_id": stem,
        "delivery_status": route.final_status,
        "reason": route.human_reason,
        **output_paths,
    }
