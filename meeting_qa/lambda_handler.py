"""SQS-triggered Lambda handler for meeting briefing QA.

Receives messages of the form:
    {"briefing_key": "meeting_pipeline/output/briefings/<slug>_<date>_briefing.json"}

For each message:
  1. Loads briefing.json + normalized.json + (optional) haystaq.json + (optional) PDF from S3
  2. Runs the QA engine
  3. Uploads outputs (qa_summary.md, review_log.xlsx, trace.json) to
     s3://<bucket>/meeting_pipeline/output/qa/<stem>/

This handler is intentionally self-contained and does NOT depend on
meeting_pipeline.shared.config — it speaks to S3 directly via boto3.

Env (set by Lambda or by Secrets Manager via _inject_secrets):
  S3_BUCKET             — bucket name (required)
  ANTHROPIC_API_KEY     — for the Claude triage judge (required)
  GEMINI_API_KEY        — for the Gemini escalation judge (required)
  AI_SECRET_ID          — optional override of the Secrets Manager secret name
                          (default: AI_SECRETS_<ENVIRONMENT>)
  ENVIRONMENT           — dev | qa | prod (used to derive AI_SECRET_ID default)
  QA_JUDGES             — optional; overrides default judge config
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import boto3

from qa.engine.config import QARunConfig
from qa.engine.runner import run_qa
from qa.evidence.grounding import load_pdf_bytes_from_files
from qa.inputs.meeting_briefing_spec import MeetingBriefingSpec

s3 = boto3.client("s3")

QA_OUTPUT_PREFIX = "meeting_pipeline/output/qa"


def _bucket() -> str:
    """Read S3_BUCKET at call time so env changes (e.g. between tests, or
    after Secrets Manager injection) are picked up. Raises if unset."""
    b = os.environ.get("S3_BUCKET", "")
    if not b:
        raise RuntimeError("S3_BUCKET environment variable is not set")
    return b


# ── Secrets injection ──────────────────────────────────────────────────────

_SECRETS_INJECTED = False


def _inject_secrets() -> None:
    """Load API keys from Secrets Manager into os.environ. Idempotent.

    Skips Secrets Manager entirely if both keys are already set in env (e.g. local CLI).
    """
    global _SECRETS_INJECTED
    if _SECRETS_INJECTED:
        return
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        _SECRETS_INJECTED = True
        return

    environment = os.environ.get("ENVIRONMENT", "dev").upper()
    secret_id = os.environ.get("AI_SECRET_ID", f"AI_SECRETS_{environment}")

    try:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_id)
        secrets = json.loads(response["SecretString"])
    except Exception as e:
        print(f"  [secrets] failed to load {secret_id}: {e}")
        return

    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        if key in secrets and not os.environ.get(key):
            os.environ[key] = secrets[key]

    _SECRETS_INJECTED = True


# ── S3 helpers ─────────────────────────────────────────────────────────────

def _read_json_s3(key: str) -> dict | None:
    try:
        obj = s3.get_object(Bucket=_bucket(), Key=key)
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        print(f"  [s3] read_json failed for {key}: {e}")
        return None


def _read_bytes_s3(key: str) -> bytes | None:
    try:
        obj = s3.get_object(Bucket=_bucket(), Key=key)
        return obj["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        print(f"  [s3] read_bytes failed for {key}: {e}")
        return None


def _upload_dir(local_dir: Path, s3_prefix: str) -> list[str]:
    """Upload every file in local_dir (recursive) to s3://<bucket>/<s3_prefix>/<relative path>."""
    bucket = _bucket()
    uploaded = []
    for path in local_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir)
        key = f"{s3_prefix}/{rel.as_posix()}"
        s3.upload_file(str(path), bucket, key)
        uploaded.append(key)
    return uploaded


# ── Storage shim used by load_pdf_bytes_from_files ─────────────────────────

class _S3Storage:
    """Minimal storage shim so qa.evidence.grounding.load_pdf_bytes_from_files works."""

    def read_bytes(self, key: str) -> bytes | None:
        return _read_bytes_s3(key)

    def exists(self, key: str) -> bool:
        try:
            s3.head_object(Bucket=_bucket(), Key=key)
            return True
        except Exception:
            return False


# ── Agenda-file lookup (matches normalized JSON shape from meeting_pipeline) ──

def _agenda_files(normalized: dict) -> list:
    """Find the agenda_files list in a normalized briefing JSON.

    Canonical location is normalized["sources"]["agenda_files"] (snake_case).
    Falls back to a top-level "agenda_files" for older shapes.
    """
    return (
        (normalized.get("sources") or {}).get("agenda_files")
        or normalized.get("agenda_files")
        or []
    )


# ── One-briefing runner ────────────────────────────────────────────────────

def run_qa_for_briefing(briefing_key: str) -> dict:
    """Run QA on a single briefing identified by its S3 key.

    Returns a result dict on permanent outcomes (briefing missing, bad shape).
    Raises on transient failures (S3 read errors, LLM call errors) so the
    Lambda handler can propagate to SQS and trigger redrive/DLQ."""
    # Derive related keys
    # briefing_key: "meeting_pipeline/output/briefings/<stem>.json"
    stem = Path(briefing_key).stem.removesuffix("_briefing")
    # e.g. chapel-hill-NC_2026-04-29
    norm_key = briefing_key.replace("/briefings/", "/normalized/").replace(
        f"{stem}_briefing.json", f"{stem}.json"
    )

    briefing = _read_json_s3(briefing_key)
    if not briefing:
        return {"status": "error", "error": "briefing not found", "briefing_key": briefing_key}

    normalized = _read_json_s3(norm_key) or {}

    # Optional haystaq
    city_slug = (briefing.get("meeting") or {}).get("citySlug", "")
    haystaq = None
    if city_slug:
        hq_key = f"meeting_pipeline/sources/{city_slug}/constituent/issue_scores.json"
        haystaq = _read_json_s3(hq_key)

    # Optional PDF — read agenda_files from canonical normalized JSON shape
    pdf_bytes: bytes | None = None
    af = _agenda_files(normalized)
    if af:
        try:
            pdf_bytes = load_pdf_bytes_from_files(af, _S3Storage())
        except Exception as e:
            print(f"  [pdf] load failed: {e}")

    # Build inputs and run (run_qa raises on LLM/transient failures)
    cfg = QARunConfig.from_env()
    spec = MeetingBriefingSpec()
    project_input = spec.to_project_input(briefing)

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        result = run_qa(project_input, spec, normalized, haystaq, pdf_bytes, cfg, out_dir)

        # Upload everything in {tmp}/{stem}/ to s3://<bucket>/{QA_OUTPUT_PREFIX}/{stem}/
        run_dir = out_dir / project_input.document_id
        if run_dir.exists():
            uploaded = _upload_dir(run_dir, f"{QA_OUTPUT_PREFIX}/{project_input.document_id}")
            result["uploaded"] = uploaded

    return result


# ── Lambda entry points ────────────────────────────────────────────────────

def handler(event, context=None):
    """SQS-triggered handler.

    Accepts either a raw SQS event (Records[]) or a direct invoke with
    {"briefing_key": "..."} for testing.

    Permanent failures (bad message body, missing briefing) are returned in
    the result list. Transient failures (S3 read, LLM call errors) are
    re-raised at the end so SQS redrives the message — defeating the swallow
    pattern that would otherwise bury errors in CloudWatch and prevent DLQ
    routing after maxReceiveCount.
    """
    _inject_secrets()

    # Direct-invoke shape (for testing) — propagate exceptions
    if "briefing_key" in event:
        return run_qa_for_briefing(event["briefing_key"])

    # SQS event shape
    results = []
    transient_errors = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
        except (json.JSONDecodeError, KeyError):
            # Bad message shape — don't retry, ack and move on.
            results.append({"status": "error", "error": "bad message body"})
            continue

        briefing_key = body.get("briefing_key", "")
        if not briefing_key:
            # Missing required field — don't retry.
            results.append({"status": "error", "error": "missing briefing_key"})
            continue

        try:
            results.append(run_qa_for_briefing(briefing_key))
        except Exception as e:
            # Transient failure (S3 read, LLM call, etc.) — collect to re-raise.
            err = f"{briefing_key}: {type(e).__name__}: {str(e)[:200]}"
            print(f"  [qa] transient failure for {briefing_key}: {e}")
            transient_errors.append(err)
            results.append({"status": "error", "error": str(e), "briefing_key": briefing_key, "transient": True})

    if transient_errors:
        # Tell SQS the invocation failed → message becomes visible again
        # → redelivered up to maxReceiveCount → routed to DLQ on persistent failure.
        raise RuntimeError("Transient QA failures: " + "; ".join(transient_errors))

    return {"results": results}
