import json
import logging
import os
import re

import boto3
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from broker.callback_sender import CallbackSender
from broker.data_query_tracker import DataQueryTracker
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.pii_scanner import scan_artifact

_PII_ENABLED_VALUES = {"1", "true", "yes"}

# PMF QA gate (contract D): the verdict is forwarded verbatim to the SQS
# callback, so it shares the callback's byte budget. The engine targets an
# 8 KiB serialized verdict (contract C), truncating to fit; this broker-side
# cap gives generous headroom over that target while still bounding a runaway
# verdict before it can blow the SQS payload limit. The broker keeps the
# verdict OPAQUE — this size check is the only verdict-specific gate.
MAX_QA_VERDICT_BYTES = 64 * 1024

# PMF QA gate (contract D / decision 13): the raw main.py stdout, written
# durably to S3 only (it never rides the SQS callback), so it has its own,
# larger budget — the same 1 MiB cap contract D specifies for the captured
# stage stdout. Bounds a runaway raw output before the broker writes it.
MAX_QA_RAW_OUTPUT_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifact", tags=["artifact"])

# Process-cached CloudWatch client. boto3.client() does endpoint resolution +
# credential fetching + TLS setup once; reused per call. Lazy-init at first
# metric emission so import-time has no AWS deps (mirrors experiment_manifest).
_cw_client = None


def _get_cw_client():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def _reset_cw_client_for_tests() -> None:
    global _cw_client
    _cw_client = None


def _emit_metric(metric_name: str, dimensions: list[dict]) -> None:
    """Emit a CloudWatch metric. Swallows all exceptions — metric emission must
    never fail the calling code. Broker has a no-cross-package-deps rule, so
    this is local instead of using shared.metrics (mirrors experiment_manifest).
    """
    try:
        _get_cw_client().put_metric_data(
            Namespace="Broker",
            MetricData=[{
                "MetricName": metric_name,
                "Value": 1,
                "Unit": "Count",
                "Dimensions": dimensions,
            }],
        )
    except Exception as e:
        logger.warning(
            "MetricEmissionFailed metric=%s exc_type=%s: %s",
            metric_name, type(e).__name__, e, exc_info=True,
        )

def _is_precondition_failed(err: Exception) -> bool:
    """True iff a boto3/ClientError carries a PreconditionFailed (HTTP 412)
    error code — i.e. an IfNoneMatch=* write-once collision. Defensive against
    non-ClientError exceptions and malformed error shapes (returns False)."""
    try:
        code = err.response["Error"]["Code"]  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError):
        return False
    return code in ("PreconditionFailed", "412")


_DANGEROUS_HTML_RE = re.compile(
    r"<script|<img\b|javascript:", re.IGNORECASE
)

# The downstream agent's sanitizer.fence_content wraps artifact text in
# <untrusted_web_content>...</untrusted_web_content> so the reading agent
# treats it as data, not instructions. An upstream agent embedding either
# tag in its artifact can break out of the fence and inject "system" text
# into any downstream experiment that depends on this artifact.
# _DANGEROUS_HTML_RE doesn't cover this, so reject explicitly.
_FENCE_BREAKOUT_RE = re.compile(r"</?untrusted_web_content\b", re.IGNORECASE)


class PublishRequest(BaseModel):
    artifact: dict
    duration_seconds: float = 0
    cost_usd: float = 0
    # PMF QA gate (contract D, v1 observe-only). The runner attaches the
    # gate's verdict here on the success path. The broker treats it as an
    # OPAQUE passthrough — it does NOT re-run jsonschema or any shape check
    # (it validates only `artifact`); the only verdict-specific gate is the
    # MAX_QA_VERDICT_BYTES size cap enforced in the handler. This field MUST
    # be DECLARED: pydantic's default extra='ignore' would silently drop an
    # undeclared field, so the runner's verdict would never reach the handler.
    # Absent / None = no gate ran (no qa folder, or a pre-gate runner) — the
    # success path then stays byte-identical to today.
    qa_verdict: dict | None = None
    # PMF QA gate (contract D / decision 13). The raw main.py stdout the gate
    # captured, carried so the broker can write it durably to S3 alongside the
    # aggregated verdict (the runner is sandboxed — the broker is its only
    # egress). It NEVER rides the SQS callback, so it has its own 1 MiB budget
    # rather than the verdict's callback-bound cap. Like qa_verdict, this MUST
    # be DECLARED: pydantic's default extra='ignore' would silently drop an
    # undeclared field, so the durable raw write would never happen. Absent /
    # None = the broker writes only the aggregated verdict.
    qa_raw_output: str | None = None


class PublishResponse(BaseModel):
    artifact_key: str
    artifact_bucket: str
    callback_sent: bool


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError

def get_s3_client():  # pragma: no cover
    raise NotImplementedError

def get_callback_sender() -> CallbackSender:  # pragma: no cover
    raise NotImplementedError

def get_ticket_store() -> ScopeTicketStore:  # pragma: no cover
    raise NotImplementedError

def get_broker_token_raw() -> str:  # pragma: no cover
    raise NotImplementedError

def get_artifact_bucket() -> str:  # pragma: no cover
    raise NotImplementedError


def get_data_query_tracker() -> DataQueryTracker:  # pragma: no cover
    raise NotImplementedError


def _collect_strings(obj, path: str = "") -> list[tuple[str, str]]:
    results = []
    if isinstance(obj, str):
        results.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(_collect_strings(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(_collect_strings(item, f"{path}[{i}]"))
    return results


def _check_html(artifact: dict) -> str | None:
    for field_path, value in _collect_strings(artifact):
        if _DANGEROUS_HTML_RE.search(value):
            return f"Raw HTML detected in field '{field_path}'"
    return None


def _check_fence_breakout(artifact: dict) -> str | None:
    for field_path, value in _collect_strings(artifact):
        if _FENCE_BREAKOUT_RE.search(value):
            return (
                f"fence-breakout token detected in field '{field_path}': "
                "artifacts cannot contain <untrusted_web_content> tags "
                "(would escape the downstream agent's data fence)"
            )
    return None


@router.post("/publish", response_model=PublishResponse)
def artifact_publish(
    req: PublishRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    s3_client=Depends(get_s3_client),
    callback_sender: CallbackSender = Depends(get_callback_sender),
    store: ScopeTicketStore = Depends(get_ticket_store),
    broker_token: str = Depends(get_broker_token_raw),
    bucket: str = Depends(get_artifact_bucket),
    tracker: DataQueryTracker = Depends(get_data_query_tracker),
):
    # Anti-fabrication gate: if the manifest declared scope.allowed_tables
    # (i.e. this experiment uses Databricks) but no Databricks query
    # succeeded during this run, refuse to publish. The agent fabricated
    # its output — Databricks was unreachable, scope rejected every query,
    # or the agent never tried. Schema-valid synthetic data passes the
    # output_schema check; the only trustworthy signal that real data
    # backed the artifact is whether the broker mediated a real query.
    #
    # Keyed off ticket.scope (manifest-derived), NOT a hardcoded experiment
    # list — broker stays consumer-domain-agnostic. Any new experiment that
    # adds allowed_tables automatically gets the safety check; web-only
    # experiments skip it.
    #
    # Carve-out for legitimate no-data outcomes: a manifest may declare
    #   scope.data_required_unless = {"field": "<artifact_field>",
    #                                  "values": ["<v1>", ...]}
    # When the artifact's named field carries one of those values (e.g.
    # meeting_briefing's `briefing_status=awaiting_agenda` placeholder when
    # the next council meeting's agenda packet hasn't been published yet),
    # the gate is skipped — no data query is appropriate for that branch.
    # Broker stays domain-agnostic: it doesn't know what the values mean,
    # only that the manifest declared them as exemptions.
    if ticket.scope.get("allowed_tables") and tracker.get(ticket.pk) == 0:
        # carve_out shape is validated by the manifest meta-schema at publish
        # time in the runbooks repo, but the broker treats it as untrusted dict
        # input — a malformed `data_required_unless` (missing 'field', missing
        # 'values', wrong types) must not crash the publish path with a raw
        # 500. Use .get() everywhere and fail safe: any malformed shape falls
        # back to today's strict gate behavior.
        carve_out = ticket.scope.get("data_required_unless")
        carve_field = (
            carve_out.get("field") if isinstance(carve_out, dict) else None
        )
        carve_values = (
            carve_out.get("values") if isinstance(carve_out, dict) else None
        )
        artifact_field_value = (
            req.artifact.get(carve_field)
            if isinstance(carve_field, str) and isinstance(req.artifact, dict)
            else None
        )
        carve_applies = (
            isinstance(carve_values, list)
            and artifact_field_value is not None
            and artifact_field_value in carve_values
        )
        if not carve_applies:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"NoDataQueriesSucceeded: experiment '{ticket.experiment_id}' "
                    f"declares scope.allowed_tables but no Databricks query succeeded "
                    f"during this run. Refusing to publish — this prevents synthetic "
                    f"artifacts from being accepted when data sources are unreachable."
                ),
            )

    if os.environ.get("ENABLE_PII_SCANNER", "").strip().lower() in _PII_ENABLED_VALUES:
        pii_matches = scan_artifact(req.artifact)
        if pii_matches:
            fields = ", ".join(m.field_path or "unknown" for m in pii_matches)
            raise HTTPException(status_code=400, detail=f"PII detected in artifact fields: {fields}")

    html_error = _check_html(req.artifact)
    if html_error:
        raise HTTPException(status_code=400, detail=f"Raw HTML not allowed: {html_error}")

    fence_error = _check_fence_breakout(req.artifact)
    if fence_error:
        raise HTTPException(status_code=400, detail=fence_error)

    # PMF QA gate (contract D, v1 observe-only / fail-open): the qa-capture
    # path must NEVER fail the publish. A 400 here would turn the run FAILED in
    # the runner, breaking observe-only — so a size-cap breach SKIPS the durable
    # S3 write instead of rejecting. The verdict still rides the SQS callback
    # verbatim (the callback layer owns its own budget); only the durable
    # verdict.json write is governed by MAX_QA_VERDICT_BYTES. An oversize verdict
    # is LOGGED + metric-emitted + the verdict.json write is skipped.
    skip_qa_verdict_write = False
    if req.qa_verdict is not None:
        verdict_bytes = len(json.dumps(req.qa_verdict))
        if verdict_bytes > MAX_QA_VERDICT_BYTES:
            skip_qa_verdict_write = True
            logger.error(
                "qa_verdict_size_cap_exceeded run_id=%s experiment_id=%s "
                "observed=%d cap=%d (fail-open: skipping durable verdict.json "
                "write; the verdict still rides the callback)",
                ticket.run_id, ticket.experiment_id, verdict_bytes, MAX_QA_VERDICT_BYTES,
            )
            _emit_metric("broker_qa_verdict_size_cap_exceeded", [
                {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
                {"Name": "experiment_id", "Value": ticket.experiment_id},
            ])

    # PMF QA gate (contract D / decision 13, fail-open): the raw main.py output
    # is written durably to S3 only (never the callback), with its own 1 MiB
    # budget. An oversize raw output SKIPS the main_output.json write (LOG +
    # metric) rather than rejecting — same observe-only / fail-open rule as the
    # verdict. The aggregated verdict.json write and the callback are unaffected.
    skip_qa_raw_write = False
    if req.qa_raw_output is not None:
        raw_bytes = len(req.qa_raw_output.encode("utf-8"))
        if raw_bytes > MAX_QA_RAW_OUTPUT_BYTES:
            skip_qa_raw_write = True
            logger.error(
                "qa_raw_output_size_cap_exceeded run_id=%s experiment_id=%s "
                "observed=%d cap=%d (fail-open: skipping durable main_output.json "
                "write; the aggregated verdict is unaffected)",
                ticket.run_id, ticket.experiment_id, raw_bytes, MAX_QA_RAW_OUTPUT_BYTES,
            )
            _emit_metric("broker_qa_raw_output_size_cap_exceeded", [
                {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "unknown")},
                {"Name": "experiment_id", "Value": ticket.experiment_id},
            ])

    artifact_json = json.dumps(req.artifact)
    latest_key = f"{ticket.experiment_id}/{ticket.organization_slug}/latest.json"
    run_key = f"{ticket.experiment_id}/{ticket.run_id}/artifact.json"

    try:
        # Write the immutable per-run archive FIRST so the mutable latest.json
        # pointer can never outrun it. If the archive put fails, latest.json is
        # not yet updated, keeping S3 internally consistent.
        # IfNoneMatch=* makes the archive write-once at the S3 layer — a
        # second publish for the same run_id (e.g., from a leaked broker_token
        # bypassing post-publish ticket-delete) will 412 instead of silently
        # overwriting the immutable record that downstream experiments depend on.
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=run_key,
                Body=artifact_json,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except Exception as run_err:
            error_code = ""
            try:
                error_code = run_err.response["Error"]["Code"]  # type: ignore[attr-defined]
            except (AttributeError, KeyError, TypeError):
                pass
            if error_code in ("PreconditionFailed", "412"):
                logger.warning(
                    "duplicate publish blocked for run_id=%s experiment_id=%s "
                    "(archive already exists)",
                    ticket.run_id, ticket.experiment_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Artifact for run {ticket.run_id} was already published; "
                        "duplicate publish refused (archive is immutable)"
                    ),
                ) from None
            raise
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=latest_key,
                Body=artifact_json,
                ContentType="application/json",
            )
        except Exception as latest_err:
            logger.warning(
                "latest.json update failed run_id=%s experiment_id=%s key=%s bucket=%s: %s. "
                "Archive write succeeded; callback carries run-scoped key. latest.json is "
                "a best-effort convenience pointer and is eventually consistent.",
                ticket.run_id, ticket.experiment_id, latest_key, bucket, latest_err,
                exc_info=True,
            )
    except HTTPException:
        raise
    except Exception:
        logger.error(
            "S3 publish failed run_id=%s experiment_id=%s bucket=%s",
            ticket.run_id, ticket.experiment_id, bucket,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to publish artifact to S3"
        ) from None

    # PMF QA gate (contract D / decision 13): durable, observe-only S3 capture.
    # When a verdict is present, write it (and the raw main.py output, when the
    # runner included it) under the run's qa prefix — the same prefix where
    # artifact.json already lives. This is INDEPENDENT of Braintrust and the
    # SQS callback: the verdict survives even if both are lost. The runner is
    # sandboxed (the broker is its only egress), so the broker performs the
    # write.
    #
    # BEST-EFFORT and ADDITIVE: it runs only AFTER artifact.json succeeded
    # (above), a failure is logged with run_id but does NOT fail the publish,
    # and the verdict still rides the callback below. No qa_verdict => no qa
    # write, so the no-qa key set stays byte-identical to a pre-gate publish.
    # Both qa writes are write-once (IfNoneMatch=*), mirroring the artifact.json
    # archive: a duplicate publish for the same run must not silently overwrite
    # the per-run qa record. A write-once collision (PreconditionFailed / 412)
    # is logged INFO and swallowed — best-effort / observe-only, never fatal.
    # Tracks whether the sibling verdict.json actually exists in S3 after this
    # block: True only when the put_object succeeded, OR when a duplicate publish
    # hit the write-once 412 (the original verdict.json is already there). A
    # caught/logged/fail-open failure leaves it False so the coupled raw write
    # below is skipped — never an orphan main_output.json without a verdict.json.
    verdict_written = False
    if req.qa_verdict is not None and not skip_qa_verdict_write:
        qa_verdict_key = f"{ticket.experiment_id}/{ticket.run_id}/qa/verdict.json"
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=qa_verdict_key,
                Body=json.dumps(req.qa_verdict),
                ContentType="application/json",
                IfNoneMatch="*",
            )
            verdict_written = True
        except Exception as verdict_err:
            if _is_precondition_failed(verdict_err):
                # The sibling already exists from a prior publish, so the raw
                # write below is NOT an orphan — treat as written.
                verdict_written = True
                logger.info(
                    "qa verdict already captured run_id=%s experiment_id=%s key=%s "
                    "(duplicate publish; write-once guard held, keeping the "
                    "original per-run record)",
                    ticket.run_id, ticket.experiment_id, qa_verdict_key,
                )
            else:
                logger.warning(
                    "qa verdict S3 capture failed run_id=%s experiment_id=%s key=%s "
                    "bucket=%s. Best-effort durable capture; the verdict still rides "
                    "the SQS callback and the Braintrust span.",
                    ticket.run_id, ticket.experiment_id, qa_verdict_key, bucket,
                    exc_info=True,
                )

    # The raw main.py output is the verdict's raw fragment output, so it is only
    # written when the sibling verdict.json actually landed in S3 (no orphan
    # main_output.json without a verdict.json) — preserving contract D's coupling
    # at the WRITE level, not just the request level. Its own size cap
    # (skip_qa_raw_write) is independent of the verdict's.
    if (
        verdict_written
        and req.qa_raw_output is not None
        and not skip_qa_raw_write
    ):
        qa_raw_key = f"{ticket.experiment_id}/{ticket.run_id}/qa/main_output.json"
        try:
            s3_client.put_object(
                Bucket=bucket,
                Key=qa_raw_key,
                Body=req.qa_raw_output,
                # Raw main.py stdout is NOT guaranteed JSON (especially on
                # stage-error paths), so it is stored as plain text.
                ContentType="text/plain; charset=utf-8",
                IfNoneMatch="*",
            )
        except Exception as raw_err:
            if _is_precondition_failed(raw_err):
                logger.info(
                    "qa raw output already captured run_id=%s experiment_id=%s "
                    "key=%s (duplicate publish; write-once guard held)",
                    ticket.run_id, ticket.experiment_id, qa_raw_key,
                )
            else:
                logger.warning(
                    "qa raw output S3 capture failed run_id=%s experiment_id=%s "
                    "key=%s bucket=%s. Best-effort durable capture; the aggregated "
                    "verdict was still captured and rides the callback.",
                    ticket.run_id, ticket.experiment_id, qa_raw_key, bucket,
                    exc_info=True,
                )

    # Callback carries the run-scoped immutable key. If we pointed gp-api at
    # latest.json, a subsequent regeneration of this (or dependent) experiment
    # would silently change what a SUCCESS run "produced", breaking the STALE
    # invariant for any downstream experiment that depends on this artifact.
    # Forward the QA verdict verbatim. req.qa_verdict defaults to None when no
    # gate ran (no qa folder, or a pre-gate runner); send_result omits the
    # qaVerdict key on the wire for a None verdict, so the no-qa success
    # callback stays byte-identical to a pre-gate run (verified in
    # test_callback_sender's omit-key tests).
    callback_sender.send_result(
        run_id=ticket.run_id,
        organization_slug=ticket.organization_slug,
        experiment_id=ticket.experiment_id,
        status="success",
        artifact_key=run_key,
        artifact_bucket=bucket,
        duration_seconds=req.duration_seconds,
        cost_usd=req.cost_usd,
        qa_verdict=req.qa_verdict,
    )

    try:
        store.delete_ticket_and_run_lock(broker_token, ticket.run_id)
    except Exception:
        logger.error(
            "ticket/run-lock delete failed after publish run_id=%s broker_token_prefix=%s",
            ticket.run_id, broker_token[:8],
            exc_info=True,
        )

    # Successful runs flow through here, not /run-status, so the per-ticket
    # tracker entry would otherwise leak forever. Failure is harmless — the
    # entry will be GC'd when the broker process restarts; reject path
    # already raised before we got here.
    try:
        tracker.clear(ticket.pk)
    except Exception:
        logger.warning(
            "tracker clear failed after publish run_id=%s",
            ticket.run_id, exc_info=True,
        )

    return PublishResponse(
        artifact_key=run_key,
        artifact_bucket=bucket,
        callback_sent=True,
    )
