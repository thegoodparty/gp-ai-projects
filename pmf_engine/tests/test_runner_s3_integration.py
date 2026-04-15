"""End-to-end S3 integration tests using moto.

The existing test_runner_main.py::TestUploadRunLogsTagging suite asserts that
`_upload_run_logs` passes `Tagging="lifecycle=logs"` to a MagicMock S3 client.
That test would silently pass even if the production code renamed the kwarg
(e.g. `Tags=` or `ObjectTagging=`), because MagicMock accepts any kwarg. These
tests use moto's in-memory S3 to verify the tag is actually readable from the
real boto3 GetObjectTagging API after the upload.

They also cover the full `run_experiment` success path — artifact + latest
pointer exist after a successful harness run, and the primary artifact is NOT
tagged `lifecycle=logs` (only diagnostic files are).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.harness.base import HarnessResult
from pmf_engine.runner.main import _upload_run_logs, run_experiment


ARTIFACT_BUCKET = "gp-agent-artifacts-test-moto"
REGION = "us-east-1"


@pytest.fixture
def s3_bucket():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=ARTIFACT_BUCKET)
        yield s3


@pytest.fixture
def sqs_queue():
    with mock_aws():
        sqs = boto3.client("sqs", region_name=REGION)
        resp = sqs.create_queue(
            QueueName="pmf-test-callback.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )
        yield sqs, resp["QueueUrl"]


def _config(**overrides) -> RunnerConfig:
    defaults = {
        "experiment_id": "voter_targeting",
        "run_id": "run-moto-001",
        "candidate_id": "cand-moto-1",
        "instruction": "do the thing",
        "params": {},
        "harness": "claude_sdk",
        "model": "sonnet",
        "environment": "dev",
        "artifact_bucket": ARTIFACT_BUCKET,
        "artifact_key_template": "{experiment_id}/{run_id}/voter_targeting.json",
        "callback_queue_url": "",
        "contract_schema": None,
    }
    defaults.update(overrides)
    return RunnerConfig(**defaults)


class TestUploadRunLogsMoto:
    def test_log_objects_actually_get_lifecycle_logs_tag_in_s3(self, tmp_path):
        """The production kwarg must be `Tagging=` — moto validates the real
        API call so this breaks if the name drifts to Tags/ObjectTagging."""
        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)

            (tmp_path / "scratch.txt").write_text("diagnostic text")
            (tmp_path / "conversation.jsonl").write_text('{"type":"result"}\n')

            config = _config()
            with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
                _upload_run_logs(s3, config, str(tmp_path))

            keys = [
                obj["Key"]
                for obj in s3.list_objects_v2(Bucket=ARTIFACT_BUCKET).get("Contents", [])
            ]
            assert keys, "no objects uploaded — expected at least scratch.txt"

            for key in keys:
                tag_set = s3.get_object_tagging(Bucket=ARTIFACT_BUCKET, Key=key)["TagSet"]
                tag_map = {t["Key"]: t["Value"] for t in tag_set}
                assert tag_map.get("lifecycle") == "logs", (
                    f"key={key!r} uploaded without lifecycle=logs tag; "
                    f"got tag_set={tag_set!r}"
                )

    def test_log_object_content_round_trips_through_s3(self, tmp_path):
        """Contents must actually land in S3 unaltered."""
        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)

            body = 'line-1\nline-2\n{"x": 42}\n'
            (tmp_path / "conversation.jsonl").write_text(body)

            config = _config()
            with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
                _upload_run_logs(s3, config, str(tmp_path))

            obj_keys = [
                obj["Key"]
                for obj in s3.list_objects_v2(Bucket=ARTIFACT_BUCKET).get("Contents", [])
            ]
            matching = [k for k in obj_keys if k.endswith("conversation.jsonl")]
            assert matching, f"conversation.jsonl not uploaded; got {obj_keys}"

            resp = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=matching[0])
            assert resp["Body"].read().decode() == body

    def test_log_keys_use_expected_experiment_run_prefix(self, tmp_path):
        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)

            (tmp_path / "a.txt").write_text("hi")

            config = _config(experiment_id="district_intel", run_id="run-xyz-42")
            with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
                _upload_run_logs(s3, config, str(tmp_path))

            keys = [
                obj["Key"]
                for obj in s3.list_objects_v2(Bucket=ARTIFACT_BUCKET).get("Contents", [])
            ]
            workspace_keys = [k for k in keys if "/logs/workspace/" in k]
            assert workspace_keys, f"expected a workspace log key; got {keys}"
            for k in workspace_keys:
                assert k.startswith("district_intel/run-xyz-42/logs/workspace/"), (
                    f"log key {k!r} missing expected experiment/run prefix"
                )

    def test_sensitive_files_are_not_uploaded(self, tmp_path):
        """Files whose name contains .env, .key, .pem, credentials, or secret
        must be skipped — dumping a .env to a shared log bucket is a severe
        accidental secret exposure."""
        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)

            (tmp_path / "safe.txt").write_text("ok")
            (tmp_path / ".env").write_text("DATABRICKS_TOKEN=abc123")
            (tmp_path / "credentials.json").write_text("{}")
            (tmp_path / "mycert.pem").write_text("-----BEGIN")

            config = _config()
            with patch("pmf_engine.runner.main._find_session_jsonl", return_value=None):
                _upload_run_logs(s3, config, str(tmp_path))

            keys = [
                obj["Key"]
                for obj in s3.list_objects_v2(Bucket=ARTIFACT_BUCKET).get("Contents", [])
            ]
            assert any(k.endswith("safe.txt") for k in keys)
            for banned in (".env", "credentials.json", "mycert.pem"):
                assert not any(k.endswith(banned) for k in keys), (
                    f"sensitive file {banned} leaked into S3: {keys}"
                )


class TestRunExperimentMoto:
    @pytest.mark.asyncio
    async def test_success_path_uploads_artifact_and_latest_pointer(self):
        """run_experiment must PUT both the experiment-specific artifact key AND
        the `{experiment_id}/latest.json` canonical pointer with identical bytes.
        gp-webapp reads the latest pointer for the canonical artifact URL."""
        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)
            sqs = boto3.client("sqs", region_name=REGION)

            artifact_json = json.dumps({"ok": True, "x": 42}).encode()
            fake_result = HarnessResult(
                artifact_bytes=artifact_json,
                content_type="application/json",
                cost_usd=0.01,
                num_turns=1,
                session_id="sess-moto",
            )
            mock_harness = AsyncMock()
            mock_harness.run.return_value = fake_result

            config = _config()

            with patch("pmf_engine.runner.main._upload_run_logs"), \
                 patch("pmf_engine.runner.main._send_callback"), \
                 patch("pmf_engine.runner.main._emit_run_metrics"):
                await run_experiment(config, harness=mock_harness, s3_client=s3, sqs_client=sqs)

            expected_key = "voter_targeting/run-moto-001/voter_targeting.json"
            resp = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=expected_key)
            assert resp["Body"].read() == artifact_json

            latest_key = "voter_targeting/latest.json"
            latest = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=latest_key)
            assert latest["Body"].read() == artifact_json

    @pytest.mark.asyncio
    async def test_primary_artifact_has_no_lifecycle_logs_tag(self):
        """The lifecycle=logs tag must be applied ONLY to diagnostic files so
        the S3 lifecycle rule expires logs without touching artifacts. A primary
        artifact tagged `lifecycle=logs` would disappear unexpectedly."""
        with mock_aws():
            s3 = boto3.client("s3", region_name=REGION)
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)
            sqs = boto3.client("sqs", region_name=REGION)

            fake_result = HarnessResult(
                artifact_bytes=b'{"hi":"x"}',
                content_type="application/json",
                cost_usd=0.0,
                num_turns=1,
                session_id="sess",
            )
            mock_harness = AsyncMock()
            mock_harness.run.return_value = fake_result

            config = _config()

            with patch("pmf_engine.runner.main._upload_run_logs"), \
                 patch("pmf_engine.runner.main._send_callback"), \
                 patch("pmf_engine.runner.main._emit_run_metrics"):
                await run_experiment(config, harness=mock_harness, s3_client=s3, sqs_client=sqs)

            for key in (
                "voter_targeting/run-moto-001/voter_targeting.json",
                "voter_targeting/latest.json",
            ):
                tag_set = s3.get_object_tagging(Bucket=ARTIFACT_BUCKET, Key=key)["TagSet"]
                tag_map = {t["Key"]: t["Value"] for t in tag_set}
                assert tag_map.get("lifecycle") != "logs", (
                    f"primary artifact {key} was tagged lifecycle=logs — "
                    f"lifecycle rule would expire it"
                )
