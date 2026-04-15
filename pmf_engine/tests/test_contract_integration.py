import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.contract import ContractViolation
from pmf_engine.runner.harness.base import HarnessResult
from pmf_engine.runner.harness.claude_sdk import build_system_prompt
from pmf_engine.runner.main import run_experiment


def _make_config(**overrides):
    defaults = {
        "experiment_id": "test_exp",
        "run_id": "run-001",
        "candidate_id": "cand-123",
        "instruction": "Write result.json",
        "params": {},
        "harness": "claude_sdk",
        "model": "sonnet",
        "environment": "dev",
        "artifact_bucket": "test-bucket",
        "artifact_key_template": "{experiment_id}/{run_id}/result.json",
        "callback_queue_url": "https://sqs.example.com/queue.fifo",
        "contract_schema": None,
    }
    defaults.update(overrides)
    return RunnerConfig(**defaults)


@patch("pmf_engine.runner.main._upload_run_logs")
class TestRunnerContractValidation:
    @pytest.mark.asyncio
    async def test_valid_artifact_uploads_and_succeeds(self, _mock_logs):
        schema = {"greeting": "string"}
        config = _make_config(contract_schema=schema)
        artifact = json.dumps({"greeting": "hello"}).encode()

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        assert "test_exp/run-001/result.json" in keys
        assert "test_exp/latest.json" in keys
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "success"

    @pytest.mark.asyncio
    async def test_invalid_artifact_sends_contract_violation(self, _mock_logs):
        schema = {"greeting": "string", "count": "number"}
        config = _make_config(contract_schema=schema)
        artifact = json.dumps({"greeting": "hello"}).encode()

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

        keys = [c.kwargs.get("Key", "") for c in mock_s3.put_object.call_args_list]
        assert "test_exp/run-001/result.json" not in keys
        assert not any("latest.json" in k for k in keys)
        assert any(k.endswith("rejected.json") for k in keys)
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "contract_violation"
        assert "count" in body["error"]

    @pytest.mark.asyncio
    async def test_no_schema_skips_validation(self, _mock_logs):
        config = _make_config(contract_schema=None)
        artifact = b"not even json"

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()

        await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        assert "test_exp/run-001/result.json" in keys
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["status"] == "success"


@patch("pmf_engine.runner.main.time.sleep", return_value=None)
@patch("pmf_engine.runner.main._upload_run_logs")
class TestCallbackFailure:
    """Terminal callbacks retry 3 times then raise (orphaned-callback path)."""

    @pytest.mark.asyncio
    async def test_success_callback_failure_raises_after_retries(self, _mock_logs, _mock_sleep):
        config = _make_config(contract_schema=None)
        artifact = json.dumps({"result": "ok"}).encode()

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = Exception("SQS unreachable")

        with pytest.raises(Exception, match="SQS unreachable"):
            await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

        assert mock_sqs.send_message.call_count == 3

    @pytest.mark.asyncio
    async def test_failure_callback_failure_raises_after_retries(self, _mock_logs, _mock_sleep):
        config = _make_config(contract_schema=None)

        mock_harness = AsyncMock()
        mock_harness.run.side_effect = RuntimeError("Agent crashed")
        mock_s3 = MagicMock()
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = Exception("SQS unreachable")

        with pytest.raises(Exception, match="SQS unreachable"):
            await run_experiment(config, harness=mock_harness, s3_client=mock_s3, sqs_client=mock_sqs)

        assert mock_sqs.send_message.call_count == 3


class TestUploadRunLogsSecurity:
    """Fix #2: _collect_files should skip sensitive files and have aggregate size cap."""

    def test_collect_files_skips_env_files(self, tmp_path):
        from pmf_engine.runner.main import _collect_files
        (tmp_path / "data.json").write_text('{"ok": true}')
        (tmp_path / ".env").write_text("SECRET_KEY=hunter2")
        (tmp_path / "credentials.json").write_text('{"token": "abc"}')
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "config.key").write_text("private key data")

        files = _collect_files(str(tmp_path), "prefix")
        filenames = [f.split("/")[-1] for _, f in files]

        assert "data.json" in filenames
        assert ".env" not in filenames
        assert "credentials.json" not in filenames
        assert "config.key" not in filenames

    def test_collect_files_respects_aggregate_cap(self, tmp_path):
        from pmf_engine.runner.main import _collect_files
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_bytes(b"x" * 1024 * 1024)

        files = _collect_files(str(tmp_path), "prefix", max_total_size=5 * 1024 * 1024)
        assert len(files) < 10

    def test_collect_files_allowlist_only_permits_safe_extensions(self, tmp_path):
        from pmf_engine.runner.main import _collect_files
        (tmp_path / "data.json").write_text('{"ok": true}')
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
        (tmp_path / "script.py").write_text("import os")
        (tmp_path / "archive.tar.gz").write_bytes(b"\x1f\x8b")
        (tmp_path / ".env").write_text("SECRET=x")

        allowed = {".json", ".txt", ".pdf"}
        files = _collect_files(str(tmp_path), "prefix", allowed_extensions=allowed)
        filenames = {f.split("/")[-1] for _, f in files}

        assert filenames == {"data.json", "notes.txt", "report.pdf"}

    def test_collect_files_allowlist_ignores_blocklist(self, tmp_path):
        """When allowlist is set, blocklist patterns are bypassed (allowlist takes precedence)."""
        from pmf_engine.runner.main import _collect_files
        (tmp_path / "credentials.json").write_text('{"token": "abc"}')

        files_blocklist = _collect_files(str(tmp_path), "prefix")
        assert len(files_blocklist) == 0

        files_allowlist = _collect_files(str(tmp_path), "prefix", allowed_extensions={".json"})
        assert len(files_allowlist) == 1


class TestSessionJsonlRedaction:
    def test_redacts_api_keys(self):
        from pmf_engine.runner.main import _redact_line
        line = '{"content": "export API_KEY=sk-abc123def456ghi789jkl012mno345"}'
        redacted = _redact_line(line)
        assert "REDACTED" in redacted
        assert "sk-abc123def456ghi789jkl012mno345" not in redacted

    def test_redacts_standalone_sk_keys(self):
        from pmf_engine.runner.main import _redact_line
        line = '{"content": "key is sk-abc123def456ghi789jkl012mno345"}'
        redacted = _redact_line(line)
        assert "REDACTED" in redacted
        assert "sk-abc123def456ghi789jkl012mno345" not in redacted

    def test_redacts_aws_access_keys(self):
        from pmf_engine.runner.main import _redact_line
        line = '{"output": "AKIAIOSFODNN7EXAMPLE"}'
        redacted = _redact_line(line)
        assert "REDACTED" in redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted

    def test_redacts_token_assignments(self):
        from pmf_engine.runner.main import _redact_line
        line = 'SECRET_KEY="myverylongsecretvalue123"'
        redacted = _redact_line(line)
        assert "myverylongsecretvalue123" not in redacted
        assert "REDACTED" in redacted

    def test_preserves_normal_content(self):
        from pmf_engine.runner.main import _redact_line
        line = '{"message": "Generated 5 segments for district analysis"}'
        assert _redact_line(line) == line

    def test_redact_session_jsonl_creates_redacted_copy(self, tmp_path):
        from pmf_engine.runner.main import _redact_session_jsonl
        source = tmp_path / "session.jsonl"
        source.write_text(
            '{"line": 1, "content": "normal text"}\n'
            '{"line": 2, "content": "API_KEY=sk-abc123def456ghi789jkl012mno345"}\n'
        )
        redacted_path = _redact_session_jsonl(str(source))
        assert redacted_path is not None
        assert redacted_path != str(source)

        content = open(redacted_path).read()
        assert "normal text" in content
        assert "sk-abc123def456ghi789jkl012mno345" not in content
        assert "REDACTED" in content

        os.unlink(redacted_path)


class TestSystemPromptContractInjection:
    def test_contract_schema_included_in_prompt(self):
        schema = {"name": "string", "items": [{"id": "number"}]}
        prompt = build_system_prompt("Do the task.", contract_schema=schema)
        assert "## OUTPUT CONTRACT" in prompt
        assert '"name": "string"' in prompt
        assert '"id": "number"' in prompt
        assert '"items":' in prompt

    def test_no_schema_no_contract_section(self):
        prompt = build_system_prompt("Do the task.", contract_schema=None)
        assert "OUTPUT CONTRACT" not in prompt

    def test_instruction_still_present(self):
        prompt = build_system_prompt("Do the specific task.", contract_schema={"x": "string"})
        assert "Do the specific task." in prompt
