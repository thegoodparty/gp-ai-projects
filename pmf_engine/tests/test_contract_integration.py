import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.harness.base import HarnessResult
from pmf_engine.runner.harness.claude_sdk import build_system_prompt
from pmf_engine.runner.main import run_experiment


def _make_config(**overrides):
    defaults = {
        "experiment_id": "test_exp",
        "run_id": "run-001",
        "organization_slug": "org-123",
        "instruction": "Write result.json",
        "params": {},
        "harness": "claude_sdk",
        "model": "sonnet",
        "environment": "dev",
        "broker_url": "https://broker.test",
        "broker_token": "tok-test",
        "contract_schema": None,
    }
    defaults.update(overrides)
    return RunnerConfig(**defaults)


@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
class TestRunnerContractValidation:
    @pytest.mark.asyncio
    async def test_valid_artifact_publishes_via_broker(self, mock_publish, _mock_logs):
        schema = {"greeting": "string"}
        config = _make_config(contract_schema=schema)
        artifact = json.dumps({"greeting": "hello"}).encode()

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )

        await run_experiment(config, harness=mock_harness)

        mock_publish.publish.assert_called_once_with({"greeting": "hello"})

    @pytest.mark.asyncio
    async def test_invalid_artifact_reports_contract_violation(self, mock_publish, _mock_logs):
        schema = {"greeting": "string", "count": "number"}
        config = _make_config(contract_schema=schema)
        artifact = json.dumps({"greeting": "hello"}).encode()

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )

        await run_experiment(config, harness=mock_harness)

        mock_publish.publish.assert_not_called()
        mock_publish.report_status.assert_called_once()
        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "contract_violation"
        assert "count" in call_args[1]["detail"]

    @pytest.mark.asyncio
    async def test_no_schema_skips_validation_and_publishes(self, mock_publish, _mock_logs):
        config = _make_config(contract_schema=None)
        artifact = b'{"result": "ok"}'

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )

        await run_experiment(config, harness=mock_harness)

        mock_publish.publish.assert_called_once_with({"result": "ok"})


@patch("pmf_engine.runner.main._upload_logs")
@patch("pmf_engine.runner.main.publish")
class TestPublishFailure:
    @pytest.mark.asyncio
    async def test_publish_failure_reports_failed_status(self, mock_publish, _mock_logs):
        config = _make_config(contract_schema=None)
        artifact = json.dumps({"result": "ok"}).encode()

        mock_harness = AsyncMock()
        mock_harness.run.return_value = HarnessResult(
            artifact_bytes=artifact, content_type="application/json",
        )
        mock_publish.publish.side_effect = Exception("Broker unreachable")

        with pytest.raises(Exception, match="Broker unreachable"):
            await run_experiment(config, harness=mock_harness)

        mock_publish.report_status.assert_called_once()
        assert mock_publish.report_status.call_args[0][0] == "failed"

    @pytest.mark.asyncio
    async def test_harness_failure_reports_failed_status(self, mock_publish, _mock_logs):
        config = _make_config(contract_schema=None)

        mock_harness = AsyncMock()
        mock_harness.run.side_effect = RuntimeError("Agent crashed")

        with pytest.raises(RuntimeError, match="Agent crashed"):
            await run_experiment(config, harness=mock_harness)

        mock_publish.report_status.assert_called_once()
        assert mock_publish.report_status.call_args[0][0] == "failed"


class TestCollectWorkspaceFilesSecurity:
    def test_skips_env_files(self, tmp_path):
        from pmf_engine.runner.main import _collect_workspace_files
        (tmp_path / "data.json").write_text('{"ok": true}')
        (tmp_path / ".env").write_text("SECRET_KEY=hunter2")
        (tmp_path / "credentials.json").write_text('{"token": "abc"}')
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "config.key").write_text("private key data")

        files = _collect_workspace_files(str(tmp_path))
        filenames = [k.split("/")[-1] for k in files.keys()]

        assert "data.json" in filenames
        assert ".env" not in filenames
        assert "credentials.json" not in filenames
        assert "config.key" not in filenames

    def test_respects_aggregate_cap(self, tmp_path):
        from pmf_engine.runner.main import _collect_workspace_files
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_bytes(b"x" * 1024 * 1024)

        files = _collect_workspace_files(str(tmp_path), max_total_size=5 * 1024 * 1024)
        assert len(files) < 10

    def test_allowlist_only_permits_safe_extensions(self, tmp_path):
        from pmf_engine.runner.main import _collect_workspace_files
        (tmp_path / "data.json").write_text('{"ok": true}')
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
        (tmp_path / "script.py").write_text("import os")
        (tmp_path / "archive.tar.gz").write_bytes(b"\x1f\x8b")
        (tmp_path / ".env").write_text("SECRET=x")

        allowed = {".json", ".txt", ".pdf"}
        files = _collect_workspace_files(str(tmp_path), allowed_extensions=allowed)
        filenames = {k.split("/")[-1] for k in files.keys()}

        assert filenames == {"data.json", "notes.txt", "report.pdf"}

    def test_blocklist_still_applies_under_allowlist(self, tmp_path):
        from pmf_engine.runner.main import _collect_workspace_files
        (tmp_path / "credentials.json").write_text('{"token": "abc"}')
        (tmp_path / "data.json").write_text('{"ok": true}')

        files_blocklist = _collect_workspace_files(str(tmp_path))
        assert len(files_blocklist) == 1
        assert list(files_blocklist.keys())[0].endswith("data.json")

        files_allowlist = _collect_workspace_files(str(tmp_path), allowed_extensions={".json"})
        assert len(files_allowlist) == 1
        assert list(files_allowlist.keys())[0].endswith("data.json")


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
