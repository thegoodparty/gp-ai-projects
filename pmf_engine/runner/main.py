from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import tempfile
import time
import uuid

import boto3

from shared.braintrust import BraintrustClient
from shared.logger import get_logger
from shared.metrics import emit_metric
from .config import RunnerConfig
from .contract import validate_artifact_contract, ContractViolation
from .harness.base import AgentHarness

logger = get_logger(__name__)

_shutdown_requested = False
_current_task: "asyncio.Task | None" = None
_terminal_callback_sent = False

_VALIDATOR_SCRIPT = '''#!/usr/bin/env python3
"""Validate output artifact against the contract schema (+ optional constraints).
Usage: python3 /workspace/validate_output.py
Exits 0 on success, 1 on validation failure.

Thin shim: delegates to pmf_engine.runner.contract (installed on PYTHONPATH in
the Fargate container). Keeps one source of truth for validation logic.
"""
import json
import os
import sys
import glob

from pmf_engine.runner.contract import collect_contract_errors

_WORKSPACE = os.path.dirname(os.path.abspath(__file__))

files = sorted(glob.glob(os.path.join(_WORKSPACE, "output", "*.json")))
if not files:
    print(f"FAIL: No JSON files in {_WORKSPACE}/output/")
    sys.exit(1)

with open(os.path.join(_WORKSPACE, "contract_schema.json"), "rb") as _fh:
    schema = json.loads(_fh.read())

constraints = None
_constraints_path = os.path.join(_WORKSPACE, "contract_constraints.json")
if os.path.exists(_constraints_path):
    with open(_constraints_path, "rb") as _fh:
        constraints = json.loads(_fh.read())

exit_code = 0
for path in files:
    with open(path, "rb") as _fh:
        artifact_bytes = _fh.read()
    errors = collect_contract_errors(artifact_bytes, schema, constraints)
    if errors:
        print(f"FAIL: {path}")
        for err in errors[:30]:
            print(f"  {err}")
        if len(errors) > 30:
            print(f"  ... and {len(errors) - 30} more errors")
        exit_code = 1
    else:
        suffix = " (+constraints)" if constraints else ""
        print(f"PASS: {path} — all fields valid{suffix}")

sys.exit(exit_code)
'''


def get_harness(harness_name: str) -> AgentHarness:
    if harness_name == "claude_sdk":
        from .harness.claude_sdk import ClaudeSdkHarness
        return ClaudeSdkHarness()
    raise ValueError(f"Unknown harness: {harness_name}")


_TERMINAL_CALLBACK_STATUSES = {"success", "contract_violation", "failed"}
_CALLBACK_RETRY_DELAYS = (1, 3, 9)


def _mark_callback_sent() -> None:
    global _terminal_callback_sent
    _terminal_callback_sent = True


def _is_callback_already_sent() -> bool:
    return _terminal_callback_sent


def _reset_callback_marker() -> None:
    global _terminal_callback_sent
    _terminal_callback_sent = False


def _emit_bootstrap_failure_metric(reason: str, environment: str) -> None:
    try:
        cw = boto3.client("cloudwatch")
        cw.put_metric_data(
            Namespace="PMFEngine",
            MetricData=[
                {
                    "MetricName": "BootstrapFailure",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Environment", "Value": environment},
                        {"Name": "Reason", "Value": reason},
                    ],
                },
            ],
        )
    except Exception as metric_err:
        logger.warning(f"Failed to emit BootstrapFailure metric: {metric_err}")


def _emit_orphaned_callback_metric(config: RunnerConfig) -> None:
    try:
        cw = boto3.client("cloudwatch")
        cw.put_metric_data(
            Namespace="PMFEngine",
            MetricData=[
                {
                    "MetricName": "OrphanedCallback",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Environment", "Value": config.environment},
                        {"Name": "ExperimentId", "Value": config.experiment_id},
                    ],
                },
            ],
        )
    except Exception as metric_err:
        logger.warning(f"Failed to emit OrphanedCallback metric: {metric_err}")


def _send_callback(
    sqs_client,
    config: RunnerConfig,
    status: str,
    artifact_key: str = "",
    cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
    error: str | None = None,
) -> None:
    if not config.callback_queue_url:
        logger.warning("No callback queue URL configured, skipping callback")
        return

    body = {
        "experiment_id": config.experiment_id,
        "run_id": config.run_id,
        "candidate_id": config.candidate_id,
        "status": status,
        "artifact_key": artifact_key,
        "artifact_bucket": config.artifact_bucket,
        "cost_usd": cost_usd,
        "duration_seconds": duration_seconds,
        "error": error,
    }

    is_terminal = status in _TERMINAL_CALLBACK_STATUSES
    max_attempts = len(_CALLBACK_RETRY_DELAYS) if is_terminal else 1
    last_exc: Exception | None = None

    for attempt in range(max_attempts):
        try:
            sqs_client.send_message(
                QueueUrl=config.callback_queue_url,
                MessageBody=json.dumps(body),
                MessageGroupId=config.candidate_id or "bootstrap",
                MessageDeduplicationId=f"{config.run_id or 'bootstrap'}-{status}",
            )
            logger.info(f"Sent {status} callback for run {config.run_id}")
            return
        except Exception as e:
            last_exc = e
            if is_terminal and attempt < max_attempts - 1:
                delay = _CALLBACK_RETRY_DELAYS[attempt]
                logger.warning(
                    f"SQS send failed for {status} callback (run {config.run_id}, "
                    f"attempt {attempt + 1}/{max_attempts}): {e}. Retrying in {delay}s."
                )
                time.sleep(delay)
                continue
            break

    if is_terminal:
        logger.error(
            f"ORPHANED CALLBACK: Failed to send {status} callback after {max_attempts} attempts. "
            f"run_id={config.run_id} experiment_id={config.experiment_id} "
            f"candidate_id={config.candidate_id} artifact_bucket={config.artifact_bucket} "
            f"artifact_key={artifact_key or '(none)'} error={last_exc}"
        )
        _emit_orphaned_callback_metric(config)
        if last_exc is None:
            raise RuntimeError(
                f"BUG: {status} callback failed after {max_attempts} attempts "
                "but no exception was captured"
            )
        raise last_exc

    logger.warning(
        f"Failed to send non-terminal {status} callback for run {config.run_id}: {last_exc}"
    )


_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret[_-]?key|access[_-]?key|token|password|credential|auth)\s*[=:]\s*["\']?([A-Za-z0-9_\-/.+]{8,})["\']?'),
    re.compile(r'(?i)(sk-[a-zA-Z0-9]{20,})'),
    re.compile(r'(?i)(AKIA[0-9A-Z]{16})'),
    re.compile(r'(?i)(ghp_[A-Za-z0-9]{36,})'),
    re.compile(r'(?i)(xox[bpra]-[A-Za-z0-9\-]+)'),
]


def _redact_line(line: str) -> str:
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub(lambda m: m.group(0)[:8] + "***REDACTED***", line)
    return line


def _emit_session_redaction_failed_metric() -> None:
    emit_metric(
        namespace="PMFEngine",
        name="SessionRedactionFailed",
        dimensions={"Environment": os.environ.get("ENVIRONMENT", "dev")},
    )


def _redact_session_jsonl(source_path: str) -> str | None:
    try:
        fd, redacted_path = tempfile.mkstemp(suffix=".jsonl", prefix="session_redacted_")
        with open(source_path, "r", errors="replace") as src, os.fdopen(fd, "w") as dst:
            for line in src:
                dst.write(_redact_line(line))
        return redacted_path
    except Exception as e:
        logger.exception(f"Failed to redact session JSONL: {e}")
        _emit_session_redaction_failed_metric()
        return None


def _find_session_jsonl() -> str | None:
    claude_dir = os.path.expanduser("~/.claude")
    if not os.path.isdir(claude_dir):
        return None
    import glob as glob_mod
    candidates = glob_mod.glob(os.path.join(claude_dir, "projects", "**", "*.jsonl"), recursive=True)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


_SENSITIVE_PATTERNS = {".env", ".key", ".pem", ".crt", "credentials", "secret"}

_SAFE_TMP_EXTENSIONS = {
    ".json", ".csv", ".txt", ".md", ".log", ".html", ".pdf", ".xml", ".yaml", ".yml",
}


def _is_sensitive_file(filename: str) -> bool:
    lower = filename.lower()
    for pattern in _SENSITIVE_PATTERNS:
        if pattern in lower:
            return True
    return False


def _collect_files(
    root_dir: str,
    prefix: str,
    max_file_size: int = 50 * 1024 * 1024,
    max_total_size: int = 200 * 1024 * 1024,
    allowed_extensions: set[str] | None = None,
) -> list[tuple[str, str]]:
    collected = []
    total_size = 0
    if not os.path.isdir(root_dir):
        return collected
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if allowed_extensions is not None:
                _, ext = os.path.splitext(filename)
                if ext.lower() not in allowed_extensions:
                    continue
            elif _is_sensitive_file(filename):
                continue
            filepath = os.path.join(dirpath, filename)
            try:
                file_size = os.path.getsize(filepath)
                if file_size > max_file_size:
                    continue
                if total_size + file_size > max_total_size:
                    return collected
                relpath = os.path.relpath(filepath, root_dir)
                s3_key = f"{prefix}/{relpath}"
                collected.append((filepath, s3_key))
                total_size += file_size
            except OSError:
                continue
    return collected


def _emit_run_log_upload_failed_metric(config: RunnerConfig, failed_count: int) -> None:
    emit_metric(
        namespace="PMFEngine",
        name="RunLogUploadFailed",
        value=failed_count,
        dimensions={
            "Environment": config.environment,
            "ExperimentId": config.experiment_id,
        },
    )


def _upload_run_logs(s3_client, config: RunnerConfig, workspace_dir: str) -> None:
    prefix = f"{config.experiment_id}/{config.run_id}/logs"
    files_to_upload: list[tuple[str, str]] = []

    files_to_upload.extend(_collect_files(workspace_dir, f"{prefix}/workspace"))
    files_to_upload.extend(_collect_files(
        "/tmp", f"{prefix}/tmp", allowed_extensions=_SAFE_TMP_EXTENSIONS,
    ))

    session_file = _find_session_jsonl()
    if session_file:
        redacted = _redact_session_jsonl(session_file)
        if redacted:
            files_to_upload.append((redacted, f"{prefix}/session.jsonl"))
        else:
            logger.warning("Skipping session JSONL upload — redaction failed")

    uploaded = 0
    failed = 0
    for filepath, s3_key in files_to_upload:
        try:
            with open(filepath, "rb") as f:
                s3_client.put_object(
                    Bucket=config.artifact_bucket,
                    Key=s3_key,
                    Body=f.read(),
                    Tagging="lifecycle=logs",
                )
            uploaded += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Failed to upload {filepath}: {e}")

    total = len(files_to_upload)
    if total > 0 and failed == total:
        logger.error(
            f"ALL run log uploads failed ({failed}/{total}) for run {config.run_id} "
            f"to s3://{config.artifact_bucket}/{prefix}/ — diagnostics lost"
        )
        _emit_run_log_upload_failed_metric(config, failed)
    else:
        logger.info(
            f"Uploaded {uploaded}/{total} run logs to s3://{config.artifact_bucket}/{prefix}/"
        )


async def run_experiment(
    config: RunnerConfig,
    harness: AgentHarness | None = None,
    s3_client=None,
    sqs_client=None,
) -> None:
    if harness is None:
        harness = get_harness(config.harness)
    if s3_client is None:
        s3_client = boto3.client("s3")
    if sqs_client is None:
        sqs_client = boto3.client("sqs")

    bt = BraintrustClient.get_instance()
    bt.init("pmf-engine")

    artifact_key = config.resolve_artifact_key()
    workspace_dir = os.environ.get("WORKSPACE_DIR", "/workspace")
    start_time = time.monotonic()

    try:
        with bt.traced_span(
            name=f"experiment:{config.experiment_id}",
            input_data={
                "experiment_id": config.experiment_id,
                "run_id": config.run_id,
                "candidate_id": config.candidate_id,
                "model": config.model,
                "params": config.params,
            },
            tags=["pmf", config.experiment_id, config.model],
            metadata={"environment": config.environment},
        ) as span:
            try:
                result = await harness.run(
                    instruction=config.instruction,
                    model=config.model,
                    max_turns=config.max_turns,
                    workspace_dir=workspace_dir,
                    params=config.params,
                    contract_schema=config.contract_schema,
                    contract_constraints=config.contract_constraints,
                    parent_span=span,
                )
            except Exception as e:
                duration = time.monotonic() - start_time
                logger.exception(f"Harness failed for run {config.run_id}: {e}")
                _upload_run_logs(s3_client, config, workspace_dir)
                _send_callback(sqs_client, config, "failed", duration_seconds=duration, error=str(e))
                span.log(output={"status": "failed", "error": str(e), "duration_seconds": duration})
                _mark_callback_sent()
                raise

            try:
                validate_artifact_contract(
                    result.artifact_bytes,
                    config.contract_schema,
                    config.contract_constraints,
                )
            except ContractViolation as e:
                duration = time.monotonic() - start_time
                logger.error(f"Contract violation for run {config.run_id}: {e}")
                rejected_key = f"{config.experiment_id}/{config.run_id}/rejected.json"
                try:
                    s3_client.put_object(
                        Bucket=config.artifact_bucket,
                        Key=rejected_key,
                        Body=result.artifact_bytes,
                        ContentType=result.content_type,
                        Tagging="lifecycle=logs",
                    )
                except Exception as upload_err:
                    logger.warning(
                        f"Failed to upload rejected artifact for run {config.run_id}: {upload_err}"
                    )
                _upload_run_logs(s3_client, config, workspace_dir)
                _send_callback(
                    sqs_client, config, "contract_violation",
                    duration_seconds=duration,
                    error=f"{e} (rejected artifact: {rejected_key})",
                )
                _mark_callback_sent()
                span.log(output={
                    "status": "contract_violation",
                    "error": str(e),
                    "rejected_artifact_key": rejected_key,
                    "cost_usd": result.cost_usd,
                    "num_turns": result.num_turns,
                    "duration_seconds": duration,
                })
                return
            except Exception as e:
                duration = time.monotonic() - start_time
                logger.exception(f"Validator error for run {config.run_id}: {e}")
                _upload_run_logs(s3_client, config, workspace_dir)
                _send_callback(
                    sqs_client, config, "failed",
                    duration_seconds=duration, error=str(e),
                )
                span.log(output={"status": "failed", "error": str(e), "duration_seconds": duration})
                _mark_callback_sent()
                raise

            try:
                s3_client.put_object(
                    Bucket=config.artifact_bucket,
                    Key=artifact_key,
                    Body=result.artifact_bytes,
                    ContentType=result.content_type,
                )
                logger.info(f"Uploaded artifact to s3://{config.artifact_bucket}/{artifact_key}")

                latest_key = f"{config.experiment_id}/latest.json"
                try:
                    s3_client.put_object(
                        Bucket=config.artifact_bucket,
                        Key=latest_key,
                        Body=result.artifact_bytes,
                        ContentType=result.content_type,
                    )
                    logger.info(
                        f"Updated canonical latest pointer at s3://{config.artifact_bucket}/{latest_key}"
                    )
                except Exception as latest_err:
                    logger.warning(
                        f"Failed to update latest pointer {latest_key} for run {config.run_id}: {latest_err}"
                    )
            except Exception as e:
                duration = time.monotonic() - start_time
                logger.exception(f"S3 upload failed for run {config.run_id}: {e}")
                _upload_run_logs(s3_client, config, workspace_dir)
                _send_callback(sqs_client, config, "failed", duration_seconds=duration, error=str(e))
                span.log(output={"status": "failed", "error": str(e), "duration_seconds": duration})
                _mark_callback_sent()
                raise

            _upload_run_logs(s3_client, config, workspace_dir)

            duration = time.monotonic() - start_time
            _emit_run_metrics(config, result.cost_usd, duration, result.num_turns)
            try:
                _send_callback(
                    sqs_client,
                    config,
                    "success",
                    artifact_key=artifact_key,
                    cost_usd=result.cost_usd,
                    duration_seconds=duration,
                )
            finally:
                _mark_callback_sent()

            span.log(output={
                "status": "success",
                "cost_usd": result.cost_usd,
                "num_turns": result.num_turns,
                "duration_seconds": duration,
            })
    finally:
        bt.flush()


def _emit_run_metrics(config: RunnerConfig, cost_usd: float, duration_seconds: float, num_turns: int) -> None:
    try:
        cw = boto3.client("cloudwatch")
        env = config.environment
        dimensions = [
            {"Name": "Environment", "Value": env},
            {"Name": "ExperimentId", "Value": config.experiment_id},
        ]
        cw.put_metric_data(
            Namespace="PMFEngine",
            MetricData=[
                {
                    "MetricName": "RunCostUsd",
                    "Value": cost_usd,
                    "Unit": "None",
                    "Dimensions": dimensions,
                },
                {
                    "MetricName": "RunDurationSeconds",
                    "Value": duration_seconds,
                    "Unit": "Seconds",
                    "Dimensions": dimensions,
                },
                {
                    "MetricName": "RunTurns",
                    "Value": num_turns,
                    "Unit": "Count",
                    "Dimensions": dimensions,
                },
            ],
        )
    except Exception as e:
        logger.warning(f"Failed to emit run metrics: {e}")


def _handle_signal(signum, _frame=None):
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning(f"Received signal {signum}, cancelling experiment task")
    task = _current_task
    if task is not None and not task.done():
        task.cancel()


def _send_bootstrap_callback(error: str) -> None:
    callback_url = os.environ.get("CALLBACK_QUEUE_URL", "")
    run_id = os.environ.get("RUN_ID", "")
    if not callback_url or not run_id:
        logger.error(
            f"Cannot send bootstrap callback (missing CALLBACK_QUEUE_URL or RUN_ID): {error}"
        )
        return
    try:
        sqs = boto3.client("sqs")
        candidate_id = os.environ.get("CANDIDATE_ID", "")
        body = json.dumps({
            "experiment_id": os.environ.get("EXPERIMENT_ID", ""),
            "run_id": run_id,
            "candidate_id": candidate_id,
            "status": "failed",
            "artifact_key": "",
            "artifact_bucket": os.environ.get("ARTIFACT_BUCKET", ""),
            "cost_usd": 0.0,
            "duration_seconds": 0.0,
            "error": error,
        })
        sqs.send_message(
            QueueUrl=callback_url,
            MessageBody=body,
            MessageGroupId=candidate_id or "bootstrap",
            MessageDeduplicationId=f"{run_id}-bootstrap-failed",
        )
        logger.info(f"Sent bootstrap-failure callback for run {run_id}")
    except Exception as cb_err:
        logger.exception(f"Failed to send bootstrap-failure callback: {cb_err}")


async def main():
    global _current_task, _shutdown_requested
    _current_task = None
    _shutdown_requested = False
    _reset_callback_marker()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: _handle_signal(signal.SIGTERM))
        loop.add_signal_handler(signal.SIGINT, lambda: _handle_signal(signal.SIGINT))
    except (NotImplementedError, RuntimeError, ValueError):
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    try:
        config = RunnerConfig.from_env()
    except Exception as e:
        environment = os.environ.get("ENVIRONMENT", "dev")
        logger.exception(f"Failed to load RunnerConfig from env: {e}")
        _emit_bootstrap_failure_metric("ConfigLoad", environment)
        _send_bootstrap_callback(f"Config load failed: {e}")
        sys.exit(1)

    timeout = config.timeout_seconds

    try:
        sqs_client = boto3.client("sqs")
    except Exception as e:
        logger.exception(f"Failed to create SQS client: {e}")
        _emit_bootstrap_failure_metric("SQSClientInit", config.environment)
        sys.exit(1)

    workspace_dir = os.environ.get("WORKSPACE_DIR", "/workspace")

    try:
        if not config.experiment_id:
            logger.error("EXPERIMENT_ID environment variable required")
            _send_callback(sqs_client, config, "failed", error="EXPERIMENT_ID not set")
            sys.exit(1)

        if not config.instruction:
            logger.error("No instruction available (not in env var or registry)")
            _send_callback(sqs_client, config, "failed", error="No instruction available")
            sys.exit(1)

        os.makedirs(workspace_dir, exist_ok=True)
        os.makedirs(os.path.join(workspace_dir, "output"), exist_ok=True)

        instruction_path = os.path.join(workspace_dir, "instruction.md")
        with open(instruction_path, "w") as f:
            f.write(config.instruction)
        logger.info(f"Wrote instruction to {instruction_path}")

        if config.contract_schema:
            schema_path = os.path.join(workspace_dir, "contract_schema.json")
            with open(schema_path, "w") as f:
                json.dump(config.contract_schema, f, indent=2)

            if config.contract_constraints:
                constraints_path = os.path.join(workspace_dir, "contract_constraints.json")
                with open(constraints_path, "w") as f:
                    json.dump(config.contract_constraints, f, indent=2)
                logger.info(f"Wrote contract constraints to {constraints_path}")

            validator_path = os.path.join(workspace_dir, "validate_output.py")
            with open(validator_path, "w") as f:
                f.write(_VALIDATOR_SCRIPT)
            logger.info(f"Wrote contract validator to {validator_path}")

        _current_task = asyncio.ensure_future(run_experiment(config))
        await asyncio.wait_for(_current_task, timeout=timeout)

    except asyncio.TimeoutError:
        logger.error(f"Experiment timed out after {timeout}s for run {config.run_id}")
        if _current_task is not None and not _current_task.done():
            _current_task.cancel()
            try:
                await _current_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            _upload_run_logs(boto3.client("s3"), config, workspace_dir)
        except Exception as log_err:
            logger.warning(f"Failed to upload run logs on timeout: {log_err}")
        _send_callback(
            sqs_client, config, "failed",
            duration_seconds=config.timeout_seconds,
            error=f"Experiment timed out after {config.timeout_seconds}s",
        )
        sys.exit(1)

    except asyncio.CancelledError:
        logger.warning(f"Experiment task cancelled by signal for run {config.run_id}")
        try:
            _upload_run_logs(boto3.client("s3"), config, workspace_dir)
        except Exception as log_err:
            logger.warning(f"Failed to upload run logs after cancel: {log_err}")
        _send_callback(
            sqs_client, config, "failed",
            duration_seconds=0,
            error="Task terminated by signal",
        )
        sys.exit(1)

    except SystemExit:
        raise

    except Exception as e:
        if _shutdown_requested:
            logger.warning(f"Task terminated by signal for run {config.run_id}")
            _send_callback(
                sqs_client, config, "failed",
                duration_seconds=0,
                error="Task terminated by signal",
            )
            sys.exit(1)

        logger.exception(f"Unhandled error in main for run {config.run_id}: {e}")
        if not _is_callback_already_sent():
            _send_callback(
                sqs_client, config, "failed",
                duration_seconds=0,
                error=f"Unhandled error: {e}",
            )
        raise

    logger.info(f"Experiment {config.experiment_id} run {config.run_id} completed")


if __name__ == "__main__":
    asyncio.run(main())
