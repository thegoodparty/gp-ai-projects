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
from pmf_engine.runner.main import run_experiment, get_harness, main, _upload_run_logs


def _make_config(**overrides):
    defaults = {
        "experiment_id": "hello_world",
        "run_id": "run-001",
        "candidate_id": "cand-123",
        "instruction": "Write result.json",
        "params": {},
        "harness": "claude_sdk",
        "model": "sonnet",
        "environment": "dev",
        "artifact_bucket": "gp-agent-artifacts-dev",
        "artifact_key_template": "{experiment_id}/{run_id}/result.json",
        "callback_queue_url": "https://sqs.us-west-2.amazonaws.com/123/agent-callback-dev.fifo",
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
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_uploads_artifact_and_sends_callback(_mock_logs):
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

    fake_s3 = FakeS3Client()
    fake_sqs = FakeSQSClient()

    await run_experiment(config, harness=mock_harness, s3_client=fake_s3, sqs_client=fake_sqs)

    artifact_calls = [
        c for c in fake_s3.put_object_calls
        if c.get("Key") == "hello_world/run-001/result.json"
    ]
    assert len(artifact_calls) == 1
    assert artifact_calls[0]["Body"] == b'{"greeting": "hello"}'
    assert artifact_calls[0]["ContentType"] == "application/json"

    assert len(fake_sqs.calls) == 1
    call_kwargs = fake_sqs.calls[0]
    assert call_kwargs["QueueUrl"] == "https://sqs.us-west-2.amazonaws.com/123/agent-callback-dev.fifo"
    body = json.loads(call_kwargs["MessageBody"])
    assert body["status"] == "success"
    assert body["experiment_id"] == "hello_world"
    assert body["run_id"] == "run-001"
    assert body["candidate_id"] == "cand-123"
    assert body["artifact_key"] == "hello_world/run-001/result.json"
    assert body["artifact_bucket"] == "gp-agent-artifacts-dev"
    assert body["cost_usd"] == 0.05


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_sends_error_callback_on_harness_failure(_mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = RuntimeError("Agent crashed")

    fake_s3 = FakeS3Client()
    fake_sqs = FakeSQSClient()

    with pytest.raises(RuntimeError, match="Agent crashed"):
        await run_experiment(config, harness=mock_harness, s3_client=fake_s3, sqs_client=fake_sqs)

    assert fake_s3.put_object_calls == []

    assert len(fake_sqs.calls) == 1
    body = json.loads(fake_sqs.calls[0]["MessageBody"])
    assert body["status"] == "failed"
    assert body["error"] == "Agent crashed"


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_sends_error_callback_on_s3_failure(_mock_logs):
    config = _make_config()
    fake_result = HarnessResult(
        artifact_bytes=b"data",
        content_type="application/json",
        cost_usd=0.01,
        num_turns=1,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result

    mock_s3 = MagicMock()
    mock_s3.put_object.side_effect = Exception("S3 upload failed")
    mock_sqs = MagicMock()

    with pytest.raises(Exception, match="S3 upload failed"):
        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "S3 upload failed" in body["error"]


@pytest.mark.asyncio
async def test_main_writes_instruction_to_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(instruction="# Test Instruction\n\nDo the thing.")

        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.run_experiment", new_callable=AsyncMock):
                    with patch("pmf_engine.runner.main.boto3"):
                        await main()

        instruction_path = os.path.join(tmpdir, "instruction.md")
        assert os.path.exists(instruction_path)
        with open(instruction_path) as f:
            assert f.read() == "# Test Instruction\n\nDo the thing."


@pytest.mark.asyncio
async def test_main_sends_failed_callback_on_timeout():
    config = _make_config(instruction="Do stuff", timeout_seconds=1)
    mock_sqs = MagicMock()

    async def slow_run(*args, **kwargs):
        await asyncio.sleep(10)

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.run_experiment", side_effect=slow_run):
                    with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                        mock_boto3.client.return_value = mock_sqs
                        with pytest.raises(SystemExit):
                            await main()

    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "timed out" in body["error"]


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_contract_violation_no_s3_upload(_mock_logs):
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

    fake_s3 = FakeS3Client()
    fake_sqs = FakeSQSClient()

    await run_experiment(config, harness=mock_harness, s3_client=fake_s3, sqs_client=fake_sqs)

    keys = [c.get("Key", "") for c in fake_s3.put_object_calls]
    assert "hello_world/run-001/result.json" not in keys
    assert not any("latest.json" in k for k in keys)

    assert len(fake_sqs.calls) == 1
    body = json.loads(fake_sqs.calls[0]["MessageBody"])
    assert body["status"] == "contract_violation"
    assert "greeting" in body["error"]


class FakeSQSClient:
    def __init__(self, fail_times: int = 0, fail_forever: bool = False):
        self._fail_times = fail_times
        self._fail_forever = fail_forever
        self.calls: list[dict] = []

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_forever:
            raise Exception("SQS permanently down")
        if len(self.calls) <= self._fail_times:
            raise Exception(f"SQS transient failure #{len(self.calls)}")
        return {"MessageId": f"msg-{len(self.calls)}"}


class FakeCloudWatchClient:
    def __init__(self):
        self.metric_calls: list[dict] = []

    def put_metric_data(self, **kwargs):
        self.metric_calls.append(kwargs)


def test_send_callback_retries_on_sqs_failure():
    from pmf_engine.runner.main import _send_callback

    config = _make_config()
    fake_sqs = FakeSQSClient(fail_times=2)

    with patch("pmf_engine.runner.main.time.sleep") as mock_sleep:
        _send_callback(
            fake_sqs, config, "success",
            artifact_key="hello_world/run-001/result.json",
            cost_usd=0.05, duration_seconds=1.0,
        )

    assert len(fake_sqs.calls) == 3
    assert mock_sleep.call_count == 2


def test_send_callback_emits_orphan_metric_on_terminal_retry_exhaustion():
    from pmf_engine.runner.main import _send_callback

    config = _make_config()
    fake_sqs = FakeSQSClient(fail_forever=True)
    fake_cw = FakeCloudWatchClient()

    with patch("pmf_engine.runner.main.time.sleep"):
        with patch("pmf_engine.runner.main.boto3") as mock_boto3:
            mock_boto3.client.return_value = fake_cw
            with pytest.raises(Exception, match="SQS permanently down"):
                _send_callback(
                    fake_sqs, config, "success",
                    artifact_key="hello_world/run-001/result.json",
                    cost_usd=0.05, duration_seconds=1.0,
                )

    assert len(fake_sqs.calls) == 3
    assert len(fake_cw.metric_calls) == 1
    metric_call = fake_cw.metric_calls[0]
    assert metric_call["Namespace"] == "PMFEngine"
    metric_names = [m["MetricName"] for m in metric_call["MetricData"]]
    assert "OrphanedCallback" in metric_names
    orphan_metric = next(m for m in metric_call["MetricData"] if m["MetricName"] == "OrphanedCallback")
    assert orphan_metric["Value"] == 1
    dim_names = {d["Name"]: d["Value"] for d in orphan_metric["Dimensions"]}
    assert dim_names.get("ExperimentId") == "hello_world"


def test_orphaned_callback_logs_s3_key_for_recovery():
    import logging
    import pmf_engine.runner.main as main_module
    from pmf_engine.runner.main import _send_callback

    config = _make_config()
    fake_sqs = FakeSQSClient(fail_forever=True)
    fake_cw = FakeCloudWatchClient()

    captured_records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            captured_records.append(record)

    handler = ListHandler(level=logging.ERROR)
    main_module.logger.addHandler(handler)
    try:
        with patch("pmf_engine.runner.main.time.sleep"):
            with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                mock_boto3.client.return_value = fake_cw
                with pytest.raises(Exception, match="SQS permanently down"):
                    _send_callback(
                        fake_sqs, config, "success",
                        artifact_key="hello_world/run-001/result.json",
                        cost_usd=0.05, duration_seconds=1.0,
                    )
    finally:
        main_module.logger.removeHandler(handler)

    error_msgs = [r.getMessage() for r in captured_records if r.levelno >= logging.ERROR]
    combined = "\n".join(error_msgs)
    assert "hello_world/run-001/result.json" in combined
    assert "run-001" in combined
    assert "hello_world" in combined
    assert "cand-123" in combined


def test_send_callback_non_terminal_swallows_sqs_failure():
    from pmf_engine.runner.main import _send_callback

    config = _make_config()
    fake_sqs = FakeSQSClient(fail_forever=True)

    with patch("pmf_engine.runner.main.time.sleep"):
        _send_callback(fake_sqs, config, "progress", error="still working")

    assert len(fake_sqs.calls) == 1


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_no_output_file_sends_failed_callback(_mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = FileNotFoundError("No output files found in /workspace/output")

    mock_s3 = MagicMock()
    mock_sqs = MagicMock()

    with pytest.raises(FileNotFoundError, match="No output files"):
        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

    mock_s3.put_object.assert_not_called()
    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "No output files" in body["error"]


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_multiple_output_files_sends_failed_callback(_mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = RuntimeError("Expected 1 output file, found 3")

    mock_s3 = MagicMock()
    mock_sqs = MagicMock()

    with pytest.raises(RuntimeError, match="Expected 1 output file"):
        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

    mock_s3.put_object.assert_not_called()
    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "Expected 1 output file" in body["error"]


@pytest.mark.asyncio
async def test_main_exits_on_missing_experiment_id():
    empty_config = _make_config(experiment_id="", instruction="something")
    mock_sqs = MagicMock()

    with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=empty_config):
        with patch("pmf_engine.runner.main.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_sqs
            with pytest.raises(SystemExit) as exc_info:
                await main()
    assert exc_info.value.code == 1

    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "EXPERIMENT_ID" in body["error"]


@pytest.mark.asyncio
async def test_main_exits_on_missing_instruction():
    no_instruction_config = _make_config(instruction="")
    mock_sqs = MagicMock()

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=no_instruction_config):
                with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                    mock_boto3.client.return_value = mock_sqs
                    with pytest.raises(SystemExit) as exc_info:
                        await main()
    assert exc_info.value.code == 1

    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "instruction" in body["error"].lower()


@pytest.mark.asyncio
async def test_main_sends_failed_callback_on_signal():
    import pmf_engine.runner.main as main_module

    config = _make_config(instruction="Do stuff", timeout_seconds=30)
    mock_sqs = MagicMock()

    async def interrupted_run(*args, **kwargs):
        main_module._shutdown_requested = True
        raise Exception("interrupted")

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
            with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                with patch("pmf_engine.runner.main.run_experiment", side_effect=interrupted_run):
                    with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                        mock_boto3.client.return_value = mock_sqs
                        with pytest.raises(SystemExit):
                            await main()

    main_module._shutdown_requested = False

    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
    assert body["status"] == "failed"
    assert "signal" in body["error"]


def _make_mock_bt():
    mock_span = MagicMock()
    mock_bt = MagicMock()

    @contextmanager
    def fake_traced_span(**kwargs):
        yield mock_span

    mock_bt.traced_span.side_effect = fake_traced_span
    return mock_bt, mock_span


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_traces_success_to_braintrust(_mock_logs):
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
    mock_s3 = MagicMock()
    mock_sqs = MagicMock()
    mock_bt, mock_span = _make_mock_bt()

    with patch("pmf_engine.runner.main.BraintrustClient.get_instance", return_value=mock_bt):
        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

    mock_bt.init.assert_called_once_with("pmf-engine")

    call_kwargs = mock_bt.traced_span.call_args[1]
    assert call_kwargs["name"] == "experiment:hello_world"
    assert call_kwargs["input_data"]["experiment_id"] == "hello_world"
    assert call_kwargs["input_data"]["run_id"] == "run-001"
    assert call_kwargs["input_data"]["candidate_id"] == "cand-123"
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
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_traces_failure_to_braintrust(_mock_logs):
    config = _make_config()
    mock_harness = AsyncMock()
    mock_harness.run.side_effect = RuntimeError("Agent crashed")
    mock_s3 = MagicMock()
    mock_sqs = MagicMock()
    mock_bt, mock_span = _make_mock_bt()

    with patch("pmf_engine.runner.main.BraintrustClient.get_instance", return_value=mock_bt):
        with pytest.raises(RuntimeError, match="Agent crashed"):
            await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

    mock_span.log.assert_called_once()
    log_kwargs = mock_span.log.call_args[1]
    assert log_kwargs["output"]["status"] == "failed"
    assert "Agent crashed" in log_kwargs["output"]["error"]

    mock_bt.flush.assert_called_once()


@pytest.mark.asyncio
@patch("pmf_engine.runner.main._upload_run_logs")
async def test_run_experiment_traces_contract_violation_to_braintrust(_mock_logs):
    config = _make_config(contract_schema={"greeting": "string"})
    fake_result = HarnessResult(
        artifact_bytes=b'{"greeting": 42}',
        content_type="application/json",
        cost_usd=0.02,
        num_turns=2,
    )
    mock_harness = AsyncMock()
    mock_harness.run.return_value = fake_result
    mock_s3 = MagicMock()
    mock_sqs = MagicMock()
    mock_bt, mock_span = _make_mock_bt()

    with patch("pmf_engine.runner.main.BraintrustClient.get_instance", return_value=mock_bt):
        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

    mock_span.log.assert_called_once()
    log_kwargs = mock_span.log.call_args[1]
    assert log_kwargs["output"]["status"] == "contract_violation"
    assert "greeting" in log_kwargs["output"]["error"]

    mock_bt.flush.assert_called_once()


class TestMainErrorPaths:
    """Regression tests for 5 CRITICAL error-reporting issues in main().

    Each test encodes a specific contract:
    - harness failure path must not send the callback twice (inner + outer handler race)
    - outer-exception callback must fire even when run_id is empty (no orphaned PENDING runs)
    - SQS client init failure must emit a CloudWatch BootstrapFailure metric
    - TimeoutError path must upload run logs before sending the failed callback
    """

    @pytest.mark.asyncio
    async def test_main_does_not_double_send_callback_on_harness_failure(self):
        """run_experiment's inner handler already sent 'failed'; main's outer
        handler must not send a second duplicate callback."""
        config = _make_config(instruction="Do stuff", timeout_seconds=30)
        mock_sqs = MagicMock()
        mock_s3 = MagicMock()

        fake_harness = AsyncMock()
        fake_harness.run.side_effect = RuntimeError("Agent crashed")

        def client_factory(service, **kwargs):
            if service == "sqs":
                return mock_sqs
            if service == "s3":
                return mock_s3
            return MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.get_harness", return_value=fake_harness):
                        with patch("pmf_engine.runner.main._upload_run_logs"):
                            with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                                mock_boto3.client.side_effect = client_factory
                                with pytest.raises((Exception, SystemExit)):
                                    await main()

        assert mock_sqs.send_message.call_count == 1, (
            f"Expected exactly 1 callback after harness failure, "
            f"got {mock_sqs.send_message.call_count}"
        )

    @pytest.mark.asyncio
    async def test_main_sends_failed_callback_even_when_run_id_is_empty(self):
        """If an unhandled error occurs and run_id is missing, we still must
        send a callback — otherwise gp-api has no signal and the run stays
        PENDING forever."""
        config = _make_config(run_id="", instruction="Do stuff", timeout_seconds=30)
        mock_sqs = MagicMock()

        async def crash(*args, **kwargs):
            raise RuntimeError("Unexpected runner crash")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.run_experiment", side_effect=crash):
                        with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                            mock_boto3.client.return_value = mock_sqs
                            with pytest.raises(RuntimeError):
                                await main()

        assert mock_sqs.send_message.call_count == 1
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "failed"
        assert "Unexpected runner crash" in body["error"]

    @pytest.mark.asyncio
    async def test_main_emits_bootstrap_failure_metric_when_sqs_client_init_fails(self):
        """If boto3.client('sqs') fails at startup, the runner can't send a
        callback — so it must at least emit a CloudWatch metric so an alarm
        can fire."""
        config = _make_config(instruction="Do stuff")
        mock_cw = MagicMock()

        def client_factory(service, **kwargs):
            if service == "sqs":
                raise RuntimeError("Network down")
            if service == "cloudwatch":
                return mock_cw
            return MagicMock()

        with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
            with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                mock_boto3.client.side_effect = client_factory
                with pytest.raises(SystemExit):
                    await main()

        mock_cw.put_metric_data.assert_called_once()
        metric_call = mock_cw.put_metric_data.call_args[1]
        assert metric_call["Namespace"] == "PMFEngine"
        metric_names = [m["MetricName"] for m in metric_call["MetricData"]]
        assert "BootstrapFailure" in metric_names
        bootstrap_metric = next(
            m for m in metric_call["MetricData"] if m["MetricName"] == "BootstrapFailure"
        )
        assert bootstrap_metric["Value"] == 1
        dim_names = {d["Name"]: d["Value"] for d in bootstrap_metric["Dimensions"]}
        assert dim_names.get("Reason") == "SQSClientInit"

    @pytest.mark.asyncio
    async def test_main_uploads_run_logs_on_timeout(self):
        """Timeout is the #1 debug case — logs must be uploaded before the
        failed callback is sent, so the operator can see how far the agent got."""
        config = _make_config(instruction="Do stuff", timeout_seconds=1)
        mock_sqs = MagicMock()

        async def slow_run(*args, **kwargs):
            await asyncio.sleep(10)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.run_experiment", side_effect=slow_run):
                        with patch("pmf_engine.runner.main._upload_run_logs") as mock_upload:
                            with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                                mock_boto3.client.return_value = mock_sqs
                                with pytest.raises(SystemExit):
                                    await main()

        mock_upload.assert_called_once()
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "failed"
        assert "timed out" in body["error"]


class FakeS3Client:
    def __init__(self):
        self.put_object_calls: list[dict] = []

    def put_object(self, **kwargs):
        self.put_object_calls.append(kwargs)
        return {}


class TestUploadRunLogsTagging:
    """Run logs must be tagged so the S3 lifecycle rule can expire them without
    also expiring canonical artifacts. Artifacts are keyed like
    `{experiment_id}/{run_id}/artifact.json` and must live indefinitely because
    peer_city_benchmarking reads prior district_intel artifacts; logs share the
    same key prefix (`{experiment_id}/{run_id}/logs/...`) so prefix-based
    lifecycle rules cannot distinguish them. A `lifecycle=logs` object tag lets
    the bucket policy expire only the log objects."""

    def test_uploaded_log_objects_carry_lifecycle_logs_tag(self, tmp_path):
        (tmp_path / "scratch.txt").write_text("workspace note")
        (tmp_path / "conversation.jsonl").write_text('{"type":"result"}\n')

        fake_s3 = FakeS3Client()
        config = _make_config()

        with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
            _upload_run_logs(fake_s3, config, str(tmp_path))

        assert fake_s3.put_object_calls, "expected at least one put_object call"
        for call in fake_s3.put_object_calls:
            assert call.get("Tagging") == "lifecycle=logs", (
                f"log upload missing lifecycle tag: key={call.get('Key')!r} "
                f"tagging={call.get('Tagging')!r}"
            )

    def test_uploaded_session_jsonl_also_carries_lifecycle_tag(self, tmp_path):
        fake_session = tmp_path / "session.jsonl"
        fake_session.write_text('{"type":"assistant"}\n')
        fake_s3 = FakeS3Client()
        config = _make_config()

        with patch("pmf_engine.runner.main._find_session_jsonl", return_value=str(fake_session)):
            _upload_run_logs(fake_s3, config, str(tmp_path))

        session_calls = [c for c in fake_s3.put_object_calls if c["Key"].endswith("session.jsonl")]
        assert session_calls, "expected a session.jsonl upload call"
        for call in session_calls:
            assert call.get("Tagging") == "lifecycle=logs"


class TestValidatorScriptShim:
    """The in-container validator script must reuse pmf_engine.runner.contract
    rather than re-implementing validation logic. The agent invokes
    `python3 /workspace/validate_output.py` — behavior must match contract.py."""

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


class TestLatestPointerWrite:
    """Task #9.1 — every successful run must also publish `{experiment_id}/latest.json`
    so downstream experiments (peer_city_benchmarking reads district_intel/latest.json)
    can find the canonical latest artifact. The Fargate task-role IAM already grants
    s3:GetObject on `*/latest.json`; without this write, the first real dispatch
    of peer_city_benchmarking would 404."""

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_run_logs")
    async def test_success_path_writes_latest_pointer(self, _mock_logs):
        config = _make_config(
            experiment_id="district_intel",
            artifact_key_template="{experiment_id}/{run_id}/district_intel.json",
        )
        fake_result = HarnessResult(
            artifact_bytes=b'{"ok": true}',
            content_type="application/json",
            cost_usd=0.02,
            num_turns=2,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        await run_experiment(
            config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs
        )

        calls = mock_s3.put_object.call_args_list
        keys = [c.kwargs["Key"] for c in calls]
        assert "district_intel/run-001/district_intel.json" in keys
        assert "district_intel/latest.json" in keys

        latest_call = next(c for c in calls if c.kwargs["Key"] == "district_intel/latest.json")
        assert latest_call.kwargs["Body"] == b'{"ok": true}'
        assert latest_call.kwargs["ContentType"] == "application/json"

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_run_logs")
    async def test_contract_violation_does_not_publish_latest(self, _mock_logs):
        config = _make_config(contract_schema={"greeting": "string"})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": 42}',
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        await run_experiment(
            config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs
        )

        calls = mock_s3.put_object.call_args_list
        keys = [c.kwargs.get("Key", "") for c in calls]
        assert not any("latest.json" in k for k in keys), (
            f"Contract violation must not publish latest pointer, got keys: {keys}"
        )


class TestContractViolationRejectedArtifact:
    """Task #9.2 — on contract violation, upload the offending artifact to
    `{run_id}/rejected.json` with `lifecycle=logs` tag. Currently the failing
    bytes are discarded and debugging requires CloudWatch log archaeology."""

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_run_logs")
    async def test_rejected_artifact_uploaded_with_logs_tag(self, _mock_logs):
        config = _make_config(contract_schema={"greeting": "string"})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": 42}',
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        await run_experiment(
            config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs
        )

        rejected_calls = [
            c for c in mock_s3.put_object.call_args_list
            if c.kwargs.get("Key", "").endswith("rejected.json")
        ]
        assert len(rejected_calls) == 1
        rejected = rejected_calls[0]
        assert rejected.kwargs["Body"] == b'{"greeting": 42}'
        assert rejected.kwargs["Tagging"] == "lifecycle=logs"
        assert rejected.kwargs["Key"].startswith("hello_world/run-001/")


class TestObservabilityHardening:
    """Task #5 — log upload failures, session redaction, registry error propagation."""

    def test_upload_run_logs_emits_error_metric_when_all_fail(self, tmp_path):
        (tmp_path / "scratch.log").write_text("hi")
        (tmp_path / "conv.jsonl").write_text('{"type":"assistant"}\n')

        class AllFailingS3:
            def __init__(self):
                self.calls = []

            def put_object(self, **kwargs):
                self.calls.append(kwargs)
                raise Exception("S3 bucket not found")

        fake_s3 = AllFailingS3()
        fake_cw = FakeCloudWatchClient()
        config = _make_config()

        import logging
        import pmf_engine.runner.main as main_module
        from shared.aws_clients import reset_client_cache

        captured: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = CaptureHandler(level=logging.ERROR)
        main_module.logger.addHandler(handler)
        reset_client_cache()
        try:
            with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
                with patch("shared.aws_clients.boto3") as mock_boto3:
                    mock_boto3.client.return_value = fake_cw
                    _upload_run_logs(fake_s3, config, str(tmp_path))
        finally:
            main_module.logger.removeHandler(handler)
            reset_client_cache()

        error_records = [r for r in captured if r.levelno >= logging.ERROR]
        assert any("log upload" in r.getMessage().lower() for r in error_records), (
            f"Expected ERROR log when all uploads fail, got: {[r.getMessage() for r in captured]}"
        )
        metric_names = [
            m["MetricName"] for call in fake_cw.metric_calls for m in call["MetricData"]
        ]
        assert "RunLogUploadFailed" in metric_names

    def test_upload_run_logs_no_error_metric_when_partial_success(self, tmp_path):
        (tmp_path / "good.log").write_text("ok")
        (tmp_path / "bad.log").write_text("also ok")

        class PartialS3:
            def __init__(self):
                self.calls = 0

            def put_object(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise Exception("transient")
                return {}

        fake_s3 = PartialS3()
        fake_cw = FakeCloudWatchClient()
        config = _make_config()

        from shared.aws_clients import reset_client_cache
        reset_client_cache()
        try:
            with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
                with patch("shared.aws_clients.boto3") as mock_boto3:
                    mock_boto3.client.return_value = fake_cw
                    _upload_run_logs(fake_s3, config, str(tmp_path))
        finally:
            reset_client_cache()

        metric_names = [
            m["MetricName"] for call in fake_cw.metric_calls for m in call["MetricData"]
        ]
        assert "RunLogUploadFailed" not in metric_names, (
            "Partial upload failure must not trip the all-fail alarm"
        )

    def test_redact_session_jsonl_emits_error_metric_on_failure(self, tmp_path):
        source = tmp_path / "session.jsonl"
        source.write_text('{"type":"assistant"}\n')

        from pmf_engine.runner.main import _redact_session_jsonl
        from shared.aws_clients import reset_client_cache

        fake_cw = FakeCloudWatchClient()

        import logging
        import pmf_engine.runner.main as main_module

        captured: list[logging.LogRecord] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record)

        handler = CaptureHandler(level=logging.ERROR)
        main_module.logger.addHandler(handler)
        reset_client_cache()
        try:
            with patch("pmf_engine.runner.main.tempfile.mkstemp", side_effect=OSError("disk full")):
                with patch("shared.aws_clients.boto3") as mock_boto3:
                    mock_boto3.client.return_value = fake_cw
                    result = _redact_session_jsonl(str(source))
        finally:
            main_module.logger.removeHandler(handler)
            reset_client_cache()

        assert result is None
        error_records = [r for r in captured if r.levelno >= logging.ERROR]
        assert any("redact" in r.getMessage().lower() for r in error_records), (
            f"Expected ERROR log for redaction failure, got: {[r.getMessage() for r in captured]}"
        )
        metric_names = [
            m["MetricName"] for call in fake_cw.metric_calls for m in call["MetricData"]
        ]
        assert "SessionRedactionFailed" in metric_names

    @pytest.mark.asyncio
    async def test_main_surfaces_registry_error_in_callback(self):
        mock_sqs = MagicMock()

        class BrokenRegistry:
            def get(self, *args, **kwargs):
                raise RuntimeError("Experiment module 'foo' failed to load: FileNotFoundError")

        def client_factory(service, **kwargs):
            if service == "sqs":
                return mock_sqs
            if service == "cloudwatch":
                return MagicMock()
            return MagicMock()

        with patch.dict(os.environ, {
            "EXPERIMENT_ID": "voter_targeting",
            "RUN_ID": "run-reg",
            "CANDIDATE_ID": "cand-reg",
            "CALLBACK_QUEUE_URL": "https://sqs.us-west-2.amazonaws.com/123/q.fifo",
            "ARTIFACT_BUCKET": "test-bucket",
            "PARAMS_JSON": "{}",
        }, clear=False):
            for k in ("INSTRUCTION",):
                os.environ.pop(k, None)
            with patch(
                "pmf_engine.control_plane.registry.EXPERIMENT_REGISTRY",
                BrokenRegistry(),
            ):
                with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                    mock_boto3.client.side_effect = client_factory
                    with pytest.raises(SystemExit):
                        await main()

        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "failed"
        assert "Experiment module 'foo' failed" in body["error"]


class TestCallbackLifecycleFix:
    """Task #2: runner callback lifecycle correctness.

    Covers:
    - Exception chaining doesn't duplicate callbacks (contextvar-backed marker)
    - S3 artifact upload failure uploads logs before failed callback
    - Corrupt PARAMS_JSON raises loudly instead of silent {} default
    - Bootstrap config failure emits metric + sends best-effort callback
    - MessageGroupId falls back to "bootstrap" when candidate_id empty
    - Non-ContractViolation validator errors upload logs and send failed callback
    - Success-callback failure does NOT trigger a second failed callback
    """

    @pytest.mark.asyncio
    async def test_exception_chaining_does_not_duplicate_callback(self):
        config = _make_config(instruction="Do stuff", timeout_seconds=30)
        mock_sqs = MagicMock()

        async def inner_send_and_chain(*args, **kwargs):
            from pmf_engine.runner.main import _send_callback, _mark_callback_sent
            _send_callback(mock_sqs, config, "failed", error="Agent crashed")
            _mark_callback_sent()
            try:
                raise RuntimeError("Agent crashed")
            except RuntimeError as e:
                raise ValueError("wrapped") from e

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.run_experiment", side_effect=inner_send_and_chain):
                        with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                            mock_boto3.client.return_value = mock_sqs
                            with pytest.raises((ValueError, Exception)):
                                await main()

        assert mock_sqs.send_message.call_count == 1, (
            f"Expected exactly 1 callback despite exception chaining, "
            f"got {mock_sqs.send_message.call_count}"
        )

    @pytest.mark.asyncio
    async def test_s3_artifact_failure_uploads_logs_before_callback(self):
        config = _make_config()
        fake_result = HarnessResult(
            artifact_bytes=b"data",
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = Exception("S3 upload failed")
        mock_sqs = MagicMock()

        call_order = []

        def track_upload_logs(*args, **kwargs):
            call_order.append("upload_logs")

        def track_send_callback(*args, **kwargs):
            call_order.append(f"send_callback:{args[2] if len(args) > 2 else kwargs.get('status', '?')}")

        with patch("pmf_engine.runner.main._upload_run_logs", side_effect=track_upload_logs):
            with patch("pmf_engine.runner.main._send_callback", side_effect=track_send_callback):
                with pytest.raises(Exception, match="S3 upload failed"):
                    await run_experiment(
                        config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs
                    )

        assert "upload_logs" in call_order, f"Expected _upload_run_logs to be called, got: {call_order}"
        assert "send_callback:failed" in call_order
        assert call_order.index("upload_logs") < call_order.index("send_callback:failed"), (
            f"Expected log upload BEFORE failed callback, got: {call_order}"
        )

    def test_corrupt_params_json_raises(self):
        with patch.dict(os.environ, {
            "PARAMS_JSON": "not-json",
            "EXPERIMENT_ID": "hello_world",
            "RUN_ID": "run-001",
            "CANDIDATE_ID": "cand-123",
            "INSTRUCTION": "x",
            "AGENT_MODEL": "sonnet",
        }):
            with pytest.raises(ValueError, match="PARAMS_JSON"):
                RunnerConfig.from_env()

    @pytest.mark.asyncio
    async def test_bootstrap_config_failure_emits_metric_and_sends_callback(self):
        mock_sqs = MagicMock()
        mock_cw = MagicMock()

        def client_factory(service, **kwargs):
            if service == "sqs":
                return mock_sqs
            if service == "cloudwatch":
                return mock_cw
            return MagicMock()

        with patch.dict(os.environ, {
            "RUN_ID": "run-boot",
            "CANDIDATE_ID": "cand-boot",
            "EXPERIMENT_ID": "voter_targeting",
            "CALLBACK_QUEUE_URL": "https://sqs.us-west-2.amazonaws.com/123/q.fifo",
            "ARTIFACT_BUCKET": "test-bucket",
            "ENVIRONMENT": "dev",
        }):
            with patch(
                "pmf_engine.runner.main.RunnerConfig.from_env",
                side_effect=ValueError("Invalid TIMEOUT_SECONDS"),
            ):
                with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                    mock_boto3.client.side_effect = client_factory
                    with pytest.raises(SystemExit):
                        await main()

        mock_cw.put_metric_data.assert_called()
        metric_call = mock_cw.put_metric_data.call_args[1]
        assert metric_call["Namespace"] == "PMFEngine"
        metric_names = [m["MetricName"] for m in metric_call["MetricData"]]
        assert "BootstrapFailure" in metric_names
        bootstrap_metric = next(
            m for m in metric_call["MetricData"] if m["MetricName"] == "BootstrapFailure"
        )
        dim_names = {d["Name"]: d["Value"] for d in bootstrap_metric["Dimensions"]}
        assert dim_names.get("Reason") == "ConfigLoad"

        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "failed"
        assert body["run_id"] == "run-boot"
        assert body["candidate_id"] == "cand-boot"
        assert body["experiment_id"] == "voter_targeting"
        assert "Invalid TIMEOUT_SECONDS" in body["error"]

    def test_send_callback_uses_bootstrap_group_id_when_candidate_empty(self):
        from pmf_engine.runner.main import _send_callback
        config = _make_config(candidate_id="")
        fake_sqs = FakeSQSClient()

        _send_callback(fake_sqs, config, "failed", error="bootstrap")

        assert len(fake_sqs.calls) == 1
        assert fake_sqs.calls[0]["MessageGroupId"] == "bootstrap"

    @pytest.mark.asyncio
    async def test_validator_non_contract_violation_uploads_logs_and_sends_failed(self):
        config = _make_config(contract_schema={"greeting": "string"})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": "hello"}',
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        with patch(
            "pmf_engine.runner.main.validate_artifact_contract",
            side_effect=TypeError("schema author bug"),
        ):
            with patch("pmf_engine.runner.main._upload_run_logs") as mock_upload:
                with pytest.raises(TypeError, match="schema author bug"):
                    await run_experiment(
                        config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs
                    )

        mock_upload.assert_called()
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "failed"
        assert "schema author bug" in body["error"]
        mock_s3.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_sigterm_handler_cancels_current_task(self):
        """The SIGTERM handler must actually cancel the running asyncio task,
        not just flip a boolean. The previous implementation set
        _shutdown_requested=True but nothing in asyncio.wait_for ever read the
        flag — so ECS SIGTERM was a complete no-op until SIGKILL."""
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
    async def test_main_cancels_task_and_sends_failed_callback_on_external_cancel(self):
        """End-to-end: SIGTERM handler cancels the running run_experiment task,
        main() catches CancelledError, uploads logs, and sends a failed
        callback with error mentioning 'signal' — not 'Unhandled error'."""
        config = _make_config(instruction="Do stuff", timeout_seconds=30)
        mock_sqs = MagicMock()

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
                    with patch("pmf_engine.runner.main.run_experiment", side_effect=long_running):
                        with patch("pmf_engine.runner.main._upload_run_logs"):
                            with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                                mock_boto3.client.return_value = mock_sqs
                                canceller = asyncio.ensure_future(cancel_soon())
                                with pytest.raises(SystemExit):
                                    await main()
                                try:
                                    await canceller
                                except Exception:
                                    pass

        assert cancelled_from_inside, "run_experiment task was not actually cancelled"
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "failed"
        assert "signal" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_success_callback_failure_does_not_trigger_failed_callback(self):
        config = _make_config()
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": "hello"}',
            content_type="application/json",
            cost_usd=0.05,
            num_turns=3,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        fake_sqs = FakeSQSClient(fail_forever=True)
        mock_s3 = MagicMock()
        fake_cw = FakeCloudWatchClient()

        def client_factory(service, **kwargs):
            if service == "sqs":
                return fake_sqs
            if service == "s3":
                return mock_s3
            if service == "cloudwatch":
                return fake_cw
            return MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"WORKSPACE_DIR": tmpdir}):
                with patch("pmf_engine.runner.main.RunnerConfig.from_env", return_value=config):
                    with patch("pmf_engine.runner.main.get_harness", return_value=mock_harness):
                        with patch("pmf_engine.runner.main._upload_run_logs"):
                            with patch("pmf_engine.runner.main.time.sleep"):
                                with patch("pmf_engine.runner.main.boto3") as mock_boto3:
                                    mock_boto3.client.side_effect = client_factory
                                    with pytest.raises(Exception):
                                        await main()

        statuses = [json.loads(c["MessageBody"])["status"] for c in fake_sqs.calls]
        assert "failed" not in statuses, (
            f"Expected no failed callback after success-path orphan, got: {statuses}"
        )
        assert all(s == "success" for s in statuses)
