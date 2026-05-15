"""run_qa.py — Unified QA pipeline runner.

Briefings can come from S3 or local storage. Input is specified as:

  Positional args (spec)
    S3 key(s)        — meeting_pipeline/output/briefings/falmouth-MA_2026-04-21_briefing.json
    Local path(s)    — output/recent/falmouth-MA_2026-04-21/falmouth-MA_2026-04-21_briefing.json
    Natural-language — "the 4 cities from today"  (resolved via LLM against available inventory)

  Flags
    --recent         briefings generated within --hours whose meeting is on/after --since
    --batch          all briefings in S3 prefix

Options:
    --since YYYY-MM-DD   minimum meeting date for --recent  (default: 7 days ago)
    --hours N            recency window for --recent  (default: 24)
    --output-dir PATH    output directory  (default: output/recent)
    --no-llm             deterministic checks only (no extraction or adjudication)

Examples:
    uv run python scripts/run_qa.py "the 4 cities from today"
    uv run python scripts/run_qa.py "all massachusetts cities" --no-llm
    uv run python scripts/run_qa.py meeting_pipeline/output/briefings/falmouth-MA_2026-04-21_briefing.json
    uv run python scripts/run_qa.py output/recent/falmouth-MA_2026-04-21/falmouth-MA_2026-04-21_briefing.json
    uv run python scripts/run_qa.py --recent --since 2026-04-10 --hours 48
    uv run python scripts/run_qa.py --batch --no-llm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

_REPO_ROOT  = Path(__file__).resolve().parent.parent
_DEFAULT_GP = _REPO_ROOT.parent / "gp-ai-projects"
_GP_ROOT    = Path(os.environ.get("GP_PIPELINE_ROOT", str(_DEFAULT_GP)))

for _p in (str(_GP_ROOT), str(_GP_ROOT / "meeting_pipeline")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.path.insert(0, str(_REPO_ROOT))

from meeting_pipeline.shared.config import AgentConfig, get_storage
from qa.engine.config import QARunConfig
from qa.engine.runner import run_qa
from qa.evidence.grounding import load_pdf_bytes_from_files
from qa.inputs.meeting_briefing_spec import MeetingBriefingSpec


# ── BriefingRef ───────────────────────────────────────────────────────────────

@dataclass
class BriefingRef:
    location: str   # S3 key or absolute/relative local path
    source: str     # "s3" or "local"
    stem: str       # e.g. falmouth-MA_2026-04-21


# ── Helpers ───────────────────────────────────────────────────────────────────

_STEM_RE = re.compile(r"^(.+)_briefing\.json$")


def _stem(location: str) -> str:
    m = _STEM_RE.match(Path(location).name)
    return m.group(1) if m else Path(location).stem


def _ref(location: str) -> BriefingRef:
    source = "local" if Path(location).exists() else "s3"
    return BriefingRef(location=location, source=source, stem=_stem(location))


def _agenda_files(normalized: dict) -> list:
    return (
        (normalized.get("sources") or {}).get("agenda_files")
        or normalized.get("agenda_files")
        or []
    )


def _load_json_s3(key: str, storage) -> dict | None:
    try:
        if storage.exists(key):
            return storage.read_json(key)
    except Exception as e:
        print(f"  [loader] {key}: {e}")
    return None


def _load_json_local(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [loader] {path}: {e}")
    return None


# ── Load data for a ref ───────────────────────────────────────────────────────

def _load_ref(
    ref: BriefingRef,
    storage,
    agent_cfg: AgentConfig,
    output_dir: Path,
) -> tuple[dict | None, dict, dict | None, bytes | None]:
    """Return (briefing, normalized, haystaq, pdf_bytes)."""
    item_dir = output_dir / ref.stem
    item_dir.mkdir(parents=True, exist_ok=True)

    if ref.source == "local":
        base       = Path(ref.location).parent
        briefing   = _load_json_local(Path(ref.location))
        normalized = _load_json_local(base / f"{ref.stem}_normalized.json") or {}
        haystaq    = (
            _load_json_local(base / f"{ref.stem}_issue_scores.json")
            or _load_json_local(base / f"{ref.stem}_haystaq.json")
        )
        if haystaq is None and briefing:
            city_slug = (briefing.get("meeting") or {}).get("citySlug", "")
            if city_slug:
                hq_key = f"meeting_pipeline/sources/{city_slug}/constituent/issue_scores.json"
                haystaq = _load_json_s3(hq_key, storage)
        pdf_path  = base / f"{ref.stem}_agenda.pdf"
        pdf_bytes = pdf_path.read_bytes() if pdf_path.exists() else None
        if pdf_bytes is None:
            af = _agenda_files(normalized)
            if af:
                pdf_bytes = load_pdf_bytes_from_files(af, storage)
    else:
        prefix     = ref.location.rsplit("/", 1)[0]
        city_slug  = re.sub(r"_\d{4}-\d{2}-\d{2}$", "", ref.stem)
        norm_key   = f"{prefix.replace('/briefings', '/normalized')}/{ref.stem}.json"
        hq_key     = f"meeting_pipeline/sources/{city_slug}/constituent/issue_scores.json"

        briefing   = _load_json_s3(ref.location, storage)
        normalized = _load_json_s3(norm_key, storage) or {}
        haystaq    = _load_json_s3(hq_key, storage)

        pdf_bytes = None
        af = _agenda_files(normalized)
        if af:
            pdf_bytes = load_pdf_bytes_from_files(af, storage)

    # Persist inputs alongside outputs
    if briefing:
        (item_dir / f"{ref.stem}_briefing.json").write_text(
            json.dumps(briefing, indent=2), encoding="utf-8"
        )
    if normalized:
        (item_dir / f"{ref.stem}_normalized.json").write_text(
            json.dumps(normalized, indent=2), encoding="utf-8"
        )
    if haystaq:
        hq_out = item_dir / f"{ref.stem}_issue_scores.json"
        if not hq_out.exists():
            hq_out.write_text(json.dumps(haystaq, indent=2), encoding="utf-8")
    if pdf_bytes:
        pdf_out = item_dir / f"{ref.stem}_agenda.pdf"
        if not pdf_out.exists():
            pdf_out.write_bytes(pdf_bytes)

    return briefing, normalized, haystaq, pdf_bytes


# ── Per-briefing runner ───────────────────────────────────────────────────────

def run_for_ref(
    ref: BriefingRef,
    storage,
    agent_cfg: AgentConfig,
    qa_cfg: QARunConfig,
    output_dir: Path,
) -> dict:
    briefing, normalized, haystaq, pdf_bytes = _load_ref(ref, storage, agent_cfg, output_dir)
    if briefing is None:
        return {"status": "error", "error": "briefing not found", "document_id": ref.stem}
    spec = MeetingBriefingSpec()
    return run_qa(
        spec.to_project_input(briefing), spec, normalized, haystaq, pdf_bytes, qa_cfg, output_dir
    )


# ── Resolvers ─────────────────────────────────────────────────────────────────

def resolve_explicit(locations: list[str]) -> list[BriefingRef]:
    return [_ref(loc) for loc in locations]


def resolve_batch(storage, agent_cfg: AgentConfig) -> list[BriefingRef]:
    prefix = f"{agent_cfg.output_prefix}/briefings"
    return sorted(
        [
            BriefingRef(location=k, source="s3", stem=_stem(k))
            for k in storage.list_keys(prefix)
            if k.endswith("_briefing.json")
        ],
        key=lambda r: r.stem,
    )


def resolve_recent(
    storage,
    agent_cfg: AgentConfig,
    cutoff_utc: datetime,
    meeting_since: date,
) -> list[BriefingRef]:
    import boto3

    bucket      = getattr(agent_cfg, "s3_bucket", None) or os.environ.get("S3_BUCKET", "meeting-pipeline-dev")
    aws_profile = getattr(agent_cfg, "aws_profile", None) or os.environ.get("AWS_PROFILE", "goodparty")
    prefix      = f"{agent_cfg.output_prefix}/briefings"

    s3        = boto3.Session(profile_name=aws_profile).client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    refs: list[BriefingRef] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            lm: datetime = obj["LastModified"]
            if not key.endswith("_briefing.json") or lm < cutoff_utc:
                continue
            m = re.search(r"_(\d{4}-\d{2}-\d{2})_briefing\.json$", key.split("/")[-1])
            if not m:
                continue
            try:
                if date.fromisoformat(m.group(1)) < meeting_since:
                    continue
            except ValueError:
                continue
            refs.append(BriefingRef(location=key, source="s3", stem=_stem(key)))

    return sorted(refs, key=lambda r: r.stem)


def resolve_llm(
    prompt: str,
    storage,
    agent_cfg: AgentConfig,
    output_dir: Path,
) -> list[BriefingRef]:
    """Resolve a natural-language description to BriefingRefs via Claude."""
    s3_refs    = resolve_batch(storage, agent_cfg)
    local_refs = [
        BriefingRef(location=str(p), source="local", stem=_stem(str(p)))
        for p in sorted(output_dir.rglob("*_briefing.json"))
    ]

    # Prefer local when stem exists in both
    local_stems = {r.stem for r in local_refs}
    combined    = local_refs + [r for r in s3_refs if r.stem not in local_stems]

    if not combined:
        print("Inventory is empty — no briefings found locally or in S3.")
        return []

    inventory = "\n".join(f"{r.stem}  [{r.source}]" for r in combined)

    client  = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Available briefings:\n\n{inventory}\n\n"
                f"Which match: \"{prompt}\"\n\n"
                "Reply with only the matching stems, one per line. No explanations."
            ),
        }],
    )

    matched_stems = {
        line.strip()
        for line in message.content[0].text.strip().splitlines()
        if line.strip()
    }

    stem_index = {r.stem: r for r in combined}
    resolved   = sorted(
        [stem_index[s] for s in matched_stems if s in stem_index],
        key=lambda r: r.stem,
    )

    if resolved:
        print(f"Resolved {len(resolved)} briefing(s) for: {prompt!r}")
        for r in resolved:
            print(f"  {r.stem}  [{r.source}]")
    else:
        print(f"No matches found for: {prompt!r}")

    return resolved


# ── S3 output uploader ────────────────────────────────────────────────────────

_QA_S3_PREFIX = "meeting_pipeline/qa_outputs"


def _upload_qa_outputs(result: dict, storage, stem: str) -> None:
    """Upload local QA artifacts to S3 at meeting_pipeline/qa_outputs/{stem}/."""
    uploaded = []
    for key in ("summary", "workbook", "trace"):
        local = result.get(key)
        if not local:
            continue
        path = Path(local)
        if not path.exists():
            continue
        s3_key = f"{_QA_S3_PREFIX}/{stem}/{path.name}"
        storage.write_bytes(s3_key, path.read_bytes())
        uploaded.append(s3_key)
    if uploaded:
        print(f"  [s3] {len(uploaded)} QA file(s) → {_QA_S3_PREFIX}/{stem}/")


# ── Summary printer ───────────────────────────────────────────────────────────

def _print_summary(results: list[dict]) -> None:
    print(f"\n{'='*65}")
    print("RESULTS")
    print(f"{'='*65}")
    counts: dict[str, int] = {}
    for r in results:
        ds = r.get("delivery_status", "error")
        counts[ds] = counts.get(ds, 0) + 1
        marker = "🔴" if ds == "Block" else "🟢" if ds == "OK" else "⚠"
        reason = r.get("reason", r.get("error", ""))
        print(f"  {marker}  {r.get('document_id', '?')}")
        if reason:
            print(f"        {reason}")
    print()
    print(f"  🔴 Block : {counts.get('Block', 0)}")
    print(f"  🟢 OK    : {counts.get('OK', 0)}")
    if counts.get("error"):
        print(f"  ⚠  Error : {counts['error']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="QA pipeline — S3 or local, single or batch, LLM-promptable"
    )
    parser.add_argument(
        "spec", nargs="*",
        help="S3 key(s), local path(s), or a natural-language prompt",
    )
    parser.add_argument("--recent",     action="store_true", help="Run on recently generated briefings")
    parser.add_argument("--batch",      action="store_true", help="Run on all briefings in S3 prefix")
    parser.add_argument("--since",      default=None,        help="Minimum meeting date (YYYY-MM-DD)")
    parser.add_argument("--hours",      type=int, default=24, help="Recency window for --recent (hours)")
    parser.add_argument("--output-dir", default="output/recent")
    parser.add_argument("--no-llm",     action="store_true", help="Deterministic checks only")
    parser.add_argument("--output-s3",  action="store_true", help="Upload QA outputs to S3 after each run")
    args = parser.parse_args()

    if not args.spec and not args.recent and not args.batch:
        parser.error("Provide a spec (keys, paths, or natural-language prompt), --recent, or --batch")

    agent_cfg  = AgentConfig.from_env()
    storage    = get_storage(agent_cfg)
    output_dir = Path(args.output_dir)

    qa_cfg = QARunConfig.from_env()
    if args.no_llm:
        qa_cfg.run_extraction = False
        qa_cfg.run_phase1     = False
        qa_cfg.run_phase2     = False
        qa_cfg.emit_workbook  = False
        qa_cfg.emit_trace     = False

    # ── Resolve ───────────────────────────────────────────────────────────────
    if args.recent:
        since_str = args.since or (date.today() - timedelta(days=7)).isoformat()
        try:
            meeting_since = date.fromisoformat(since_str)
        except ValueError:
            parser.error(f"--since must be YYYY-MM-DD, got: {since_str!r}")
        cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        refs = resolve_recent(storage, agent_cfg, cutoff_utc, meeting_since)

    elif args.batch:
        refs = resolve_batch(storage, agent_cfg)

    else:
        looks_explicit = all(
            Path(s).exists() or s.endswith("_briefing.json")
            for s in args.spec
        )
        if looks_explicit:
            refs = resolve_explicit(args.spec)
        else:
            refs = resolve_llm(" ".join(args.spec), storage, agent_cfg, output_dir)

    if not refs:
        print("No briefings to process.")
        return

    print(f"\n{'='*65}")
    print(f"QA PIPELINE  —  {len(refs)} briefing(s)")
    print(f"  LLM adjudication : {'off (--no-llm)' if args.no_llm else 'on'}")
    print(f"  Output           : {output_dir.resolve()}")
    print(f"{'='*65}")

    # ── Run ───────────────────────────────────────────────────────────────────
    results = []
    for ref in refs:
        print(f"\n  ── {ref.stem}  [{ref.source}]")
        try:
            r = run_for_ref(ref, storage, agent_cfg, qa_cfg, output_dir)
            if args.output_s3 and r.get("status") == "ok":
                _upload_qa_outputs(r, storage, ref.stem)
        except Exception as e:
            r = {"status": "error", "error": str(e), "document_id": ref.stem}
            print(f"  ERROR: {e}")
        results.append(r)

    _print_summary(results)
    print(f"\nOutputs: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
