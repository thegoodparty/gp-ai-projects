from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import tempfile
import time

from shared.braintrust import BraintrustClient
from shared.logger import get_logger
from .config import RunnerConfig, BrokerUrlSchemeError, validate_broker_url_scheme
from .contract import validate_artifact_contract, ContractViolation
from .harness.base import AgentHarness
from .pmf_runtime import publish
from .pmf_runtime.config import init_config

logger = get_logger(__name__)

_shutdown_requested = False
_current_task: "asyncio.Task | None" = None
_terminal_callback_sent = False

# Files the runner writes itself during workspace setup. An attachment that
# matches one of these names would silently clobber the runner's own write.
# The publisher rejects these at upload time; this set is the runtime
# defense-in-depth guard.
_RESERVED_WORKSPACE_FILES = frozenset({
    "instruction.md",
    "contract_schema.json",
    "validate_output.py",
})


class AttachmentSafetyViolation(RuntimeError):
    """Raised when an attachment write would clobber a reserved file, escape
    the workspace, or otherwise violate the runner's path-safety guarantees.

    Distinct subclass of RuntimeError so the outer error handler surfaces
    reason_code='AttachmentSafetyViolation' to gp-api — operators get a
    greppable, alertable error type instead of a generic 'RuntimeError'.
    """

_VALIDATOR_SCRIPT = '''#!/usr/bin/env python3
"""Validate output artifact against the contract schema.
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

exit_code = 0
for path in files:
    with open(path, "rb") as _fh:
        artifact_bytes = _fh.read()
    errors = collect_contract_errors(artifact_bytes, schema)
    if errors:
        print(f"FAIL: {path}")
        for err in errors[:30]:
            print(f"  {err}")
        if len(errors) > 30:
            print(f"  ... and {len(errors) - 30} more errors")
        exit_code = 1
    else:
        print(f"PASS: {path} — all fields valid")

sys.exit(exit_code)
'''


def get_harness(harness_name: str) -> AgentHarness:
    if harness_name == "claude_sdk":
        from .harness.claude_sdk import ClaudeSdkHarness
        return ClaudeSdkHarness()
    raise ValueError(f"Unknown harness: {harness_name}")


def _mark_callback_sent() -> None:
    global _terminal_callback_sent
    _terminal_callback_sent = True


def _is_callback_already_sent() -> bool:
    return _terminal_callback_sent


def _reset_callback_marker() -> None:
    global _terminal_callback_sent
    _terminal_callback_sent = False


def _send_failed_to_sqs_directly(
    *,
    run_id: str,
    experiment_id: str,
    reason_code: str,
    detail: str,
) -> bool:
    """Last-resort failed callback when the broker is unreachable.

    Posts the same envelope shape the broker would post to RESULTS_QUEUE_URL.
    Returns True if SQS accepted the message.

    The runner in v2 deliberately has no AWS task-role permissions — all I/O
    goes through the broker. If/when ops decides to plug a narrow SQS:SendMessage
    permission into the runner task role and wire RESULTS_QUEUE_URL onto the
    task definition, this helper will deliver a terminal status and prevent
    PENDING-forever hangs when the broker can't be reached during init. Until
    then, the helper still runs but no-ops cleanly (returns False), keeping
    the entrypoint's exit path identical regardless of wiring state.
    """
    queue_url = os.environ.get("RESULTS_QUEUE_URL", "").strip()
    if not queue_url:
        return False
    try:
        import boto3
        sqs = boto3.client("sqs")
        body = {
            "run_id": run_id,
            "experiment_id": experiment_id,
            "status": "failed",
            "reason_code": reason_code,
            "detail": detail,
        }
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(body),
            MessageGroupId=run_id or "unknown",
            MessageDeduplicationId=f"failed-{run_id}-{reason_code}",
        )
        return True
    except Exception:
        logger.exception(
            "last_resort_sqs_send_failed run_id=%s experiment_id=%s",
            run_id, experiment_id,
        )
        return False


def _exit_on_pre_task_shutdown(
    *,
    run_id: str,
    experiment_id: str,
    main_start_time: float,
    broker_initialized: bool,
) -> None:
    """If a SIGTERM/SIGINT arrived during pre-task setup, exit cleanly with a
    failed-status callback BEFORE wasting an ECS task slot launching the
    agent. Returns silently if no shutdown was requested.
    """
    if not _shutdown_requested:
        return
    duration = time.monotonic() - main_start_time
    detail = "Task terminated by signal during init"
    logger.info(
        "pre_task_shutdown_requested run_id=%s experiment_id=%s",
        run_id, experiment_id,
    )
    if broker_initialized:
        try:
            publish.report_status(
                "failed",
                reason_code="Signal",
                detail=detail,
                duration_seconds=duration,
            )
        except Exception:
            logger.exception(
                "pre_task_shutdown_callback_failed run_id=%s", run_id,
            )
            _send_failed_to_sqs_directly(
                run_id=run_id,
                experiment_id=experiment_id,
                reason_code="Signal",
                detail=detail,
            )
    else:
        _send_failed_to_sqs_directly(
            run_id=run_id,
            experiment_id=experiment_id,
            reason_code="Signal",
            detail=detail,
        )
    sys.exit(1)


_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret[_-]?key|access[_-]?key|token|password|credential|auth)\s*[=:]\s*["\']?([A-Za-z0-9_\-/.+]{8,})["\']?'),
    re.compile(r'(?i)(sk-[a-zA-Z0-9]{20,})'),
    re.compile(r'(?i)(AKIA[0-9A-Z]{16})'),
    re.compile(r'(?i)(ghp_[A-Za-z0-9]{36,})'),
    re.compile(r'(?i)(xox[bpra]-[A-Za-z0-9\-]+)'),
]

# Bearer-token redaction. `Authorization: Bearer <token>` doesn't match the
# key=value/key:value shape above (the space between "Bearer" and the token
# isn't in `[=:]`), so a BROKER_TOKEN that the Claude SDK serializes into its
# session JSONL (~/.claude/projects/**/*.jsonl) would otherwise land in S3
# unredacted through _upload_logs. Captured separately so the substitution
# can preserve the "Bearer " prefix (diagnostic value) while redacting only
# the secret portion.
_BEARER_TOKEN_PATTERN = re.compile(r'(?i)(Bearer\s+)([A-Za-z0-9_\-/.+=]{8,})')


def _redact_line(line: str) -> str:
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub(lambda m: m.group(0)[:8] + "***REDACTED***", line)
    line = _BEARER_TOKEN_PATTERN.sub(lambda m: m.group(1) + "***REDACTED***", line)
    return line


def _redact_session_jsonl(source_path: str) -> str | None:
    try:
        fd, redacted_path = tempfile.mkstemp(suffix=".jsonl", prefix="session_redacted_")
        with open(source_path, "r", errors="replace") as src, os.fdopen(fd, "w") as dst:
            for line in src:
                dst.write(_redact_line(line))
        return redacted_path
    except Exception as e:
        logger.exception(f"Failed to redact session JSONL: {e}")
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


def _collect_workspace_files(
    root_dir: str,
    max_file_size: int = 50 * 1024 * 1024,
    max_total_size: int = 200 * 1024 * 1024,
    allowed_extensions: set[str] | None = None,
) -> dict[str, bytes]:
    collected: dict[str, bytes] = {}
    total_size = 0
    if not os.path.isdir(root_dir):
        return collected
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if _is_sensitive_file(filename):
                continue
            if allowed_extensions is not None:
                _, ext = os.path.splitext(filename)
                if ext.lower() not in allowed_extensions:
                    continue
            filepath = os.path.join(dirpath, filename)
            try:
                file_size = os.path.getsize(filepath)
                if file_size > max_file_size:
                    continue
                if total_size + file_size > max_total_size:
                    return collected
                relpath = os.path.relpath(filepath, root_dir)
                with open(filepath, "rb") as f:
                    collected[f"workspace/{relpath}"] = f.read()
                total_size += file_size
            except OSError:
                continue
    return collected


def _collect_log_files(workspace_dir: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    files.update(_collect_workspace_files(workspace_dir))
    files.update(_collect_workspace_files(
        "/tmp", allowed_extensions=_SAFE_TMP_EXTENSIONS,
    ))

    session_file = _find_session_jsonl()
    if session_file:
        redacted = _redact_session_jsonl(session_file)
        if redacted:
            try:
                with open(redacted, "rb") as f:
                    files["session.jsonl"] = f.read()
            except OSError:
                pass
        else:
            logger.warning("Skipping session JSONL upload — redaction failed")

    return files


def _upload_logs(workspace_dir: str, *, run_id: str, experiment_id: str) -> None:
    try:
        files = _collect_log_files(workspace_dir)
        if files:
            publish.upload_logs(files)
            logger.info(
                "log upload ok run_id=%s experiment_id=%s files=%d",
                run_id, experiment_id, len(files),
            )
    except Exception as e:
        # Swallow intentionally — terminal callback is more important than logs.
        # But upgrade context: run_id + experiment_id for correlation, exc_type
        # for alerting grep patterns, exc_info=True so the stack trace isn't lost.
        logger.warning(
            "log upload failed run_id=%s experiment_id=%s exc_type=%s: %s",
            run_id, experiment_id, type(e).__name__, e,
            exc_info=True,
        )


async def run_experiment(
    config: RunnerConfig,
    harness: AgentHarness | None = None,
) -> None:
    if harness is None:
        harness = get_harness(config.harness)

    bt = BraintrustClient.get_instance()
    bt.init("pmf-engine")

    workspace_dir = os.environ.get("WORKSPACE_DIR", "/workspace")
    start_time = time.monotonic()

    try:
        with bt.traced_span(
            name=f"experiment:{config.experiment_id}",
            input_data={
                "experiment_id": config.experiment_id,
                "run_id": config.run_id,
                "organization_slug": config.organization_slug,
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
                    parent_span=span,
                    experiment_id=config.experiment_id,
                    system_prompt=config.system_prompt,
                    permission_mode=config.permission_mode,
                    allowed_external_tools=config.allowed_external_tools,
                )
            except Exception as e:
                duration = time.monotonic() - start_time
                logger.exception(f"Harness failed for run {config.run_id}: {e}")
                _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)
                publish.report_status(
                    "failed",
                    reason_code=type(e).__name__,
                    detail=str(e),
                    duration_seconds=duration,
                )
                span.log(output={"status": "failed", "error": str(e), "duration_seconds": duration})
                _mark_callback_sent()
                raise

            try:
                validate_artifact_contract(
                    result.artifact_bytes,
                    config.contract_schema,
                )
            except ContractViolation as e:
                duration = time.monotonic() - start_time
                logger.error(f"Contract violation for run {config.run_id}: {e}")
                _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)
                # ContractViolation fires for Invalid-JSON too, so json.loads
                # would JSONDecodeError — silently skipping the callback and
                # leaving the run PENDING forever. Preserve the raw bytes
                # instead when we can't re-parse. Coerce to bytes first so
                # None / str / other types don't crash the fallback itself.
                raw = result.artifact_bytes or b""
                if isinstance(raw, str):
                    raw = raw.encode("utf-8", errors="replace")
                try:
                    rejected = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, ValueError, TypeError):
                    rejected = {
                        "_raw_bytes": raw[:4096].decode("utf-8", errors="replace"),
                        "_truncated": len(raw) > 4096,
                    }
                if not isinstance(rejected, dict):
                    rejected = {"_raw_bytes": str(rejected)[:4096], "_truncated": False}
                if "_raw_bytes" not in rejected and not raw:
                    # None / empty bytes case — preserve the empty marker
                    # for downstream tooling that branches on truncation.
                    rejected = {"_raw_bytes": "", "_truncated": False}
                publish.report_status(
                    "contract_violation",
                    rejected_artifact=rejected,
                    detail=str(e),
                    duration_seconds=duration,
                    cost_usd=result.cost_usd,
                )
                _mark_callback_sent()
                span.log(output={
                    "status": "contract_violation",
                    "error": str(e),
                    "cost_usd": result.cost_usd,
                    "num_turns": result.num_turns,
                    "duration_seconds": duration,
                })
                return
            except Exception as e:
                duration = time.monotonic() - start_time
                logger.exception(f"Validator error for run {config.run_id}: {e}")
                _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)
                publish.report_status(
                    "failed",
                    reason_code=type(e).__name__,
                    detail=str(e),
                    duration_seconds=duration,
                    cost_usd=result.cost_usd,
                )
                span.log(output={"status": "failed", "error": str(e), "duration_seconds": duration})
                _mark_callback_sent()
                raise

            try:
                artifact = json.loads(result.artifact_bytes)
            except (json.JSONDecodeError, TypeError) as e:
                duration = time.monotonic() - start_time
                logger.exception(f"Artifact not valid JSON for run {config.run_id}: {e}")
                _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)
                publish.report_status(
                    "failed",
                    reason_code="InvalidJSON",
                    detail=str(e),
                    duration_seconds=duration,
                    cost_usd=result.cost_usd,
                )
                _mark_callback_sent()
                raise

            _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)

            duration = time.monotonic() - start_time
            try:
                publish.publish(
                    artifact,
                    duration_seconds=duration,
                    cost_usd=result.cost_usd,
                )
                _mark_callback_sent()
                logger.info(f"Published artifact via broker for run {config.run_id}")
            except Exception as e:
                # Mark callback sent ONLY after report_status returns successfully.
                # If report_status itself raises (e.g. broker is fully down),
                # leave the marker False so main()'s outer handler still gets a
                # chance to send a fallback terminal callback. Eagerly setting
                # the marker via finally:_mark_callback_sent() would skip that
                # fallback and leave the run PENDING forever in gp-api.
                logger.exception(f"Broker publish failed for run {config.run_id}: {e}")
                try:
                    publish.report_status(
                        "failed",
                        reason_code="PublishFailed",
                        detail=str(e),
                        duration_seconds=duration,
                        cost_usd=result.cost_usd,
                    )
                    _mark_callback_sent()
                except Exception as report_err:
                    logger.exception(
                        f"report_status during publish-failure handling also failed "
                        f"for run {config.run_id}: {report_err}"
                    )
                raise

            span.log(output={
                "status": "success",
                "cost_usd": result.cost_usd,
                "num_turns": result.num_turns,
                "duration_seconds": duration,
            })
    finally:
        bt.flush()


def _handle_signal(signum, _frame=None):
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning(f"Received signal {signum}, cancelling experiment task")
    task = _current_task
    if task is not None and not task.done():
        task.cancel()


async def main():
    global _current_task, _shutdown_requested
    _current_task = None
    _shutdown_requested = False
    _reset_callback_marker()
    main_start_time = time.monotonic()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: _handle_signal(signal.SIGTERM))
        loop.add_signal_handler(signal.SIGINT, lambda: _handle_signal(signal.SIGINT))
    except (NotImplementedError, RuntimeError, ValueError):
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    # Raw env reads first so we can init the broker before from_env() — the
    # broker must be reachable to surface config/manifest failures back to
    # gp-api as a proper "failed" callback rather than an opaque PENDING hang.
    raw_broker_url = os.environ.get("BROKER_URL", "").strip()
    raw_broker_token = os.environ.get("BROKER_TOKEN", "").strip()
    raw_run_id = os.environ.get("RUN_ID", "").strip()
    raw_experiment_id = os.environ.get("EXPERIMENT_ID", "").strip()
    raw_environment = os.environ.get("ENVIRONMENT", "dev")

    # Scheme guard runs BEFORE init_config — a misconfigured plaintext URL
    # (e.g. http://broker.example.test in prod) must never get baked into
    # the broker client. Without this, the failed-callback fires over an
    # unencrypted channel.
    try:
        validate_broker_url_scheme(raw_broker_url, raw_environment)
    except BrokerUrlSchemeError as e:
        logger.exception(
            "broker_url_scheme_invalid errorType=BrokerUrlSchemeError "
            f"run_id={raw_run_id} experiment_id={raw_experiment_id} error={e}"
        )
        _send_failed_to_sqs_directly(
            run_id=raw_run_id,
            experiment_id=raw_experiment_id,
            reason_code="BrokerUrlSchemeError",
            detail=str(e),
        )
        sys.exit(1)

    broker_initialized = False
    if raw_broker_url and raw_broker_token:
        try:
            init_config(raw_broker_url, raw_broker_token)
            broker_initialized = True
        except Exception as e:
            logger.exception(
                "broker_init_failed errorType=BrokerInitError "
                f"run_id={raw_run_id} experiment_id={raw_experiment_id} error={e}"
            )

    _exit_on_pre_task_shutdown(
        run_id=raw_run_id,
        experiment_id=raw_experiment_id,
        main_start_time=main_start_time,
        broker_initialized=broker_initialized,
    )

    try:
        config = RunnerConfig.from_env()
    except Exception as e:
        error_type = type(e).__name__
        logger.exception(
            "runner_config_load_failed "
            f"errorType={error_type} run_id={raw_run_id} "
            f"experiment_id={raw_experiment_id} error={e}"
        )
        callback_sent = False
        if broker_initialized:
            try:
                publish.report_status(
                    "failed",
                    reason_code=error_type,
                    detail=f"Runner config load failed: {e}",
                    duration_seconds=time.monotonic() - main_start_time,
                )
                callback_sent = True
            except Exception as report_err:
                logger.exception(
                    "failed_callback_send_failed errorType=ReportStatusError "
                    f"run_id={raw_run_id} error={report_err}"
                )
        # C2 fallback: if the broker channel never came up (or the callback
        # itself failed), post the failed envelope DIRECTLY to RESULTS_QUEUE_URL
        # so the run row in gp-api flips PENDING → FAILED instead of hanging
        # forever. The helper no-ops cleanly when RESULTS_QUEUE_URL isn't
        # plumbed (current default), so this is safe to always attempt.
        if not callback_sent:
            _send_failed_to_sqs_directly(
                run_id=raw_run_id,
                experiment_id=raw_experiment_id,
                reason_code=error_type,
                detail=f"Runner config load failed: {e}",
            )
        sys.exit(1)

    if not broker_initialized:
        try:
            init_config(config.broker_url, config.broker_token)
        except Exception as e:
            logger.exception(
                "broker_init_failed errorType=BrokerInitError "
                f"run_id={config.run_id} experiment_id={config.experiment_id} error={e}"
            )
            _send_failed_to_sqs_directly(
                run_id=config.run_id,
                experiment_id=config.experiment_id,
                reason_code="BrokerInitError",
                detail=f"Broker init failed: {e}",
            )
            sys.exit(1)

    _exit_on_pre_task_shutdown(
        run_id=config.run_id,
        experiment_id=config.experiment_id,
        main_start_time=main_start_time,
        broker_initialized=True,
    )

    timeout = config.timeout_seconds
    workspace_dir = os.environ.get("WORKSPACE_DIR", "/workspace")

    try:
        if not config.experiment_id:
            logger.error("EXPERIMENT_ID environment variable required")
            publish.report_status(
                "failed",
                reason_code="MissingConfig",
                detail="EXPERIMENT_ID not set",
                duration_seconds=time.monotonic() - main_start_time,
            )
            sys.exit(1)

        if not config.instruction:
            logger.error("No instruction available (broker fetch failed and INSTRUCTION env var not set)")
            publish.report_status(
                "failed",
                reason_code="MissingConfig",
                detail="No instruction available",
                duration_seconds=time.monotonic() - main_start_time,
            )
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

            validator_path = os.path.join(workspace_dir, "validate_output.py")
            with open(validator_path, "w") as f:
                f.write(_VALIDATOR_SCRIPT)
            logger.info(f"Wrote contract validator to {validator_path}")

        # Defense in depth: the publisher and broker both validate attachment
        # names, but we double-check here because the workspace is a real
        # filesystem and a malformed broker response could otherwise write
        # outside /workspace/ or clobber files the runner just wrote.
        workspace_real = os.path.realpath(workspace_dir)
        for name, body in (config.attachments or {}).items():
            if name in _RESERVED_WORKSPACE_FILES:
                logger.error(
                    "attachment_safety_violation errorType=%s "
                    "run_id=%s experiment_id=%s basename=%r",
                    "reserved_basename", config.run_id, config.experiment_id, name,
                )
                raise AttachmentSafetyViolation(
                    f"attachment {name!r} collides with reserved basename"
                )
            if name != os.path.basename(name) or name.startswith("/") or ".." in name.split("/"):
                logger.error(
                    "attachment_safety_violation errorType=%s "
                    "run_id=%s experiment_id=%s basename=%r",
                    "unsafe_basename", config.run_id, config.experiment_id, name,
                )
                raise AttachmentSafetyViolation(
                    f"attachment {name!r} has an unsafe basename"
                )
            attachment_path = os.path.realpath(os.path.join(workspace_dir, name))
            # Realpath containment check: even after the basename guard, a
            # symlinked workspace_dir or future validator drift could let
            # the path leave /workspace/. Catch it before we open.
            if not (
                attachment_path == workspace_real
                or attachment_path.startswith(workspace_real + os.sep)
            ):
                logger.error(
                    "attachment_safety_violation errorType=%s "
                    "run_id=%s experiment_id=%s basename=%r resolved=%r",
                    "path_escape", config.run_id, config.experiment_id, name,
                    attachment_path,
                )
                raise AttachmentSafetyViolation(
                    f"attachment path {attachment_path!r} escapes workspace {workspace_real!r}"
                )
            # Exclusive-create + explicit utf-8: never silently clobber an
            # existing file (FileExistsError), never trust the locale's
            # default encoding (UnicodeEncodeError on locale-C with non-ASCII).
            with open(attachment_path, "x", encoding="utf-8") as f:
                f.write(body)
            logger.info("Wrote attachment to %s (%d bytes)", attachment_path, len(body))

        _current_task = asyncio.ensure_future(run_experiment(config))
        await asyncio.wait_for(_current_task, timeout=timeout)

    except asyncio.TimeoutError:
        logger.error(f"Experiment timed out after {timeout}s for run {config.run_id}")
        if _current_task is not None and not _current_task.done():
            _current_task.cancel()
            try:
                await _current_task
            except asyncio.CancelledError:
                pass
            except Exception as drain_err:
                logger.warning(
                    f"Error draining cancelled task for run {config.run_id}: "
                    f"{drain_err}",
                    exc_info=True,
                )
        _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)
        publish.report_status(
            "failed",
            reason_code="Timeout",
            detail=f"Experiment timed out after {config.timeout_seconds}s",
            duration_seconds=time.monotonic() - main_start_time,
        )
        sys.exit(1)

    except asyncio.CancelledError:
        logger.warning(f"Experiment task cancelled by signal for run {config.run_id}")
        _upload_logs(workspace_dir, run_id=config.run_id, experiment_id=config.experiment_id)
        publish.report_status(
            "failed",
            reason_code="Signal",
            detail="Task terminated by signal",
            duration_seconds=time.monotonic() - main_start_time,
        )
        sys.exit(1)

    except SystemExit:
        raise

    except Exception as e:
        if _shutdown_requested:
            logger.warning(f"Task terminated by signal for run {config.run_id}")
            publish.report_status(
                "failed",
                reason_code="Signal",
                detail="Task terminated by signal",
                duration_seconds=time.monotonic() - main_start_time,
            )
            sys.exit(1)

        logger.exception(f"Unhandled error in main for run {config.run_id}: {e}")
        if not _is_callback_already_sent():
            publish.report_status(
                "failed",
                reason_code=type(e).__name__,
                detail=f"Unhandled error: {e}",
                duration_seconds=time.monotonic() - main_start_time,
            )
        raise

    logger.info(f"Experiment {config.experiment_id} run {config.run_id} completed")


if __name__ == "__main__":
    asyncio.run(main())
