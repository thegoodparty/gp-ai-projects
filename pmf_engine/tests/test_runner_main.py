import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.harness.base import HarnessResult
from pmf_engine.runner.main import run_experiment, get_harness, main


class Clock:
    """Controllable monotonic-clock fake.

    Replaces the brittle `iter([100.0, 130.0, 130.0, 130.0])` pattern that
    silently masked drift when a refactor added one more `time.monotonic()`
    call (StopIteration → fallback default → test still passes for the
    wrong reason).

    Usage:
        clock = Clock(start=100.0)
        with patch("pmf_engine.runner.main.time.monotonic", clock.now):
            # tick the clock manually between assertions
            clock.advance(30.0)
            ...

    Each `.now()` returns the *current* cursor — no consumption, no
    StopIteration. The caller controls all advancement explicitly via
    `.advance(seconds)`.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _make_config(**overrides):
    defaults = {
        "experiment_id": "hello_world",
        "run_id": "run-001",
        "organization_slug": "org-123",
        "instruction": "Write result.json",
        "params": {},
        "harness": "claude_sdk",
        "model": "sonnet",
        "environment": "dev",
        "broker_url": "https://broker.test",
        "broker_token": "tok-test",
    }
    defaults.update(overrides)
    return RunnerConfig(**defaults)


class TestUploadLogsObservability:
    """R8 fix: `_upload_logs` used to swallow failures silently with a WARN
    log that had no run_id, no experiment_id, and no stack trace. On a
    terminal failure path, the operator has no way to debug the failed run
    if the log-upload itself also failed — double blind spot.

    Fix: require `run_id` + `experiment_id` kwargs, log exc_type + exc_info,
    keep the swallow (terminal callback is more important than logs) but
    make it observable.
    """

    def test_upload_logs_failure_warns_with_run_id_experiment_id_and_stacktrace(
        self, tmp_path
    ):
        """`shared.logger` disables propagation (propagate=False), so pytest's
        caplog can't see these records via the root logger. Attach a
        BufferingHandler to the specific logger instead.
        """
        import logging
        from pmf_engine.runner.main import _upload_logs
        import pmf_engine.runner.main as _main_mod

        workspace = tmp_path
        (workspace / "conversation.jsonl").write_text('{"type":"assistant"}\n')

        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture(level=logging.WARNING)
        _main_mod.logger.addHandler(handler)
        try:
            with patch(
                "pmf_engine.runner.main.publish.upload_logs",
                side_effect=RuntimeError("broker 503 from upload_logs"),
            ):
                # MUST NOT raise — log-upload failure is swallowed.
                _upload_logs(
                    str(workspace),
                    run_id="run-upload-obs-001",
                    experiment_id="smoke_test",
                )
        finally:
            _main_mod.logger.removeHandler(handler)

        warns = [
            r for r in captured
            if r.levelno >= logging.WARNING and "upload" in r.getMessage().lower()
        ]
        assert warns, (
            f"expected log-upload failure to warn from pmf_engine.runner.main.logger; "
            f"got: {[(r.levelname, r.getMessage()) for r in captured]}"
        )
        msg = warns[0].getMessage()
        assert "run-upload-obs-001" in msg, f"expected run_id in log, got: {msg}"
        assert "smoke_test" in msg, f"expected experiment_id in log, got: {msg}"
        assert "RuntimeError" in msg, f"expected exc_type in log, got: {msg}"
        assert warns[0].exc_info is not None, "expected exc_info=True so stacktrace is preserved"


def test_get_harness_raises_on_unknown():
    with pytest.raises(ValueError, match="Unknown harness: nonexistent"):
        get_harness("nonexistent")


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_publishes_artifact_via_broker(mock_publish, _mock_logs):
    config = _make_config()
    fake_result = HarnessResult(
        artifact_bytes=b'{"greeting": "hello"}',
        content_type="application/json",
        cost_usd=0.05,
        num_turns=3,
        session_id="sess-abc",
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result

    await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_called_once()
    call_args = mock_publish.publish.call_args
    assert call_args[0] == ({"greeting": "hello"},)
    assert call_args.kwargs.get("cost_usd") == pytest.approx(0.05)
    assert "duration_seconds" in call_args.kwargs


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_reports_failed_on_harness_failure(mock_publish, _mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = RuntimeError("Agent crashed")

    with pytest.raises(RuntimeError, match="Agent crashed"):
        await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_not_called()
    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "failed"
    assert call_args[1]["detail"] == "Agent crashed"
    assert call_args[1]["reason_code"] == "RuntimeError"


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_reports_failed_on_publish_failure(mock_publish, _mock_logs):
    config = _make_config()
    fake_result = HarnessResult(
        artifact_bytes=b'{"data": "ok"}',
        content_type="application/json",
        cost_usd=0.01,
        num_turns=1,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result
    mock_publish.publish.side_effect = Exception("Broker down")

    with pytest.raises(Exception, match="Broker down"):
        await run_experiment(config, harness=mock_harness)

    assert mock_publish.report_status.call_count == 1
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "failed"
    assert "PublishFailed" in call_args[1]["reason_code"]


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_publish_failure_then_report_status_failure_leaves_callback_unsent(
    mock_publish, _mock_logs
):
    """Double-failure path: broker.publish raises, then broker.report_status
    also raises. The terminal-callback marker MUST stay False so the outer
    main() handler can attempt its own fallback callback. If the marker is set
    eagerly via finally, main()'s `if not _is_callback_already_sent()` skips —
    and the run goes PENDING forever in gp-api with no terminal status."""
    from pmf_engine.runner.main import _is_callback_already_sent, _reset_callback_marker

    _reset_callback_marker()
    config = _make_config()
    fake_result = HarnessResult(
        artifact_bytes=b'{"data": "ok"}',
        content_type="application/json",
        cost_usd=0.01,
        num_turns=1,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result
    mock_publish.publish.side_effect = Exception("Broker down")
    mock_publish.report_status.side_effect = Exception("Broker still down")

    with pytest.raises(Exception, match="Broker down"):
        await run_experiment(config, harness=mock_harness)

    assert _is_callback_already_sent() is False, (
        "Marker must remain False when report_status itself failed — "
        "otherwise main()'s fallback handler skips and the run is stuck PENDING."
    )


@pytest.mark.asyncio
async def test_main_writes_instruction_to_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(instruction="# Test Instruction\n\nDo the thing.")

        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish"):
                        with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock):
                            await main()

        instruction_path = os.path.join(tmpdir, "instruction.md")
        assert os.path.exists(instruction_path)
        with open(instruction_path) as f:
            assert f.read() == "# Test Instruction\n\nDo the thing."


# ---------------------------------------------------------------------------
# Attachments — runner writes sidecar files to /workspace/ before agent spawn
# ---------------------------------------------------------------------------
#
# Contract: every entry in config.attachments lands as /workspace/<basename>
# *before* run_experiment is invoked, so the agent can read it from turn 1.
# The runner also re-checks the safety constraints the publisher/broker
# already enforced — defense in depth, because the workspace is a real
# filesystem.


@pytest.mark.asyncio
async def test_main_writes_attachments_to_workspace_before_running_experiment():
    """Pin strict ordering: attachments land on disk AFTER from_env returns
    but BEFORE run_experiment is invoked. Use two snapshot hooks:

      1. from_env side_effect — fires before the workspace-setup section
         runs. Attachments must NOT yet be on disk.
      2. run_experiment side_effect — fires after the workspace-setup
         section finishes. Attachments MUST be on disk.

    The previous version only snapshotted at run_experiment, which couldn't
    distinguish "writes happen before run" from "writes happen at any point
    in main()". This catches a regression where attachment writes drift to
    after run_experiment.start()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(
            instruction="# Hello",
            attachments={
                "catalog.md": "# Catalog\n\n- alpha\n- beta\n",
                "lookup.csv": "k,v\n1,a\n",
            },
        )

        files_before_setup: dict[str, str] = {}
        files_at_run_time: dict[str, str] = {}

        def _snapshot_before_setup():
            for name in os.listdir(tmpdir):
                path = os.path.join(tmpdir, name)
                if os.path.isfile(path):
                    with open(path) as fh:
                        files_before_setup[name] = fh.read()
            return config

        async def _capture_workspace_state(*args, **kwargs):
            for name in os.listdir(tmpdir):
                path = os.path.join(tmpdir, name)
                if os.path.isfile(path):
                    with open(path) as fh:
                        files_at_run_time[name] = fh.read()

        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch(
                "pmf_engine.runner.main.RunnerConfig.from_env",
                side_effect=_snapshot_before_setup,
            ):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish"):
                        with patch(
                            "pmf_engine.runner.main.run_experiment",
                            side_effect=_capture_workspace_state,
                        ):
                            await main()

        # BEFORE workspace setup: attachments must NOT be on disk yet.
        assert "catalog.md" not in files_before_setup, (
            f"attachments must not exist before from_env returns; got: "
            f"{list(files_before_setup.keys())!r}"
        )
        assert "lookup.csv" not in files_before_setup
        assert "instruction.md" not in files_before_setup

        # AT run_experiment time: every attachment + instruction.md is on disk.
        assert files_at_run_time.get("catalog.md") == "# Catalog\n\n- alpha\n- beta\n"
        assert files_at_run_time.get("lookup.csv") == "k,v\n1,a\n"
        assert files_at_run_time.get("instruction.md") == "# Hello"


@pytest.mark.asyncio
async def test_main_rejects_reserved_basename_attachment():
    """The publisher and broker already refuse these, but the runner
    double-checks at write time. A broker drift that surfaces a reserved
    basename must crash loudly, not silently clobber instruction.md.

    The runner's main() catches the AttachmentSafetyViolation, reports
    "failed" with reason_code='AttachmentSafetyViolation' so ops can grep
    for the distinct cause, then re-raises — so callers see the original
    exception, not a SystemExit."""
    from pmf_engine.runner.main import AttachmentSafetyViolation

    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(
            instruction="legit instruction",
            attachments={"instruction.md": "MALICIOUS — would clobber instruction"},
        )

        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish") as mock_publish:
                        with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock) as mock_run:
                            with pytest.raises(AttachmentSafetyViolation, match="reserved basename"):
                                await main()

        # The agent must NEVER have been spawned — the bad attachment is a
        # routing-level bug, not something the agent can recover from.
        mock_run.assert_not_called()
        # The legit instruction.md write happened before the attachment
        # write, so the file exists with the *legitimate* content — never
        # the attempted clobber.
        with open(os.path.join(tmpdir, "instruction.md")) as fh:
            content = fh.read()
        assert content == "legit instruction"
        # And the runner must have reported failure with the distinct
        # error type so the run doesn't hang in PENDING and ops can grep
        # CloudWatch for AttachmentSafetyViolation specifically.
        mock_publish.report_status.assert_called()
        failed_call = mock_publish.report_status.call_args
        assert failed_call[0][0] == "failed"
        assert failed_call[1]["reason_code"] == "AttachmentSafetyViolation"


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_name", [
    "../escape.md",        # parent-dir traversal
    "/abs/path.md",        # absolute path
    "nested/file.md",      # nested subdir (not a basename)
])
async def test_main_rejects_unsafe_attachment_basenames(unsafe_name):
    """Path-safety belt-and-suspenders: the runner refuses any attachment
    name that isn't a clean basename, even if the broker somehow forwards
    one. Without this guard, a malformed broker response could write
    outside /workspace/ entirely.

    Pin reason_code='AttachmentSafetyViolation' so the unsafe-basename and
    reserved-basename branches share the same greppable error type but
    distinct error messages."""
    from pmf_engine.runner.main import AttachmentSafetyViolation

    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(
            instruction="legit",
            attachments={unsafe_name: "would-escape-or-nest"},
        )

        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish") as mock_publish:
                        with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock) as mock_run:
                            with pytest.raises(AttachmentSafetyViolation, match="unsafe basename"):
                                await main()

        mock_run.assert_not_called()
        failed_calls = [
            c for c in mock_publish.report_status.call_args_list
            if c[0][0] == "failed"
        ]
        assert failed_calls
        assert failed_calls[-1].kwargs.get("reason_code") == "AttachmentSafetyViolation"


@pytest.mark.asyncio
async def test_main_writes_attachments_with_utf8_encoding(tmp_path):
    """Attachments may contain non-ASCII content (em-dashes, CJK, smart quotes).
    The runner must explicitly write UTF-8 — otherwise on a locale-C container
    the default `open()` encoding is ASCII and non-ASCII bodies crash mid-write,
    leaving a partial file.
    """
    body = "Hello—world. 你好.\n"  # em-dash + "hello" in Chinese
    config = _make_config(
        instruction="# Hello",
        attachments={"unicode.md": body},
    )

    with patch.dict(os.environ, {"WORKSPACE_DIR": str(tmp_path)}):
        with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
            with patch("pmf_engine.runner.main.init_config"):
                with patch("pmf_engine.runner.main.publish"):
                    with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock):
                        await main()

    written_path = tmp_path / "unicode.md"
    on_disk_bytes = written_path.read_bytes()
    # Exact UTF-8 byte sequence — proves no locale fallback happened.
    assert on_disk_bytes == body.encode("utf-8")


@pytest.mark.asyncio
async def test_main_attachment_write_uses_exclusive_create(tmp_path):
    """`open(..., "w")` silently truncates. `open(..., "x")` raises
    FileExistsError on collision — a non-reserved-but-already-present file
    must NOT be clobbered, even though the reserved-name set blocks the
    obvious cases. Defense in depth for any file the runner/harness writes
    that isn't in the reserved set."""
    # Pre-create a file at the target attachment path with old content.
    target = tmp_path / "preexisting.txt"
    target.write_text("ORIGINAL — must not be clobbered")

    config = _make_config(
        instruction="# Hello",
        attachments={"preexisting.txt": "new contents"},
    )

    with patch.dict(os.environ, {"WORKSPACE_DIR": str(tmp_path)}):
        with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
            with patch("pmf_engine.runner.main.init_config"):
                with patch("pmf_engine.runner.main.publish") as mock_publish:
                    with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock) as mock_run:
                        with pytest.raises((FileExistsError, RuntimeError)):
                            await main()

    # File contents must be unchanged — the exclusive-create open raised
    # *before* writing.
    assert target.read_text() == "ORIGINAL — must not be clobbered"
    # Agent must NOT have been spawned.
    mock_run.assert_not_called()
    # Runner must have surfaced a failed callback so the run doesn't hang
    # PENDING in gp-api.
    mock_publish.report_status.assert_called()


@pytest.mark.asyncio
async def test_attachment_safety_violation_log_includes_error_type(tmp_path):
    """When the runner rejects a reserved-name attachment, it must emit a
    structured log with errorType=reserved_basename so ops can grep
    CloudWatch for the specific cause without parsing free-form messages."""
    import logging
    import pmf_engine.runner.main as _main_mod

    config = _make_config(
        instruction="legit",
        attachments={"instruction.md": "would clobber"},
    )

    from pmf_engine.runner.main import AttachmentSafetyViolation

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.ERROR)
    _main_mod.logger.addHandler(handler)
    try:
        with patch.dict(os.environ, {"WORKSPACE_DIR": str(tmp_path)}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish"):
                        with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock):
                            with pytest.raises(AttachmentSafetyViolation):
                                await main()
    finally:
        _main_mod.logger.removeHandler(handler)

    msgs = [r.getMessage() for r in captured]
    joined = "\n".join(msgs)
    assert "errorType=reserved_basename" in joined, (
        f"expected structured log with errorType=reserved_basename; got: {msgs!r}"
    )


@pytest.mark.asyncio
async def test_main_partial_attachment_write_failure_does_not_invoke_run_experiment(tmp_path):
    """If attachment N raises mid-loop, partial writes from N-1 and earlier
    stay on disk (current behavior — workspace state is undefined). The
    contract worth pinning: run_experiment must NOT be invoked and the
    runner must report 'failed' with the safety-violation reason code.
    """
    from pmf_engine.runner.main import AttachmentSafetyViolation

    config = _make_config(
        instruction="instruction",
        attachments={
            "first.md": "first body",
            "instruction.md": "RESERVED — must raise",
            "third.md": "third body",
        },
    )

    with patch.dict(os.environ, {"WORKSPACE_DIR": str(tmp_path)}):
        with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
            with patch("pmf_engine.runner.main.init_config"):
                with patch("pmf_engine.runner.main.publish") as mock_publish:
                    with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock) as mock_run:
                        with pytest.raises(AttachmentSafetyViolation):
                            await main()

    # run_experiment never invoked.
    mock_run.assert_not_called()

    # report_status('failed') called with the new error class.
    failed_calls = [
        c for c in mock_publish.report_status.call_args_list
        if c[0][0] == "failed"
    ]
    assert failed_calls, "expected a failed callback"
    assert any(
        c.kwargs.get("reason_code") == "AttachmentSafetyViolation"
        for c in failed_calls
    ), (
        f"expected reason_code='AttachmentSafetyViolation'; got: "
        f"{[c.kwargs.get('reason_code') for c in failed_calls]!r}"
    )

    # Document current partial-write behavior — first.md on disk, instruction.md
    # is the legit one (written before the attachment loop ran), third.md
    # never reached the disk. If a future "clean up on failure" change lands,
    # this assertion forces a deliberate update.
    assert (tmp_path / "first.md").exists()
    assert (tmp_path / "instruction.md").read_text() == "instruction"
    assert not (tmp_path / "third.md").exists()


@pytest.mark.asyncio
async def test_main_works_with_runner_config_default_attachments(tmp_path):
    """Integration pin for D13: when RunnerConfig is constructed without
    attachments=... (the default factory produces an empty dict), main.py's
    attachment-write loop must iterate cleanly — no AttributeError on None,
    no extra files created, run_experiment still spawned. This catches
    regressions where the default factory is removed without auditing the
    callsite."""
    # Construct WITHOUT attachments kwarg — exercises default_factory.
    config = RunnerConfig(
        experiment_id="hello_world",
        run_id="run-default-attachments",
        organization_slug="org-x",
        instruction="# Hi",
        broker_url="https://broker.test",
        broker_token="tok",
    )
    # Sanity: the default factory produced an empty dict, not None.
    assert config.attachments == {}

    with patch.dict(os.environ, {"WORKSPACE_DIR": str(tmp_path)}):
        with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
            with patch("pmf_engine.runner.main.init_config"):
                with patch("pmf_engine.runner.main.publish"):
                    with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock) as mock_run:
                        await main()

    mock_run.assert_called_once()
    # Only instruction.md present — no spurious attachment files.
    assert (tmp_path / "instruction.md").exists()
    extra_files = [
        p.name for p in tmp_path.iterdir()
        if p.is_file() and p.name != "instruction.md"
    ]
    assert extra_files == [], (
        f"default-attachments path must not create extra files; got: {extra_files!r}"
    )


@pytest.mark.asyncio
async def test_main_handles_empty_attachments_dict():
    """The common case (experiment publishes no sidecars) must not regress.
    Empty attachments dict → instruction.md written, run_experiment called,
    no errors."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(instruction="just instruction", attachments={})

        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish"):
                        with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock) as mock_run:
                            await main()

        mock_run.assert_called_once()
        assert os.path.exists(os.path.join(tmpdir, "instruction.md"))


@pytest.mark.asyncio
async def test_main_sends_failed_status_on_timeout():
    config = _make_config(instruction="Do stuff", timeout_seconds=1)

    async def slow_run(*args, **kwargs):
        await asyncio.sleep(10)

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish") as mock_publish:
                        with patch("pmf_engine.runner.main.run_experiment", side_effect=slow_run):
                            with pytest.raises(SystemExit):
                                await main()

    status_calls = [c for c in mock_publish.report_status.call_args_list]
    statuses = [c[0][0] for c in status_calls]
    assert "failed" in statuses
    failed_call = next(c for c in status_calls if c[0][0] == "failed")
    # Primary contract: reason_code is the stable greppable field operators
    # alert on. The free-form detail string is a secondary, human-readable
    # signal.
    assert failed_call[1]["reason_code"] == "Timeout"
    assert "timed out" in failed_call[1]["detail"]


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_contract_violation_reports_status(mock_publish, _mock_logs):
    config = _make_config(
        contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}},
    )
    fake_result = HarnessResult(
        artifact_bytes=b'{"greeting": 42}',
        content_type="application/json",
        cost_usd=0.02,
        num_turns=2,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result

    await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_not_called()
    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "contract_violation"
    assert "greeting" in call_args[1]["detail"]
    assert call_args[1]["rejected_artifact"] == {"greeting": 42}


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_contract_violation_invalid_json_still_reports(mock_publish, _mock_logs):
    """When the agent writes malformed JSON, contract validation raises
    ContractViolation with 'Invalid JSON' in its message. The handler then
    tries to re-parse artifact_bytes to include as rejected_artifact — which
    will JSONDecodeError if we don't guard it. That secondary crash would
    skip report_status and mark_callback_sent, leaving the run PENDING
    forever in gp-api with no way to know why.
    """
    config = _make_config(contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}})
    fake_result = HarnessResult(
        artifact_bytes=b'this is not valid json {{{',
        content_type="application/json",
        cost_usd=0.02,
        num_turns=2,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result

    await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_not_called()
    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "contract_violation"
    # Rejected artifact shape: a dict with the raw bytes preserved so humans
    # can still see what the agent wrote.
    rejected = call_args[1]["rejected_artifact"]
    assert isinstance(rejected, dict)
    assert "this is not valid json" in rejected.get("_raw_bytes", "")


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_contract_violation_none_artifact_bytes(mock_publish, _mock_logs):
    """Edge case: an empty harness result that somehow surfaces with
    artifact_bytes=None. The fallback dict-builder must not crash on
    `None[:4096]` — which would skip report_status entirely and leave the run
    PENDING forever (the same failure mode Fix #6 was supposed to close).
    """
    config = _make_config(contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}})
    fake_result = HarnessResult(
        artifact_bytes=None,  # type: ignore[arg-type]
        content_type="application/json",
        cost_usd=0.02,
        num_turns=2,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result

    await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_not_called()
    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "contract_violation"
    rejected = call_args[1]["rejected_artifact"]
    assert isinstance(rejected, dict)
    assert rejected.get("_raw_bytes") == ""
    assert rejected.get("_truncated") is False


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_contract_violation_str_artifact_bytes(mock_publish, _mock_logs):
    """If a fake harness or buggy plugin hands back artifact_bytes as str
    instead of bytes, .decode() on str raises AttributeError — same lost
    callback. Coerce defensively.
    """
    config = _make_config(contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}})
    fake_result = HarnessResult(
        artifact_bytes="not bytes, but str",  # type: ignore[arg-type]
        content_type="application/json",
        cost_usd=0.02,
        num_turns=2,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result

    await run_experiment(config, harness=mock_harness)

    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "contract_violation"
    rejected = call_args[1]["rejected_artifact"]
    assert "not bytes, but str" in rejected.get("_raw_bytes", "")


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_no_output_file_reports_failed(mock_publish, _mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = FileNotFoundError("No output files found in /workspace/output")

    with pytest.raises(FileNotFoundError, match="No output files"):
        await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_not_called()
    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "failed"
    assert "No output files" in call_args[1]["detail"]


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_multiple_output_files_reports_failed(mock_publish, _mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = RuntimeError("Expected 1 output file, found 3")

    with pytest.raises(RuntimeError, match="Expected 1 output file"):
        await run_experiment(config, harness=mock_harness)

    mock_publish.publish.assert_not_called()
    mock_publish.report_status.assert_called_once()
    call_args = mock_publish.report_status.call_args
    assert call_args[0][0] == "failed"
    assert "Expected 1 output file" in call_args[1]["detail"]


@pytest.mark.asyncio
async def test_main_exits_on_missing_experiment_id():
    empty_config = _make_config(experiment_id="", instruction="something")

    with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=empty_config):
        with patch("pmf_engine.runner.main.init_config"):
            with patch("pmf_engine.runner.main.publish") as mock_publish:
                with pytest.raises(SystemExit) as exc_info:
                    await main()
    assert exc_info.value.code == 1

    status_calls = [c[0][0] for c in mock_publish.report_status.call_args_list]
    assert "failed" in status_calls
    failed_call = next(c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed")
    assert "EXPERIMENT_ID" in failed_call[1]["detail"]


@pytest.mark.asyncio
async def test_main_exits_on_missing_instruction():
    no_instruction_config = _make_config(instruction="")

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=no_instruction_config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish") as mock_publish:
                        with pytest.raises(SystemExit) as exc_info:
                            await main()
    assert exc_info.value.code == 1

    status_calls = [c[0][0] for c in mock_publish.report_status.call_args_list]
    assert "failed" in status_calls
    failed_call = next(c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed")
    assert "instruction" in failed_call[1]["detail"].lower()


@pytest.mark.asyncio
async def test_main_sends_failed_status_on_signal():
    import pmf_engine.runner.main as main_module

    config = _make_config(instruction="Do stuff", timeout_seconds=30)

    async def interrupted_run(*args, **kwargs):
        main_module._shutdown_requested = True
        raise Exception("interrupted")

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.init_config"):
                    with patch("pmf_engine.runner.main.publish") as mock_publish:
                        with patch("pmf_engine.runner.main.run_experiment", side_effect=interrupted_run):
                            with pytest.raises(SystemExit):
                                await main()

    main_module._shutdown_requested = False

    status_calls = [c[0][0] for c in mock_publish.report_status.call_args_list]
    assert "failed" in status_calls
    failed_call = next(c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed")
    assert "signal" in failed_call[1]["detail"].lower()


def _make_mock_bt():
    mock_span = MagicMock()
    mock_bt = MagicMock()

    @contextmanager
    def fake_traced_span(**kwargs):
        yield mock_span

    mock_bt.traced_span.side_effect = fake_traced_span
    return mock_bt, mock_span


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_traces_success_to_braintrust(mock_publish, _mock_logs):
    config = _make_config()
    fake_result = HarnessResult(
        artifact_bytes=b'{"greeting": "hello"}',
        content_type="application/json",
        cost_usd=0.05,
        num_turns=3,
        session_id="sess-abc",
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result
    mock_bt, mock_span = _make_mock_bt()

    with patch("pmf_engine.runner.main.BraintrustClient.get_instance", return_value=mock_bt):
        await run_experiment(config, harness=mock_harness)

    mock_bt.init.assert_called_once_with("pmf-engine")

    call_kwargs = mock_bt.traced_span.call_args[1]
    assert call_kwargs["name"] == "experiment:hello_world"
    assert call_kwargs["input_data"]["experiment_id"] == "hello_world"
    assert call_kwargs["input_data"]["run_id"] == "run-001"
    assert call_kwargs["input_data"]["organization_slug"] == "org-123"
    assert call_kwargs["input_data"]["model"] == "sonnet"
    assert "pmf" in call_kwargs["tags"]
    assert "hello_world" in call_kwargs["tags"]

    mock_span.log.assert_called_once()
    log_kwargs = mock_span.log.call_args[1]
    assert log_kwargs["output"]["status"] == "success"
    assert log_kwargs["output"]["cost_usd"] == 0.05
    assert log_kwargs["output"]["num_turns"] == 3
    assert log_kwargs["output"]["duration_seconds"] > 0

    mock_bt.flush.assert_called_once()


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_traces_failure_to_braintrust(mock_publish, _mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = RuntimeError("Agent crashed")
    mock_bt, mock_span = _make_mock_bt()

    with patch("pmf_engine.runner.main.BraintrustClient.get_instance", return_value=mock_bt):
        with pytest.raises(RuntimeError, match="Agent crashed"):
            await run_experiment(config, harness=mock_harness)

    mock_span.log.assert_called_once()
    log_kwargs = mock_span.log.call_args[1]
    assert log_kwargs["output"]["status"] == "failed"
    assert "Agent crashed" in log_kwargs["output"]["error"]

    mock_bt.flush.assert_called_once()


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_traces_contract_violation_to_braintrust(mock_publish, _mock_logs):
    config = _make_config(contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}})
    fake_result = HarnessResult(
        artifact_bytes=b'{"greeting": 42}',
        content_type="application/json",
        cost_usd=0.02,
        num_turns=2,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result
    mock_bt, mock_span = _make_mock_bt()

    with patch("pmf_engine.runner.main.BraintrustClient.get_instance", return_value=mock_bt):
        await run_experiment(config, harness=mock_harness)

    mock_span.log.assert_called_once()
    log_kwargs = mock_span.log.call_args[1]
    assert log_kwargs["output"]["status"] == "contract_violation"
    assert "greeting" in log_kwargs["output"]["error"]

    mock_bt.flush.assert_called_once()


class TestMainErrorPaths:
    @pytest.mark.asyncio
    async def test_main_does_not_double_send_status_on_harness_failure(self):
        config = _make_config(instruction="Do stuff", timeout_seconds=30)

        fake_harness = AsyncMock()
        fake_harness.run.side_effect = RuntimeError("Agent crashed")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.get_harness", return_value=fake_harness):
                                with patch("pmf_engine.runner.main._upload_logs"):
                                    with pytest.raises((Exception, SystemExit)):
                                        await main()

        failed_calls = [c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed"]
        assert len(failed_calls) == 1, (
            f"Expected exactly 1 failed status after harness failure, "
            f"got {len(failed_calls)}"
        )

    @pytest.mark.asyncio
    async def test_main_sends_failed_status_even_when_run_id_is_empty(self):
        config = _make_config(run_id="", instruction="Do stuff", timeout_seconds=30)

        async def crash(*args, **kwargs):
            raise RuntimeError("Unexpected runner crash")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.run_experiment", side_effect=crash):
                                with pytest.raises(RuntimeError):
                                    await main()

        failed_calls = [c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed"]
        assert len(failed_calls) >= 1
        assert "Unexpected runner crash" in failed_calls[-1][1]["detail"]

    @pytest.mark.asyncio
    async def test_main_exits_on_broker_init_failure(self):
        config = _make_config(instruction="Do stuff")

        with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
            with patch("pmf_engine.runner.main.init_config", side_effect=ValueError("BROKER_URL is required")):
                with pytest.raises(SystemExit) as exc_info:
                    await main()
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_main_uploads_logs_on_timeout(self):
        config = _make_config(instruction="Do stuff", timeout_seconds=1)

        async def slow_run(*args, **kwargs):
            await asyncio.sleep(10)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.run_experiment", side_effect=slow_run):
                                with patch("pmf_engine.runner.main._upload_logs") as mock_upload:
                                    with pytest.raises(SystemExit):
                                        await main()

        mock_upload.assert_called_once()
        failed_calls = [c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed"]
        assert len(failed_calls) >= 1
        assert "timed out" in failed_calls[0][1]["detail"]


class TestValidatorScriptShim:
    def _obj(self, required, **props):
        return {"type": "object", "required": required, "properties": props}

    def _write_workspace(self, tmpdir: Path, schema: dict, artifacts: dict):
        (tmpdir / "output").mkdir(exist_ok=True)
        (tmpdir / "contract_schema.json").write_text(json.dumps(schema))
        for name, body in artifacts.items():
            (tmpdir / "output" / name).write_text(json.dumps(body))
        from pmf_engine.runner.main import _VALIDATOR_SCRIPT
        (tmpdir / "validate_output.py").write_text(_VALIDATOR_SCRIPT)

    def _run_validator(self, tmpdir: Path) -> subprocess.CompletedProcess:
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing}" if existing else str(repo_root)
        return subprocess.run(
            [sys.executable, str(tmpdir / "validate_output.py")],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_passes_on_valid_artifact(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema=self._obj(["greeting", "count"], greeting={"type": "string"}, count={"type": "number"}),
            artifacts={"result.json": {"greeting": "hello", "count": 5}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS" in result.stdout

    def test_fails_on_missing_field_and_lists_it(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema=self._obj(["greeting", "count"], greeting={"type": "string"}, count={"type": "number"}),
            artifacts={"result.json": {"greeting": "hello"}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "FAIL" in result.stdout
        assert "count" in result.stdout

    def test_fails_on_multiple_errors_and_lists_all(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema=self._obj(
                ["a", "b", "c"],
                a={"type": "string"},
                b={"type": "number"},
                c={"type": "boolean"},
            ),
            artifacts={"result.json": {}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "a" in result.stdout
        assert "b" in result.stdout
        assert "c" in result.stdout

    def test_fails_on_enum_violation(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema=self._obj(
                ["tier"],
                tier={"type": "string", "enum": ["bronze", "silver", "gold"]},
            ),
            artifacts={"result.json": {"tier": "platinum"}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "platinum" in result.stdout

    def test_fails_when_output_dir_empty(self, tmp_path):
        (tmp_path / "output").mkdir()
        (tmp_path / "contract_schema.json").write_text(json.dumps(self._obj(["x"], x={"type": "string"})))
        from pmf_engine.runner.main import _VALIDATOR_SCRIPT
        (tmp_path / "validate_output.py").write_text(_VALIDATOR_SCRIPT)
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "No JSON" in result.stdout or "No output" in result.stdout


class TestCallbackLifecycleFix:
    @pytest.mark.asyncio
    async def test_exception_chaining_does_not_duplicate_status(self):
        config = _make_config(instruction="Do stuff", timeout_seconds=30)

        async def inner_send_and_chain(*args, **kwargs):
            from pmf_engine.runner.main import _mark_callback_sent
            _mark_callback_sent()
            try:
                raise RuntimeError("Agent crashed")
            except RuntimeError as e:
                raise ValueError("wrapped") from e

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.run_experiment", side_effect=inner_send_and_chain):
                                with pytest.raises((ValueError, Exception)):
                                    await main()

        failed_calls = [c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed"]
        assert len(failed_calls) == 0, (
            f"Expected no failed status when callback already sent, "
            f"got {len(failed_calls)}"
        )

    @pytest.mark.asyncio
    async def test_publish_failure_uploads_logs_before_failed_status(self):
        config = _make_config()
        fake_result = HarnessResult(
            artifact_bytes=b'{"data": "ok"}',
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        call_order = []

        def track_upload_logs(*args, **kwargs):
            call_order.append("upload_logs")

        real_report_status_called = False

        original_publish = MagicMock()
        original_publish.publish.side_effect = Exception("Broker down")

        def track_report_status(status, **kwargs):
            call_order.append(f"report_status:{status}")

        original_publish.report_status.side_effect = track_report_status

        with patch("pmf_engine.runner.main._upload_logs", side_effect=track_upload_logs):
            with patch("pmf_engine.runner.main.publish", original_publish):
                with pytest.raises(Exception, match="Broker down"):
                    await run_experiment(config, harness=mock_harness)

        assert "upload_logs" in call_order, f"Expected _upload_logs to be called, got: {call_order}"
        assert "report_status:failed" in call_order
        assert call_order.index("upload_logs") < call_order.index("report_status:failed"), (
            f"Expected log upload BEFORE failed status, got: {call_order}"
        )

    def test_corrupt_params_json_raises(self):
        with patch.dict(os.environ, {
            "PARAMS_JSON": "not-json",
            "EXPERIMENT_ID": "hello_world",
            "RUN_ID": "run-001",
            "ORGANIZATION_SLUG": "org-123",
            "INSTRUCTION": "x",
            "AGENT_MODEL": "sonnet",
        }):
            with pytest.raises(ValueError, match="PARAMS_JSON"):
                RunnerConfig.from_env()

    @pytest.mark.asyncio
    async def test_bootstrap_config_failure_exits(self):
        with patch.dict(os.environ, {
            "RUN_ID": "run-boot",
            "ORGANIZATION_SLUG": "org-boot",
            "EXPERIMENT_ID": "smoke_test",
            "BROKER_URL": "https://broker.test",
            "BROKER_TOKEN": "tok-test",
            "ENVIRONMENT": "dev",
        }):
            with patch(
                "pmf_engine.runner.main.RunnerConfig.from_env",
                side_effect=ValueError("Invalid TIMEOUT_SECONDS"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    await main()

        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_validator_non_contract_violation_uploads_logs_and_reports_failed(self):
        config = _make_config(contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": "hello"}',
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        with patch(
            "pmf_engine.runner.main.validate_artifact_contract",
            side_effect=TypeError("schema author bug"),
        ):
            with patch("pmf_engine.runner.main._upload_logs") as mock_upload:
                with patch("pmf_engine.runner.main.publish") as mock_publish:
                    with pytest.raises(TypeError, match="schema author bug"):
                        await run_experiment(config, harness=mock_harness)

        mock_upload.assert_called()
        mock_publish.report_status.assert_called_once()
        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "failed"
        assert "schema author bug" in call_args[1]["detail"]
        mock_publish.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_sigterm_handler_cancels_current_task(self):
        import pmf_engine.runner.main as main_module
        import signal as signal_module

        main_module._shutdown_requested = False

        cancelled = False

        async def long_running():
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

        task = asyncio.ensure_future(long_running())
        main_module._current_task = task
        try:
            await asyncio.sleep(0)

            main_module._handle_signal(signal_module.SIGTERM)

            with pytest.raises(asyncio.CancelledError):
                await task

            assert cancelled is True
            assert main_module._shutdown_requested is True
        finally:
            main_module._current_task = None
            main_module._shutdown_requested = False

    @pytest.mark.asyncio
    async def test_main_cancels_task_and_sends_failed_status_on_external_cancel(self):
        config = _make_config(instruction="Do stuff", timeout_seconds=30)

        cancelled_from_inside = False

        async def long_running(*args, **kwargs):
            nonlocal cancelled_from_inside
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled_from_inside = True
                raise

        async def cancel_soon():
            await asyncio.sleep(0.05)
            import pmf_engine.runner.main as main_module
            import signal as signal_module
            main_module._handle_signal(signal_module.SIGTERM)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.run_experiment", side_effect=long_running):
                                with patch("pmf_engine.runner.main._upload_logs"):
                                    canceller = asyncio.ensure_future(cancel_soon())
                                    with pytest.raises(SystemExit):
                                        await main()
                                    try:
                                        await canceller
                                    except Exception:
                                        pass

        assert cancelled_from_inside, "run_experiment task was not actually cancelled"
        failed_calls = [c for c in mock_publish.report_status.call_args_list if c[0][0] == "failed"]
        assert len(failed_calls) >= 1
        assert "signal" in failed_calls[0][1]["detail"].lower()

    @pytest.mark.asyncio
    async def test_success_publish_failure_does_not_trigger_failed_after_success(self):
        config = _make_config()
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": "hello"}',
            content_type="application/json",
            cost_usd=0.05,
            num_turns=3,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        mock_pub = MagicMock()
        mock_pub.publish.side_effect = Exception("Broker down")
        statuses_reported = []

        def track_status(status, **kwargs):
            statuses_reported.append(status)

        mock_pub.report_status.side_effect = track_status

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish", mock_pub):
                            with patch("pmf_engine.runner.main.get_harness", return_value=mock_harness):
                                with patch("pmf_engine.runner.main._upload_logs"):
                                    with pytest.raises(Exception, match="Broker down"):
                                        await main()

        assert statuses_reported.count("failed") == 1, (
            f"Expected exactly 1 failed status, got: {statuses_reported}"
        )


class TestFailureCallbacksCarryDurationAndCost:
    """gp-api's ExperimentRun.durationSeconds / .costUsd land as 0 for every
    failed run because the runner never passes time.monotonic() -
    start_time (or HarnessResult.cost_usd when available) into
    publish.report_status on the failure paths. Lock in the contract at every
    failure callsite so the billing/analytics columns carry real values.
    """

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_logs")
    @patch("pmf_engine.runner.main.publish")
    async def test_harness_failure_reports_duration_seconds(self, mock_publish, _mock_logs):
        config = _make_config()
        clock = Clock(start=100.0)

        async def harness_run_advances_clock(*args, **kwargs):
            # The agent ran for 30s before crashing.
            clock.advance(30.0)
            raise RuntimeError("Agent crashed")

        mock_harness = AsyncMock()
        mock_harness.run.side_effect = harness_run_advances_clock

        with patch("pmf_engine.runner.main.time.monotonic", clock.now):
            with pytest.raises(RuntimeError, match="Agent crashed"):
                await run_experiment(config, harness=mock_harness)

        mock_publish.report_status.assert_called_once()
        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "failed"
        assert call_args[1].get("duration_seconds") == pytest.approx(30.0, abs=0.01)

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_logs")
    @patch("pmf_engine.runner.main.publish")
    async def test_contract_violation_reports_duration_and_cost(self, mock_publish, _mock_logs):
        config = _make_config(contract_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": 42}',
            content_type="application/json",
            cost_usd=0.13,
            num_turns=2,
        )
        clock = Clock(start=100.0)

        async def harness_run_advances_clock(*args, **kwargs):
            clock.advance(42.5)
            return fake_result

        mock_harness = AsyncMock()
        mock_harness.run.side_effect = harness_run_advances_clock

        with patch("pmf_engine.runner.main.time.monotonic", clock.now):
            await run_experiment(config, harness=mock_harness)

        mock_publish.report_status.assert_called_once()
        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "contract_violation"
        assert call_args[1].get("duration_seconds") == pytest.approx(42.5, abs=0.01)
        assert call_args[1].get("cost_usd") == pytest.approx(0.13, abs=1e-6)

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_logs")
    @patch("pmf_engine.runner.main.publish")
    async def test_publish_failure_reports_duration_and_cost(self, mock_publish, _mock_logs):
        config = _make_config()
        fake_result = HarnessResult(
            artifact_bytes=b'{"data": "ok"}',
            content_type="application/json",
            cost_usd=0.21,
            num_turns=1,
        )
        clock = Clock(start=100.0)

        async def harness_run_advances_clock(*args, **kwargs):
            clock.advance(15.0)
            return fake_result

        mock_harness = AsyncMock()
        mock_harness.run.side_effect = harness_run_advances_clock
        mock_publish.publish.side_effect = Exception("Broker down")

        with patch("pmf_engine.runner.main.time.monotonic", clock.now):
            with pytest.raises(Exception, match="Broker down"):
                await run_experiment(config, harness=mock_harness)

        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "failed"
        assert call_args[1].get("duration_seconds") == pytest.approx(15.0, abs=0.01)
        assert call_args[1].get("cost_usd") == pytest.approx(0.21, abs=1e-6)


class TestObservabilityHardening:
    @pytest.mark.asyncio
    async def test_main_surfaces_broker_fetch_error_in_status(self):
        """A broker fetch failure (transport error, malformed envelope, etc.)
        must surface as SystemExit(1) so ECS marks the task FAILED instead of
        silently passing the runner empty state."""
        from pmf_engine.runner.manifest_loader import ManifestLoadError
        with patch.dict(os.environ, {
            "EXPERIMENT_ID": "smoke_test",
            "RUN_ID": "run-broker-err",
            "ORGANIZATION_SLUG": "org-x",
            "BROKER_URL": "https://broker.test",
            "BROKER_TOKEN": "tok-test",
            "PARAMS_JSON": "{}",
        }, clear=False):
            os.environ.pop("INSTRUCTION", None)
            with patch(
                "pmf_engine.runner.manifest_loader.load_from_broker",
                side_effect=ManifestLoadError("broker 503"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    await main()

        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_main_reports_failed_callback_when_broker_fetch_fails(self):
        """When the broker is reachable enough to mint a token but the
        manifest fetch fails (e.g., 503, malformed envelope), the runner MUST
        send a `report_status('failed', ...)` callback so gp-api flips the
        ExperimentRun row from PENDING → FAILED. Without this, runs hang
        PENDING forever and operators have to grep CloudWatch to find them.
        """
        from pmf_engine.runner.manifest_loader import ManifestLoadError
        with patch.dict(os.environ, {
            "EXPERIMENT_ID": "smoke_test",
            "RUN_ID": "run-callback-on-fail",
            "ORGANIZATION_SLUG": "org-x",
            "BROKER_URL": "https://broker.test",
            "BROKER_TOKEN": "tok-test",
            "PARAMS_JSON": "{}",
        }, clear=False):
            os.environ.pop("INSTRUCTION", None)
            with patch("pmf_engine.runner.main.publish") as mock_publish, \
                 patch("pmf_engine.runner.main.init_config"), \
                 patch(
                     "pmf_engine.runner.manifest_loader.load_from_broker",
                     side_effect=ManifestLoadError("broker 503"),
                 ):
                with pytest.raises(SystemExit):
                    await main()

        mock_publish.report_status.assert_called_once()
        call = mock_publish.report_status.call_args
        assert call.args[0] == "failed", (
            f"first arg must be 'failed' status, got {call.args[0]!r}"
        )
        assert call.kwargs.get("reason_code") == "ManifestLoadError", (
            f"reason_code must surface the exception type for ops triage, "
            f"got {call.kwargs.get('reason_code')!r}"
        )
        detail = call.kwargs.get("detail", "")
        assert "broker 503" in detail, (
            f"detail must include the underlying error message, got {detail!r}"
        )


class TestRunExperimentNoS3OrSQS:
    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_logs")
    @patch("pmf_engine.runner.main.publish")
    async def test_run_experiment_signature_has_no_s3_or_sqs_params(self, mock_publish, _mock_logs):
        import inspect
        sig = inspect.signature(run_experiment)
        param_names = set(sig.parameters.keys())
        assert "s3_client" not in param_names
        assert "sqs_client" not in param_names


class TestCollectWorkspaceFilesSensitiveWithAllowedExtensions:
    """Sensitive filename patterns (credentials, .env, secret, key, pem, crt)
    MUST be excluded even when allowed_extensions is set. The /tmp scan in
    _collect_log_files uses allowed_extensions=_SAFE_TMP_EXTENSIONS, and
    without an unconditional sensitivity check, a file like
    /tmp/credentials.json would be uploaded to S3 because it matches the
    .json allowlist.
    """

    def test_credentials_json_excluded_despite_json_allowlist(self, tmp_path):
        from pmf_engine.runner.main import (
            _collect_workspace_files,
            _SAFE_TMP_EXTENSIONS,
        )

        (tmp_path / "credentials.json").write_text('{"api_key": "secret"}')
        (tmp_path / "normal.json").write_text('{"ok": true}')

        collected = _collect_workspace_files(
            str(tmp_path), allowed_extensions=_SAFE_TMP_EXTENSIONS,
        )

        keys = set(collected.keys())
        assert "workspace/normal.json" in keys, (
            f"normal.json should be collected, got keys: {keys}"
        )
        assert "workspace/credentials.json" not in keys, (
            f"credentials.json must NOT be collected, got keys: {keys}"
        )

    def test_env_pattern_excluded_despite_yaml_allowlist(self, tmp_path):
        from pmf_engine.runner.main import (
            _collect_workspace_files,
            _SAFE_TMP_EXTENSIONS,
        )

        (tmp_path / "config.env.yaml").write_text("key: value\n")
        (tmp_path / "settings.yaml").write_text("ok: true\n")

        collected = _collect_workspace_files(
            str(tmp_path), allowed_extensions=_SAFE_TMP_EXTENSIONS,
        )

        keys = set(collected.keys())
        assert "workspace/settings.yaml" in keys, (
            f"settings.yaml should be collected, got keys: {keys}"
        )
        assert "workspace/config.env.yaml" not in keys, (
            f"config.env.yaml must NOT be collected (contains .env), got keys: {keys}"
        )


class TestBrokerInitLastResortSqsFallback:
    """C2: When BOTH init_config AND the subsequent RunnerConfig.from_env
    broker fetch fail, the runner has no broker channel to send a failed
    callback through. Without a fallback, the run hangs PENDING forever in
    gp-api — exactly the hang this PR was supposed to fix.

    Fix: post the failed-status envelope DIRECTLY to RESULTS_QUEUE_URL via
    SQS as a last resort. Even if the runner doesn't currently have IAM/env
    wired, the helper must exist, be exercised, and gracefully no-op when
    RESULTS_QUEUE_URL is unset.
    """

    @pytest.mark.asyncio
    async def test_double_failure_attempts_direct_sqs_send_then_exits(
        self, monkeypatch
    ):
        """init_config raises, then from_env's broker fetch raises. The
        last-resort SQS sender MUST be invoked, then sys.exit(1).
        """
        monkeypatch.setenv("BROKER_URL", "https://broker.test")
        monkeypatch.setenv("BROKER_TOKEN", "tok-test")
        monkeypatch.setenv("RUN_ID", "run-double-fail")
        monkeypatch.setenv("EXPERIMENT_ID", "smoke_test")
        monkeypatch.setenv("RESULTS_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/results.fifo")

        with patch(
            "pmf_engine.runner.main.init_config",
            side_effect=RuntimeError("broker unreachable"),
        ):
            with patch(
                "pmf_engine.runner.main.RunnerConfig.from_env",
                side_effect=RuntimeError("broker manifest fetch failed"),
            ):
                with patch(
                    "pmf_engine.runner.main._send_failed_to_sqs_directly",
                    return_value=True,
                ) as mock_sqs_send:
                    with pytest.raises(SystemExit) as exc_info:
                        await main()

        assert exc_info.value.code == 1
        mock_sqs_send.assert_called_once()
        call_kwargs = mock_sqs_send.call_args.kwargs or {}
        call_args = mock_sqs_send.call_args.args
        # Helper signature: (run_id, experiment_id, reason_code, detail)
        all_args = list(call_args) + list(call_kwargs.values())
        joined = " ".join(str(a) for a in all_args)
        assert "run-double-fail" in joined, (
            f"run_id must be in last-resort SQS send args, got: {all_args!r}"
        )
        assert "smoke_test" in joined, (
            f"experiment_id must be in last-resort SQS send args, got: {all_args!r}"
        )

    @pytest.mark.asyncio
    async def test_double_failure_still_exits_when_direct_sqs_send_fails(
        self, monkeypatch
    ):
        """If even the SQS direct send fails (e.g., no IAM, no RESULTS_QUEUE_URL,
        SQS down) we MUST still sys.exit(1). The hang-prevention is best-effort —
        a failed sentinel is still better than a CrashLoop on the SQS retry.
        """
        monkeypatch.setenv("BROKER_URL", "https://broker.test")
        monkeypatch.setenv("BROKER_TOKEN", "tok-test")
        monkeypatch.setenv("RUN_ID", "run-no-sqs")
        monkeypatch.setenv("EXPERIMENT_ID", "smoke_test")

        with patch(
            "pmf_engine.runner.main.init_config",
            side_effect=RuntimeError("broker unreachable"),
        ):
            with patch(
                "pmf_engine.runner.main.RunnerConfig.from_env",
                side_effect=RuntimeError("broker manifest fetch failed"),
            ):
                with patch(
                    "pmf_engine.runner.main._send_failed_to_sqs_directly",
                    return_value=False,
                ) as mock_sqs_send:
                    with pytest.raises(SystemExit) as exc_info:
                        await main()

        assert exc_info.value.code == 1
        mock_sqs_send.assert_called_once()

    def test_helper_returns_false_when_results_queue_url_unset(self, monkeypatch):
        """Without RESULTS_QUEUE_URL set, the helper must no-op (return False)
        rather than raising — the entrypoint must still reach sys.exit(1).
        """
        from pmf_engine.runner.main import _send_failed_to_sqs_directly
        monkeypatch.delenv("RESULTS_QUEUE_URL", raising=False)
        ok = _send_failed_to_sqs_directly(
            run_id="run-001",
            experiment_id="smoke_test",
            reason_code="BrokerInitError",
            detail="broker 503",
        )
        assert ok is False

    def test_helper_posts_envelope_to_sqs_when_url_set(self, monkeypatch):
        """When RESULTS_QUEUE_URL is set and SQS accepts the message, the
        helper returns True and the envelope contains the canonical fields
        gp-api's results consumer expects.
        """
        from pmf_engine.runner.main import _send_failed_to_sqs_directly

        monkeypatch.setenv(
            "RESULTS_QUEUE_URL",
            "https://sqs.us-west-2.amazonaws.com/123/results.fifo",
        )
        sent = {}

        class _FakeSqs:
            def send_message(self, **kwargs):
                sent.update(kwargs)
                return {"MessageId": "fake-msg-id"}

        with patch(
            "boto3.client",
            return_value=_FakeSqs(),
        ):
            ok = _send_failed_to_sqs_directly(
                run_id="run-001",
                experiment_id="smoke_test",
                reason_code="BrokerInitError",
                detail="broker 503",
            )

        assert ok is True
        assert sent["QueueUrl"] == "https://sqs.us-west-2.amazonaws.com/123/results.fifo"
        body = json.loads(sent["MessageBody"])
        assert body["run_id"] == "run-001"
        assert body["experiment_id"] == "smoke_test"
        assert body["status"] == "failed"
        assert body["reason_code"] == "BrokerInitError"
        assert body["detail"] == "broker 503"
        assert sent["MessageGroupId"] == "run-001"
        assert "run-001" in sent["MessageDeduplicationId"]
        assert "BrokerInitError" in sent["MessageDeduplicationId"]

    def test_helper_returns_false_on_sqs_exception(self, monkeypatch):
        from pmf_engine.runner.main import _send_failed_to_sqs_directly

        monkeypatch.setenv(
            "RESULTS_QUEUE_URL",
            "https://sqs.us-west-2.amazonaws.com/123/results.fifo",
        )

        class _BrokenSqs:
            def send_message(self, **kwargs):
                raise RuntimeError("sqs down")

        with patch("boto3.client", return_value=_BrokenSqs()):
            ok = _send_failed_to_sqs_directly(
                run_id="run-001",
                experiment_id="smoke_test",
                reason_code="BrokerInitError",
                detail="x",
            )
        assert ok is False


class TestBrokerUrlSchemeGuardBeforeInit:
    """H4: validate_broker_url_scheme MUST run before init_config so a
    plaintext BROKER_URL in prod never gets wired into the broker client.
    The previous code passed http://... straight to init_config, then only
    raised in from_env() AFTER the failed-callback path was already pointed
    at an unencrypted channel.
    """

    @pytest.mark.asyncio
    async def test_plaintext_broker_url_in_prod_does_not_call_init_config(
        self, monkeypatch
    ):
        monkeypatch.setenv("BROKER_URL", "http://broker.example.test:8080")
        monkeypatch.setenv("BROKER_TOKEN", "tok-test")
        monkeypatch.setenv("RUN_ID", "run-001")
        monkeypatch.setenv("EXPERIMENT_ID", "smoke_test")
        monkeypatch.setenv("ENVIRONMENT", "prod")

        with patch("pmf_engine.runner.main.init_config") as mock_init:
            with patch(
                "pmf_engine.runner.main._send_failed_to_sqs_directly",
                return_value=True,
            ):
                with pytest.raises(SystemExit) as exc_info:
                    await main()

        assert exc_info.value.code == 1
        mock_init.assert_not_called()


class TestSigtermDuringInitExitsCleanly:
    """L3: SIGTERM/SIGINT during pre-task init currently sets
    `_shutdown_requested = True` but no code checks it before
    `asyncio.wait_for(...)`. The task then runs to completion or timeout
    despite the signal. This wastes ECS task time and confuses operators
    grepping for SIGTERM-triggered exits.

    Fix: check _shutdown_requested after init_config and after from_env.
    On True, send a Signal failed-callback and sys.exit(1).
    """

    @pytest.mark.asyncio
    async def test_shutdown_requested_after_init_exits_cleanly(self, monkeypatch, tmp_path):
        config = _make_config(instruction="Do stuff")
        monkeypatch.setenv("BROKER_URL", "https://broker.test")
        monkeypatch.setenv("BROKER_TOKEN", "tok-test")
        monkeypatch.setenv("RUN_ID", "run-sig")
        monkeypatch.setenv("EXPERIMENT_ID", "smoke_test")
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))

        # init_config succeeds, but signal arrives during it — simulated by
        # toggling the module flag inside the patched init_config.
        def init_then_signal(*args, **kwargs):
            import pmf_engine.runner.main as _m
            _m._shutdown_requested = True

        # Use AsyncMock so the patched run_experiment is a valid coroutine
        # — without this, the test "passes" incidentally because MagicMock
        # is not awaitable and the broad exception handler catches it.
        async_run = AsyncMock()

        with patch("pmf_engine.runner.main.init_config", side_effect=init_then_signal):
            with patch(
                "pmf_engine.runner.main.RunnerConfig.from_env",
                return_value=config,
            ):
                with patch("pmf_engine.runner.main.publish") as mock_publish:
                    with patch("pmf_engine.runner.main.run_experiment", side_effect=async_run):
                        with pytest.raises(SystemExit) as exc_info:
                            await main()

        assert exc_info.value.code == 1
        async_run.assert_not_called(), (
            "run_experiment must NOT be reached after a pre-task signal — "
            "the runner should exit before launching the agent task"
        )
        # We should have sent a failed callback with reason_code Signal.
        signal_calls = [
            c for c in mock_publish.report_status.call_args_list
            if c[0][0] == "failed"
            and "Signal" in (c.kwargs.get("reason_code", "") or "")
        ]
        assert signal_calls, (
            f"expected failed-status with reason_code='Signal' on pre-task "
            f"shutdown; got calls: {mock_publish.report_status.call_args_list!r}"
        )


# ---------------------------------------------------------------------------
# Write-action manifest end-to-end (ENG-10234)
#
# Asserts the full chain: manifest_loader.load_from_broker returns a write-
# action manifest → RunnerConfig.from_env extracts the three new fields →
# run_experiment passes them to the harness → ClaudeAgentOptions reflects
# them all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_action_manifest_flows_through_to_claude_agent_options(
    monkeypatch, tmp_path
):
    """ENG-10234: a write-action manifest with system_prompt /
    permission_mode / allowed_external_tools flows end-to-end. The dispatch
    side (ENG-10128) routes the SQS message; this test covers the runner side
    that consumes the resulting manifest and builds ClaudeAgentOptions for
    the Fargate task's Claude SDK session.
    """
    from claude_agent_sdk import ResultMessage

    from pmf_engine.runner.harness.claude_sdk import ALLOWED_TOOLS, ClaudeSdkHarness

    synthetic_envelope = {
        "manifest": {
            "id": "compliance_setup",
            "version": 1,
            "mode": "win",
            "model": "sonnet",
            "max_turns": 60,
            "timeout_seconds": 1200,
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {
                "type": "object",
                "properties": {"stage": {"type": "string"}},
            },
            "system_prompt": "You are setting up TCR compliance for a candidate.",
            "permission_mode": "default",
            "allowed_external_tools": ["Read"],
        },
        "instruction": "# Compliance setup\n\nDo the thing.",
        "attachments": {},
    }

    monkeypatch.setattr(
        "pmf_engine.runner.manifest_loader.load_from_broker",
        lambda **kwargs: synthetic_envelope,
    )
    monkeypatch.setenv("EXPERIMENT_ID", "compliance_setup")
    monkeypatch.setenv("RUN_ID", "run-write-001")
    monkeypatch.setenv("ORGANIZATION_SLUG", "org-test")
    monkeypatch.setenv("BROKER_URL", "https://broker-dev.test")
    monkeypatch.setenv("BROKER_TOKEN", "tok-end-to-end")
    # `local` is outside _AWS_DEPLOYMENT_ENVS so the https-only guard is a
    # no-op — keeps the synthetic broker URL valid for the test boundary.
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("PARAMS_JSON", "{}")
    monkeypatch.delenv("PMF_AGENT_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("INSTRUCTION", raising=False)

    # Step 1: loader → from_env populates the three RunnerConfig fields.
    config = RunnerConfig.from_env()
    assert config.system_prompt == "You are setting up TCR compliance for a candidate."
    assert config.permission_mode == "default"
    assert config.allowed_external_tools == ["Read"]
    assert config.instruction == "# Compliance setup\n\nDo the thing."

    # Step 2: run_experiment + real ClaudeSdkHarness → ClaudeAgentOptions
    # carries all three. Use the real harness with `query` patched at the
    # SDK boundary; this is the lowest-mocking point that still proves the
    # options shape without launching a real agent process.
    captured: dict = {}

    async def fake_query(prompt, options):
        captured["options"] = options
        yield ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-e2e",
            total_cost_usd=0.01,
            result="Done",
        )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "output").mkdir()
    (workspace / "output" / "result.json").write_text(json.dumps({"stage": "done"}))

    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))

    with patch(
        "pmf_engine.runner.harness.claude_sdk.query", side_effect=fake_query
    ), patch("pmf_engine.runner.main.publish"), patch(
        "pmf_engine.runner.main._upload_logs"
    ):
        harness = ClaudeSdkHarness()
        await run_experiment(config, harness=harness)

    options = captured["options"]
    # system_prompt prepended above the capability section.
    assert options.system_prompt.startswith(
        "You are setting up TCR compliance for a candidate.\n"
    )
    # permission_mode overrides the bypassPermissions default.
    assert options.permission_mode == "default"
    # allowed_external_tools extended onto ALLOWED_TOOLS, base set preserved.
    assert options.allowed_tools == [*ALLOWED_TOOLS, "Read"]
    # MCP server wired from BROKER_URL + BROKER_TOKEN env.
    assert options.mcp_servers["broker"]["type"] == "http"
    assert options.mcp_servers["broker"]["url"] == "https://broker-dev.test/agent/mcp"
    assert options.mcp_servers["broker"]["headers"] == {
        "Authorization": "Bearer tok-end-to-end"
    }


# ---------------------------------------------------------------------------
# Bearer-token redaction (ENG-10234 hardening — delegate-review finding)
#
# The Claude SDK writes a session JSONL at ~/.claude/projects/**/*.jsonl that
# the runner uploads to S3 via _upload_logs. Pre-ENG-10234 the runner only
# ever passed env-derived secrets to the SDK; the new BROKER_TOKEN bearer
# header inside ClaudeAgentOptions.mcp_servers is potentially serializable
# into that JSONL by SDK internals. The existing _SECRET_PATTERNS only catch
# `key=value`/`key:value` shapes — `Authorization: Bearer <token>` has a
# space the char class doesn't include, so it passes through unredacted.
# ---------------------------------------------------------------------------


class TestBearerTokenRedaction:
    def test_redacts_authorization_header_bearer_token(self):
        from pmf_engine.runner.main import _redact_line

        # JSON-serialized header shape that the SDK could emit into session
        # logs. This is the exact shape that escaped _SECRET_PATTERNS pre-fix.
        line = '{"headers": {"Authorization": "Bearer tok-mcp-123-secret-stuff"}}\n'
        redacted = _redact_line(line)

        assert "tok-mcp-123-secret-stuff" not in redacted
        assert "Bearer " in redacted, "Bearer prefix preserved for diagnostic value"
        assert "REDACTED" in redacted

    def test_redacts_bearer_in_curl_style_header(self):
        from pmf_engine.runner.main import _redact_line

        # Whatever the SDK chooses to emit, the redaction must apply to any
        # `Bearer <token>` shape — header lines, curl reproducers, etc.
        line = "curl -H 'Authorization: Bearer broker-jwt-eyJhbGciOiJIUzI1NiJ9...'"
        redacted = _redact_line(line)

        assert "broker-jwt-eyJhbGciOiJIUzI1NiJ9" not in redacted
        assert "Bearer " in redacted

    def test_redacts_jwt_with_dots_and_dashes(self):
        from pmf_engine.runner.main import _redact_line

        # JWTs contain `.` and `-` — verify the char class covers them.
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        line = f'"auth": "Bearer {jwt}"'
        redacted = _redact_line(line)

        assert jwt not in redacted
        assert "Bearer " in redacted

    def test_short_bearer_value_below_threshold_not_redacted(self):
        """Threshold of 8 chars matches the existing _SECRET_PATTERNS
        convention — tokens shorter than that aren't credentials worth
        guarding against; this avoids redacting words like "Bearer hi"
        in agent-authored prose."""
        from pmf_engine.runner.main import _redact_line

        line = "the Bearer hi was retrieved"
        redacted = _redact_line(line)

        assert redacted == line

    def test_redaction_does_not_destroy_surrounding_json(self):
        """The substitution must leave the surrounding JSON parseable so log
        diffing / cwltail-style tools that consume the redacted file still
        work."""
        import json as _json
        from pmf_engine.runner.main import _redact_line

        original = '{"event": "session_start", "headers": {"Authorization": "Bearer tok-12345678"}}'
        redacted = _redact_line(original)

        # Both ends still parseable as JSON — the redacted portion is a
        # string value, so the JSON structure stays intact.
        parsed = _json.loads(redacted)
        assert parsed["event"] == "session_start"
        assert "tok-12345678" not in parsed["headers"]["Authorization"]
        assert parsed["headers"]["Authorization"].startswith("Bearer ")

