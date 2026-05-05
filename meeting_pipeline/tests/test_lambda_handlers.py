"""
Lambda handler tests for meeting_pipeline.lambda_handlers.{scan,process,discover}.

These live on the meeting-pipeline-infra branch with the handlers themselves.
"""
import json
from unittest.mock import MagicMock, patch


# ────────────────────────────────────────────────────────────────────────────
# In-memory storage shim — duplicated from test_pipeline.py to keep this file
# self-contained (the handler tests live on a different branch from the rest
# of test_pipeline.py during the staged review of meeting-pipeline + infra).
# ────────────────────────────────────────────────────────────────────────────

class FakeStorage:
    """Minimal in-memory StorageBackend for testing (no S3 needed)."""

    def __init__(self, data: dict | None = None):
        self._store: dict[str, bytes] = {}
        if data:
            for k, v in data.items():
                if isinstance(v, dict):
                    self._store[k] = json.dumps(v).encode()
                else:
                    self._store[k] = v

    def read_json(self, key: str) -> dict:
        raw = self._store.get(key)
        if raw is None:
            raise FileNotFoundError(key)
        return json.loads(raw)

    def write_json(self, key: str, data: dict) -> None:
        self._store[key] = json.dumps(data).encode()

    def write_bytes(self, key: str, data: bytes) -> None:
        self._store[key] = data

    def read_bytes(self, key: str) -> bytes:
        raw = self._store.get(key)
        if raw is None:
            raise FileNotFoundError(key)
        return raw

    def exists(self, key: str) -> bool:
        return key in self._store

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def get_size(self, key: str) -> int:
        raw = self._store.get(key)
        return len(raw) if raw else 0

    def append_line(self, key: str, line: str) -> None:
        pass

    def get_presigned_url(self, key: str, expiry_seconds: int = 300) -> str:
        return f"https://fake-presigned/{key}"


# ────────────────────────────────────────────────────────────────────────────
# Import smoke tests — every handler module loads without errors.
# ────────────────────────────────────────────────────────────────────────────

class TestImports:
    @patch("boto3.client")
    def test_import_lambda_scan(self, mock_boto):
        from meeting_pipeline.lambda_handlers import scan  # noqa: F401

    @patch("boto3.client")
    def test_import_lambda_process(self, mock_boto):
        from meeting_pipeline.lambda_handlers import process  # noqa: F401

    @patch("boto3.client")
    def test_import_lambda_discover(self, mock_boto):
        from meeting_pipeline.lambda_handlers import discover  # noqa: F401


# ────────────────────────────────────────────────────────────────────────────
# Scan handler — list_cities mode + single-city scan + new-posted detection.
# ────────────────────────────────────────────────────────────────────────────

class TestScanHandler:
    @patch("boto3.client")
    def test_list_cities_returns_verified_slugs(self, mock_boto):
        from meeting_pipeline.lambda_handlers.scan import handler

        source_verified = {
            "city": "TestCity",
            "state": "OH",
            "best_source": {
                "platform": "civicplus",
                "verification": {"status": "verified"},
            },
        }
        source_unverified = {
            "city": "BadCity",
            "state": "TX",
            "best_source": {
                "platform": "unknown",
                "verification": {"status": "unverified"},
            },
        }

        storage = FakeStorage({
            "meeting_pipeline/sources/test-city-OH/source.json": source_verified,
            "meeting_pipeline/sources/bad-city-TX/source.json": source_unverified,
        })

        with patch("meeting_pipeline.lambda_handlers.scan.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.scan.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.scan.get_storage", return_value=storage):
            cfg = MagicMock()
            cfg.sources_prefix = "meeting_pipeline/sources"
            mock_cfg.return_value = cfg
            result = handler({"action": "list_cities"})

        assert "cities" in result
        slugs = [c["slug"] for c in result["cities"]]
        assert "test-city-OH" in slugs
        assert "bad-city-TX" not in slugs

    @patch("boto3.client")
    def test_scan_requires_slug(self, mock_boto):
        with patch("meeting_pipeline.lambda_handlers.scan.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.scan.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.scan.get_storage"):
            mock_cfg.return_value = MagicMock()
            from meeting_pipeline.lambda_handlers.scan import handler
            result = handler({})
        assert "error" in result

    @patch("boto3.client")
    def test_scan_sends_new_posted_to_queue(self, mock_boto):
        """When scan finds a newly posted agenda, it sends to SQS."""
        from meeting_pipeline.lambda_handlers.scan import _detect_new_posted

        previous = {
            "upcoming": [
                {"date": "2026-04-10", "agenda_posted": True},
            ]
        }
        current = {
            "upcoming": [
                {"date": "2026-04-10", "agenda_posted": True},
                {"date": "2026-04-17", "agenda_posted": True},
            ]
        }
        new = _detect_new_posted(previous, current)
        assert len(new) == 1
        assert new[0]["date"] == "2026-04-17"

    @patch("boto3.client")
    def test_detect_new_posted_first_scan(self, mock_boto):
        """First scan (no previous): all posted meetings are new."""
        from meeting_pipeline.lambda_handlers.scan import _detect_new_posted

        current = {
            "upcoming": [
                {"date": "2026-04-10", "agenda_posted": True},
                {"date": "2026-04-17", "agenda_posted": False},
            ]
        }
        new = _detect_new_posted(None, current)
        assert len(new) == 1
        assert new[0]["date"] == "2026-04-10"


# ────────────────────────────────────────────────────────────────────────────
# Process handler — SQS record parsing + inline event support.
# ────────────────────────────────────────────────────────────────────────────

class TestProcessHandler:
    @patch("boto3.client")
    def test_sqs_record_parsing(self, mock_boto):
        """Handler correctly parses SQS Records wrapping."""
        from meeting_pipeline.lambda_handlers.process import handler

        event = {
            "Records": [
                {
                    "body": json.dumps({
                        "slug": "test-city-OH",
                        "date": "2026-04-15",
                        "platform": "civicplus",
                    }),
                }
            ]
        }

        with patch("meeting_pipeline.lambda_handlers.process.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.process.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.process.get_storage"), \
             patch("meeting_pipeline.lambda_handlers.process._process_meeting") as mock_proc:
            mock_cfg.return_value = MagicMock()
            mock_proc.return_value = {"status": "ok"}
            handler(event)

        mock_proc.assert_called_once()
        call_args = mock_proc.call_args
        assert call_args[0][0] == "test-city-OH"
        assert call_args[0][1] == "2026-04-15"
        assert call_args[0][2] == "civicplus"

    @patch("boto3.client")
    def test_process_handler_inline_event(self, mock_boto):
        """Handler also supports inline event (not wrapped in Records)."""
        from meeting_pipeline.lambda_handlers.process import handler

        event = {
            "slug": "test-city-OH",
            "date": "2026-04-15",
            "platform": "legistar",
        }

        with patch("meeting_pipeline.lambda_handlers.process.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.process.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.process.get_storage"), \
             patch("meeting_pipeline.lambda_handlers.process._process_meeting") as mock_proc:
            mock_cfg.return_value = MagicMock()
            mock_proc.return_value = {"status": "ok"}
            result = handler(event)

        mock_proc.assert_called_once()
        assert result["results"] == [{"status": "ok"}]


# ────────────────────────────────────────────────────────────────────────────
# Scan handler — must enqueue SQS BEFORE persisting upcoming_meetings.json,
# otherwise an SQS failure mid-loop loses the un-enqueued items: on retry,
# the persisted state is already the new one, so _detect_new_posted returns []
# and the missed messages are silently dropped.
# ────────────────────────────────────────────────────────────────────────────

class TestScanPersistAfterSend:
    @patch("boto3.client")
    def test_sqs_failure_leaves_previous_state_intact(self, mock_boto):
        """Regression: when sqs.send_message raises mid-loop, the old
        upcoming_meetings.json is preserved so the Step Function retry
        re-detects the un-enqueued meetings."""
        from meeting_pipeline.lambda_handlers import scan as scan_mod

        previous = {"upcoming": [], "platform": "civicplus"}
        new_result = {
            "upcoming": [
                {"date": "2099-01-15", "agenda_posted": True},  # far future
                {"date": "2099-01-22", "agenda_posted": True},
                {"date": "2099-01-29", "agenda_posted": True},
            ],
            "platform": "civicplus",
        }

        storage = FakeStorage({
            "meeting_pipeline/sources/test-OH/source.json": {
                "city": "Test", "state": "OH",
                "best_source": {"verification": {"status": "verified"}},
            },
            "meeting_pipeline/sources/test-OH/upcoming_meetings.json": previous,
        })

        # Make SQS raise after the first send to simulate a transient failure
        sqs_calls = {"count": 0}
        def boom_after_first(*args, **kwargs):
            sqs_calls["count"] += 1
            if sqs_calls["count"] >= 2:
                raise RuntimeError("simulated SQS failure")

        with patch.object(scan_mod, "sqs") as mock_sqs, \
             patch.object(scan_mod, "PROCESS_QUEUE_URL", "https://sqs/process"), \
             patch("meeting_pipeline.lambda_handlers.scan.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.scan.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.scan.get_storage", return_value=storage), \
             patch("meeting_pipeline.lambda_handlers.scan._scan", new=AsyncMock(return_value=new_result)):
            mock_cfg.return_value = MagicMock(sources_prefix="meeting_pipeline/sources")
            mock_sqs.send_message.side_effect = boom_after_first
            try:
                scan_mod.handler({"slug": "test-OH"})
            except RuntimeError:
                pass  # expected

        # Persisted state must still be the OLD one so retry can re-detect
        persisted = storage.read_json("meeting_pipeline/sources/test-OH/upcoming_meetings.json")
        assert persisted == previous, (
            "regression: upcoming_meetings.json was overwritten before SQS "
            "completed — retry will see the new state and lose un-enqueued items"
        )


# ────────────────────────────────────────────────────────────────────────────
# Discover Lambda module must not require playwright/chromium (Dockerfile
# was claiming to need them via a non-existent --extra discover, but no code
# actually imports playwright).
# ────────────────────────────────────────────────────────────────────────────

class TestDiscoverNoPlaywright:
    @patch("boto3.client")
    def test_discover_module_does_not_import_playwright(self, mock_boto):
        import sys
        # Force a fresh import path by clearing relevant cache entries
        for mod_name in list(sys.modules):
            if mod_name.startswith("meeting_pipeline.lambda_handlers.discover"):
                del sys.modules[mod_name]
        from meeting_pipeline.lambda_handlers import discover  # noqa: F401
        assert "playwright" not in sys.modules
        assert "playwright.async_api" not in sys.modules


# ────────────────────────────────────────────────────────────────────────────
# Discover poll_loop robustness — the always-on Fargate task must not crash
# on transient SQS errors or malformed messages.
# ────────────────────────────────────────────────────────────────────────────

import pytest  # noqa: E402


class TestDiscoverPollLoopRobustness:
    @patch("boto3.client")
    def test_receive_message_error_does_not_crash(self, mock_boto):
        """A receive_message failure must be caught — otherwise the container
        exits and ECS restarts it on every transient SQS/STS hiccup."""
        from meeting_pipeline.lambda_handlers import discover as d

        sentinel = SystemExit("stop")
        with patch.object(d, "DISCOVER_QUEUE_URL", "https://sqs.test/q"), \
             patch.object(d, "inject_secrets"), \
             patch.object(d, "AgentConfig"), \
             patch.object(d, "get_storage"), \
             patch.object(d.sqs, "receive_message", side_effect=[
                 Exception("boto3 retry exhausted"),
                 sentinel,
             ]) as mock_recv, \
             patch.object(d.time, "sleep") as mock_sleep:
            with pytest.raises(SystemExit):
                d.poll_loop()

        assert mock_recv.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch("boto3.client")
    def test_malformed_json_message_is_deleted(self, mock_boto):
        """A poison-pill message (invalid JSON) must be deleted, otherwise it
        crashes the loop on every redelivery until DLQ."""
        from meeting_pipeline.lambda_handlers import discover as d

        sentinel = SystemExit("stop")
        with patch.object(d, "DISCOVER_QUEUE_URL", "https://sqs.test/q"), \
             patch.object(d, "inject_secrets"), \
             patch.object(d, "AgentConfig"), \
             patch.object(d, "get_storage"), \
             patch.object(d.sqs, "receive_message", side_effect=[
                 {"Messages": [{"Body": "not json{", "ReceiptHandle": "rh1"}]},
                 sentinel,
             ]), \
             patch.object(d.sqs, "delete_message") as mock_delete:
            with pytest.raises(SystemExit):
                d.poll_loop()

        mock_delete.assert_called_once_with(
            QueueUrl="https://sqs.test/q", ReceiptHandle="rh1"
        )


# ────────────────────────────────────────────────────────────────────────────
# process.py briefing-skipped handling — generate_briefing_for_meeting can
# return status="skipped" for permanent outcomes (too_few_items,
# too_few_substantive_items, pipeline_excluded). Those must NOT route to
# briefing_failed (which is in TRANSIENT_FAILURE_STATUSES → re-raised → SQS
# redelivers → false DLQ pages on every sparse-agenda meeting).
# ────────────────────────────────────────────────────────────────────────────

class TestProcessSkippedBriefing:
    @patch("boto3.client")
    def test_skipped_briefing_does_not_trigger_retry(self, mock_boto):
        """Handler must not raise when _process_meeting returns skipped."""
        from meeting_pipeline.lambda_handlers.process import handler

        event = {"slug": "test-OH", "date": "2026-04-15", "platform": "civicplus"}

        with patch("meeting_pipeline.lambda_handlers.process.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.process.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.process.get_storage"), \
             patch("meeting_pipeline.lambda_handlers.process._process_meeting") as mock_proc:
            mock_cfg.return_value = MagicMock()
            mock_proc.return_value = {"status": "skipped", "reason": "too_few_items"}
            # Must not raise — skipped is a permanent no-op, not a transient error.
            result = handler(event)

        assert result["results"][0]["status"] == "skipped"

    @patch("boto3.client")
    def test_briefing_failed_does_trigger_retry(self, mock_boto):
        """Sanity check: briefing_failed (real transient errors) still raises."""
        from meeting_pipeline.lambda_handlers.process import handler

        event = {"slug": "test-OH", "date": "2026-04-15", "platform": "civicplus"}

        with patch("meeting_pipeline.lambda_handlers.process.inject_secrets"), \
             patch("meeting_pipeline.lambda_handlers.process.AgentConfig.from_env") as mock_cfg, \
             patch("meeting_pipeline.lambda_handlers.process.get_storage"), \
             patch("meeting_pipeline.lambda_handlers.process._process_meeting") as mock_proc:
            mock_cfg.return_value = MagicMock()
            mock_proc.return_value = {"status": "briefing_failed", "error": "API timeout"}
            with pytest.raises(RuntimeError, match="Transient processing failures"):
                handler(event)


# ────────────────────────────────────────────────────────────────────────────
# process.py must surface CollectionResult.error_result(...) as collect_failed
# rather than letting it fall through to no_pdf (permanent skip → SQS deletes).
# ────────────────────────────────────────────────────────────────────────────

class TestProcessCollectErrorVisibility:
    @patch("boto3.client")
    def test_collector_error_result_routes_to_collect_failed(self, mock_boto):
        """When process_one_city returns CollectionResult with error set (no
        exception), _process_meeting must report collect_failed so SQS retries
        instead of silently dropping the message via no_pdf."""
        from meeting_pipeline.lambda_handlers.process import _process_meeting
        from meeting_pipeline.shared.models import CollectionResult

        cfg = MagicMock(sources_prefix="meeting_pipeline/sources")
        storage = FakeStorage({
            "meeting_pipeline/sources/test-OH/source.json": {"city": "Test", "state": "OH"},
        })

        async def fake_collect(*args, **kwargs):
            return CollectionResult.error_result("Test", "OH", "civicclerk", "tenant lookup failed")

        # Mock find_best_pdf to return no PDF before AND after collect, so the
        # control flow reaches the collect path and the post-collect retry.
        with patch("meeting_pipeline.stages.extract.normalize.find_best_pdf",
                   return_value=(None, None)), \
             patch("meeting_pipeline.stages.collect.process.process_one_city",
                   new=fake_collect):
            result = _process_meeting("test-OH", "2026-04-15", "civicclerk", cfg, storage)

        assert result["status"] == "collect_failed"
        assert "tenant lookup failed" in result.get("error", "")


# Need AsyncMock import at module level for ScanPersistAfterSend
from unittest.mock import AsyncMock  # noqa: E402
