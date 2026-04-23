import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.harness.base import HarnessResult
from pmf_engine.runner.main import run_experiment, get_harness, main


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


def test_get_harness_returns_claude_sdk():
    harness = get_harness("claude_sdk")
    from pmf_engine.runner.harness.claude_sdk import ClaudeSdkHarness
    assert isinstance(harness, ClaudeSdkHarness)


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

    mock_publish.publish.assert_called_once_with({"greeting": "hello"})


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
    assert "running" in statuses
    assert "failed" in statuses
    failed_call = next(c for c in status_calls if c[0][0] == "failed")
    assert "timed out" in failed_call[1]["detail"]


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
async def test_run_experiment_contract_violation_reports_status(mock_publish, _mock_logs):
    config = _make_config(
        contract_schema={"greeting": "string"},
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
    config = _make_config(contract_schema={"greeting": "string"})
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
    config = _make_config(contract_schema={"greeting": "string"})
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
    config = _make_config(contract_schema={"greeting": "string"})
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
    config = _make_config(contract_schema={"greeting": "string"})
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
    def _write_workspace(self, tmpdir: Path, schema: dict, artifacts: dict,
                         constraints: dict | None = None):
        (tmpdir / "output").mkdir(exist_ok=True)
        (tmpdir / "contract_schema.json").write_text(json.dumps(schema))
        if constraints is not None:
            (tmpdir / "contract_constraints.json").write_text(json.dumps(constraints))
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
            schema={"greeting": "string", "count": "number"},
            artifacts={"result.json": {"greeting": "hello", "count": 5}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS" in result.stdout

    def test_fails_on_missing_field_and_lists_it(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema={"greeting": "string", "count": "number"},
            artifacts={"result.json": {"greeting": "hello"}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "FAIL" in result.stdout
        assert "count" in result.stdout

    def test_fails_on_multiple_errors_and_lists_all(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema={"a": "string", "b": "number", "c": "boolean"},
            artifacts={"result.json": {}},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "a" in result.stdout
        assert "b" in result.stdout
        assert "c" in result.stdout

    def test_fails_on_constraint_violation(self, tmp_path):
        self._write_workspace(
            tmp_path,
            schema={"tier": "string"},
            artifacts={"result.json": {"tier": "platinum"}},
            constraints={"enums": [{"path": "tier", "values": ["bronze", "silver", "gold"]}]},
        )
        result = self._run_validator(tmp_path)
        assert result.returncode == 1
        assert "platinum" in result.stdout

    def test_fails_when_output_dir_empty(self, tmp_path):
        (tmp_path / "output").mkdir()
        (tmp_path / "contract_schema.json").write_text(json.dumps({"x": "string"}))
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
            "EXPERIMENT_ID": "voter_targeting",
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
        config = _make_config(contract_schema={"greeting": "string"})
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
                                    with pytest.raises(Exception):
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
        mock_harness = AsyncMock()
        mock_harness.run.side_effect = RuntimeError("Agent crashed")

        monotonic_values = iter([100.0, 130.0, 130.0, 130.0])

        def fake_monotonic():
            try:
                return next(monotonic_values)
            except StopIteration:
                return 130.0

        with patch("pmf_engine.runner.main.time.monotonic", side_effect=fake_monotonic):
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
        config = _make_config(contract_schema={"greeting": "string"})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": 42}',
            content_type="application/json",
            cost_usd=0.13,
            num_turns=2,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        monotonic_values = iter([100.0, 142.5, 142.5, 142.5])

        def fake_monotonic():
            try:
                return next(monotonic_values)
            except StopIteration:
                return 142.5

        with patch("pmf_engine.runner.main.time.monotonic", side_effect=fake_monotonic):
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
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result
        mock_publish.publish.side_effect = Exception("Broker down")

        monotonic_values = iter([100.0, 115.0, 115.0, 115.0])

        def fake_monotonic():
            try:
                return next(monotonic_values)
            except StopIteration:
                return 115.0

        with patch("pmf_engine.runner.main.time.monotonic", side_effect=fake_monotonic):
            with pytest.raises(Exception, match="Broker down"):
                await run_experiment(config, harness=mock_harness)

        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "failed"
        assert call_args[1].get("duration_seconds") == pytest.approx(15.0, abs=0.01)
        assert call_args[1].get("cost_usd") == pytest.approx(0.21, abs=1e-6)


class TestObservabilityHardening:
    @pytest.mark.asyncio
    async def test_main_surfaces_registry_error_in_status(self):
        class BrokenRegistry:
            def get(self, *args, **kwargs):
                raise RuntimeError("Experiment module 'foo' failed to load: FileNotFoundError")

        with patch.dict(os.environ, {
            "EXPERIMENT_ID": "voter_targeting",
            "RUN_ID": "run-reg",
            "ORGANIZATION_SLUG": "org-reg",
            "BROKER_URL": "https://broker.test",
            "BROKER_TOKEN": "tok-test",
            "PARAMS_JSON": "{}",
        }, clear=False):
            for k in ("INSTRUCTION",):
                os.environ.pop(k, None)
            with patch(
                "pmf_engine.control_plane.registry.EXPERIMENT_REGISTRY",
                BrokenRegistry(),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    await main()

        assert exc_info.value.code == 1


class TestMainReportsRunningAtBoot:
    @pytest.mark.asyncio
    async def test_main_reports_running_status_before_experiment(self):
        config = _make_config(instruction="Do stuff")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock):
                                await main()

        first_status_call = mock_publish.report_status.call_args_list[0]
        assert first_status_call[0][0] == "running"


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


class TestMainInitialRunningStatusResilience:
    """Covers the residual of CRITICAL #2: the retry in report_status handles
    ~7s of broker blip but longer outages must not crash Fargate unhandled
    (the SQS dispatch message is already ACK'd, so a crashed task leaves
    the run stuck PENDING forever).

    The initial report_status('running') at the top of main() is the one
    call that, if it raises past all retries, bypasses every try/except in
    the rest of main(). Must swallow-and-log — the agent still runs and
    the terminal callback will reach the broker when it recovers."""

    @pytest.mark.asyncio
    async def test_broker_outage_on_initial_running_status_does_not_crash_main(self):
        import httpx
        config = _make_config(instruction="Do stuff")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.init_config"):
                        with patch("pmf_engine.runner.main.publish") as mock_publish:
                            with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock):
                                request = httpx.Request("POST", "https://broker/internal/run-status")
                                response = httpx.Response(502, request=request)
                                mock_publish.report_status.side_effect = [
                                    httpx.HTTPStatusError(
                                        "upstream 502", request=request, response=response
                                    ),
                                ]
                                await main()

        running_calls = [
            c for c in mock_publish.report_status.call_args_list
            if c.args and c.args[0] == "running"
        ]
        assert len(running_calls) == 1, (
            "initial report_status('running') must fire exactly once — "
            "the subsequent exception must be caught, not bubble past "
            "the main() async frame"
        )
