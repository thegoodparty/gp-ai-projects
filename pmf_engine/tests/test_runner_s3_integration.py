"""Broker-based publish integration tests.

These replace the moto-based S3 tests that verified direct S3 uploads.
The runner now delegates all artifact publishing and log uploads to
pmf_runtime.publish, which talks to the broker API. These tests verify
the runner correctly calls the publish module in success, failure, and
contract violation paths.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.harness.base import HarnessResult
from pmf_engine.runner.main import run_experiment, _collect_workspace_files


def _config(**overrides) -> RunnerConfig:
    defaults = {
        "experiment_id": "smoke_test",
        "run_id": "run-broker-001",
        "organization_slug": "org-broker-1",
        "instruction": "do the thing",
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


class TestPublishIntegration:
    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_logs")
    @patch("pmf_engine.runner.main.publish")
    async def test_success_path_publishes_artifact(self, mock_publish, _mock_logs):
        artifact_dict = {"ok": True, "x": 42}
        artifact_json = json.dumps(artifact_dict).encode()
        fake_result = HarnessResult(
            artifact_bytes=artifact_json,
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
            session_id="sess-broker",
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result
        config = _config()

        await run_experiment(config, harness=mock_harness)

        mock_publish.publish.assert_called_once()
        assert mock_publish.publish.call_args[0] == (artifact_dict,)

    @pytest.mark.asyncio
    @patch("pmf_engine.runner.main._upload_logs")
    @patch("pmf_engine.runner.main.publish")
    async def test_contract_violation_does_not_publish(self, mock_publish, _mock_logs):
        config = _config(contract_schema={"greeting": "string"})
        fake_result = HarnessResult(
            artifact_bytes=b'{"greeting": 42}',
            content_type="application/json",
            cost_usd=0.01,
            num_turns=1,
        )
        mock_harness = AsyncMock()
        mock_harness.run.return_value = fake_result

        await run_experiment(config, harness=mock_harness)

        mock_publish.publish.assert_not_called()
        mock_publish.report_status.assert_called_once()
        call_args = mock_publish.report_status.call_args
        assert call_args[0][0] == "contract_violation"
        assert call_args[1]["rejected_artifact"] == {"greeting": 42}


class TestCollectWorkspaceFiles:
    def test_collects_safe_files_and_skips_sensitive(self, tmp_path):
        (tmp_path / "safe.txt").write_text("ok")
        (tmp_path / ".env").write_text("SECRET=abc")
        (tmp_path / "credentials.json").write_text("{}")
        (tmp_path / "mycert.pem").write_text("-----BEGIN")

        files = _collect_workspace_files(str(tmp_path))

        names = set(files.keys())
        assert any("safe.txt" in n for n in names)
        for banned in (".env", "credentials.json", "mycert.pem"):
            assert not any(banned in n for n in names), (
                f"sensitive file {banned} leaked into collected files: {names}"
            )

    def test_file_content_preserved(self, tmp_path):
        body = "line-1\nline-2\n"
        (tmp_path / "data.txt").write_text(body)

        files = _collect_workspace_files(str(tmp_path))

        matching = [v for k, v in files.items() if k.endswith("data.txt")]
        assert matching
        assert matching[0] == body.encode()

    def test_returns_empty_for_nonexistent_dir(self):
        files = _collect_workspace_files("/nonexistent/path/xyz")
        assert files == {}
