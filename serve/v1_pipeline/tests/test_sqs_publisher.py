import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from serve.v1_pipeline.models.unified_record import UnifiedCampaignRecord
from serve.v1_pipeline.pipeline.sqs_publisher import SQSEventPublisher


def _make_record(
    phone_number: str,
    poll_id: str,
    cluster_id: int = 1,
    theme: str = "Roads",
    is_opt_out: bool = False,
) -> UnifiedCampaignRecord:
    cluster_data = {
        "15": {
            "cluster_id": cluster_id,
            "cluster_theme": theme,
            "issues_summary": f"Summary for {theme}",
            "detailed_analysis": f"Analysis for {theme}",
            "quotes": [{"quote": "Fix the roads", "phone_number": phone_number}],
        }
    }
    return UnifiedCampaignRecord(
        campaign_id="test-campaign",
        record_id=str(uuid.uuid4()),
        atomic_id=str(uuid.uuid4()),
        phone_number=phone_number,
        message_text="Fix the roads please",
        sent_at=datetime.now(timezone.utc),
        round="R1",
        poll_id=poll_id,
        multi_cluster_data=cluster_data if not is_opt_out else None,
        is_opt_out=is_opt_out,
    )


def _make_config(
    tmp_path: Path,
    publish_to_sqs: bool = False,
    s3_output_path: str = "s3://bucket/prefix/path",
    s3_bucket: str = "test-bucket",
    queue_url: str = "https://sqs.us-west-2.amazonaws.com/123/test-queue.fifo",
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "publish_to_sqs": publish_to_sqs,
        "output_dir": str(tmp_path / "output"),
        "s3_output_path": s3_output_path,
        "s3_bucket": s3_bucket,
        "publish_top_n": 3,
        "min_unique_respondents": 1,
    }
    if publish_to_sqs:
        config["queue_url"] = queue_url
    return config


@pytest.fixture
def call_log():
    return []


@pytest.fixture
def mock_s3(call_log):
    client = MagicMock()

    def put_object(**kwargs):
        call_log.append(("s3_put_object", kwargs))
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    client.put_object.side_effect = put_object
    return client


@pytest.fixture
def mock_sqs(call_log):
    client = MagicMock()

    def send_message(**kwargs):
        call_log.append(("sqs_send_message", kwargs))
        return {"MessageId": str(uuid.uuid4())}

    client.send_message.side_effect = send_message
    return client


@pytest.fixture
def publisher(tmp_path, mock_s3, mock_sqs, call_log):
    json_dir = tmp_path / "output" / "consolidated"
    json_dir.mkdir(parents=True)
    json_file = json_dir / "test-campaign_all_cluster_analysis.json"
    json_file.write_text(json.dumps([{"atomicId": "a1", "phoneNumber": "+1111"}]))

    config = _make_config(tmp_path, publish_to_sqs=True)
    pub = SQSEventPublisher(config, s3_client=mock_s3, sqs_client=mock_sqs)
    return pub


@pytest.fixture
def publisher_no_local_file(tmp_path, mock_s3, mock_sqs, call_log):
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    config = _make_config(tmp_path, publish_to_sqs=True)
    pub = SQSEventPublisher(config, s3_client=mock_s3, sqs_client=mock_sqs)
    return pub


class TestContractCompliance:
    @pytest.mark.asyncio
    async def test_event_structure_matches_gp_api_schema(self, publisher, call_log):
        records = [_make_record("+1111111111", "poll-1")]
        result = await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )

        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        assert len(sqs_calls) == 1
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        assert body["type"] == "pollAnalysisComplete"
        assert "pollId" in body["data"]
        assert "totalResponses" in body["data"]
        assert "responsesLocation" in body["data"]
        assert "issues" in body["data"]
        assert isinstance(body["data"]["issues"], list)

    @pytest.mark.asyncio
    async def test_responses_location_format(self, publisher, call_log):
        records = [_make_record("+1111111111", "poll-1")]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        assert body["data"]["responsesLocation"] == "prefix/path/consolidated/test-campaign_all_cluster_analysis.json"

    @pytest.mark.asyncio
    async def test_responses_location_with_trailing_slash(self, tmp_path, mock_s3, mock_sqs):
        config = _make_config(tmp_path, publish_to_sqs=True, s3_output_path="s3://bucket/prefix/path/")
        pub = SQSEventPublisher(config, s3_client=mock_s3, sqs_client=mock_sqs)
        loc = pub._compute_responses_location("test-campaign")
        assert loc == "prefix/path/consolidated/test-campaign_all_cluster_analysis.json"


class TestS3UploadBeforeSqsSend:
    @pytest.mark.asyncio
    async def test_s3_upload_happens_before_sqs_send(self, publisher, call_log):
        records = [_make_record("+1111111111", "poll-1")]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        ops = [c[0] for c in call_log]
        s3_idx = ops.index("s3_put_object")
        sqs_idx = ops.index("sqs_send_message")
        assert s3_idx < sqs_idx, f"S3 upload (index {s3_idx}) must happen before SQS send (index {sqs_idx})"

    @pytest.mark.asyncio
    async def test_s3_upload_uses_correct_bucket_and_key(self, publisher, call_log):
        records = [_make_record("+1111111111", "poll-1")]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        s3_calls = [c for c in call_log if c[0] == "s3_put_object"]
        assert len(s3_calls) == 1
        assert s3_calls[0][1]["Bucket"] == "test-bucket"
        assert s3_calls[0][1]["Key"] == "prefix/path/consolidated/test-campaign_all_cluster_analysis.json"

    @pytest.mark.asyncio
    async def test_s3_upload_sends_file_content(self, publisher, call_log):
        records = [_make_record("+1111111111", "poll-1")]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        s3_calls = [c for c in call_log if c[0] == "s3_put_object"]
        body = s3_calls[0][1]["Body"]
        parsed = json.loads(body)
        assert isinstance(parsed, list)
        assert len(parsed) > 0

    @pytest.mark.asyncio
    async def test_s3_failure_prevents_sqs_send(self, tmp_path, mock_sqs, call_log):
        failing_s3 = MagicMock()
        failing_s3.put_object.side_effect = Exception("S3 network error")

        json_dir = tmp_path / "output" / "consolidated"
        json_dir.mkdir(parents=True)
        json_file = json_dir / "test-campaign_all_cluster_analysis.json"
        json_file.write_text(json.dumps([{"atomicId": "a1"}]))

        config = _make_config(tmp_path, publish_to_sqs=True)
        pub = SQSEventPublisher(config, s3_client=failing_s3, sqs_client=mock_sqs)

        records = [_make_record("+1111111111", "poll-1")]
        with pytest.raises(RuntimeError, match="S3 upload failed.*S3 network error"):
            await pub.publish_poll_completion(
                poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
            )

        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        assert len(sqs_calls) == 0, "SQS must NOT send if S3 upload fails"


class TestEmptyPollCompletion:
    @pytest.mark.asyncio
    async def test_empty_poll_has_valid_responses_location(self, publisher_no_local_file, call_log):
        await publisher_no_local_file.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=[], campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        loc = body["data"]["responsesLocation"]
        assert loc != "", "responsesLocation must not be empty string"
        assert "consolidated" in loc

    @pytest.mark.asyncio
    async def test_empty_poll_uploads_empty_array_to_s3(self, publisher_no_local_file, call_log):
        await publisher_no_local_file.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=[], campaign_name="test-campaign"
        )
        s3_calls = [c for c in call_log if c[0] == "s3_put_object"]
        assert len(s3_calls) == 1
        body = s3_calls[0][1]["Body"]
        parsed = json.loads(body)
        assert parsed == []

    @pytest.mark.asyncio
    async def test_empty_poll_s3_upload_before_sqs(self, publisher_no_local_file, call_log):
        await publisher_no_local_file.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=[], campaign_name="test-campaign"
        )
        ops = [c[0] for c in call_log]
        s3_idx = ops.index("s3_put_object")
        sqs_idx = ops.index("sqs_send_message")
        assert s3_idx < sqs_idx

    @pytest.mark.asyncio
    async def test_empty_poll_event_has_zero_responses_and_no_issues(self, publisher_no_local_file, call_log):
        await publisher_no_local_file.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=[], campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        assert body["data"]["totalResponses"] == 0
        assert body["data"]["issues"] == []


class TestMultiPollCompleteness:
    @pytest.mark.asyncio
    async def test_poll_with_zero_records_still_gets_event(self, publisher, call_log):
        records = [_make_record("+1111111111", "poll-1")]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1", "poll-2"], unified_records=records, campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        assert len(sqs_calls) == 2, "Both poll-1 and poll-2 must get SQS events"
        poll_ids_sent = [json.loads(c[1]["MessageBody"])["data"]["pollId"] for c in sqs_calls]
        assert "poll-1" in poll_ids_sent
        assert "poll-2" in poll_ids_sent

        poll_2_body = next(
            json.loads(c[1]["MessageBody"]) for c in sqs_calls
            if json.loads(c[1]["MessageBody"])["data"]["pollId"] == "poll-2"
        )
        assert poll_2_body["data"]["totalResponses"] == 0
        assert poll_2_body["data"]["issues"] == []

    @pytest.mark.asyncio
    async def test_single_s3_upload_for_multiple_polls(self, publisher, call_log):
        records = [
            _make_record("+1111111111", "poll-1"),
            _make_record("+2222222222", "poll-2"),
        ]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1", "poll-2"], unified_records=records, campaign_name="test-campaign"
        )
        s3_calls = [c for c in call_log if c[0] == "s3_put_object"]
        assert len(s3_calls) == 1, "Only one S3 put_object regardless of poll count"


class TestRespondentCounting:
    @pytest.mark.asyncio
    async def test_unique_phones_excluding_opt_outs(self, publisher, call_log):
        records = [
            _make_record("+1111111111", "poll-1"),
            _make_record("+1111111111", "poll-1", cluster_id=2, theme="Water"),
            _make_record("+2222222222", "poll-1"),
            _make_record("+3333333333", "poll-1", is_opt_out=True),
        ]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        assert body["data"]["totalResponses"] == 2, "Only 2 unique non-opt-out phones"

    @pytest.mark.asyncio
    async def test_all_opt_out_campaign_sends_zero_responses(self, publisher, call_log):
        records = [
            _make_record("+1111111111", "poll-1", is_opt_out=True),
            _make_record("+2222222222", "poll-1", is_opt_out=True),
        ]
        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        assert body["data"]["totalResponses"] == 0


class TestClusterRanking:
    @pytest.mark.asyncio
    async def test_top_n_by_response_count(self, publisher, call_log):
        records = []
        for i in range(10):
            records.append(_make_record(f"+{i:010d}", "poll-1", cluster_id=1, theme="Roads"))
        for i in range(10, 15):
            records.append(_make_record(f"+{i:010d}", "poll-1", cluster_id=2, theme="Water"))
        for i in range(15, 23):
            records.append(_make_record(f"+{i:010d}", "poll-1", cluster_id=3, theme="Schools"))
        for i in range(23, 25):
            records.append(_make_record(f"+{i:010d}", "poll-1", cluster_id=4, theme="Parks"))

        await publisher.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        issues = body["data"]["issues"]
        assert len(issues) == 3, "top_n=3 means only 3 issues"
        response_counts = [i["responseCount"] for i in issues]
        assert response_counts == sorted(response_counts, reverse=True)

    @pytest.mark.asyncio
    async def test_respects_min_respondents_threshold(self, tmp_path, mock_s3, mock_sqs, call_log):
        json_dir = tmp_path / "output" / "consolidated"
        json_dir.mkdir(parents=True)
        (json_dir / "test-campaign_all_cluster_analysis.json").write_text("[]")

        config = _make_config(tmp_path, publish_to_sqs=True)
        config["min_unique_respondents"] = 5
        pub = SQSEventPublisher(config, s3_client=mock_s3, sqs_client=mock_sqs)

        records = [_make_record(f"+{i:010d}", "poll-1", cluster_id=1, theme="Roads") for i in range(3)]
        await pub.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )
        sqs_calls = [c for c in call_log if c[0] == "sqs_send_message"]
        body = json.loads(sqs_calls[0][1]["MessageBody"])
        assert body["data"]["issues"] == [], "Cluster with 3 respondents < min 5 should be excluded"


class TestLocalEventSaving:
    @pytest.mark.asyncio
    async def test_events_saved_locally_even_when_sqs_disabled(self, tmp_path, mock_s3):
        config = _make_config(tmp_path, publish_to_sqs=False)
        pub = SQSEventPublisher(config, s3_client=mock_s3)

        records = [_make_record("+1111111111", "poll-1")]
        await pub.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=records, campaign_name="test-campaign"
        )

        events_dir = tmp_path / "output" / "events"
        assert events_dir.exists()
        event_files = list(events_dir.glob("events_*.json"))
        assert len(event_files) == 1
        events = json.loads(event_files[0].read_text())
        assert len(events) == 1
        assert events[0]["data"]["pollId"] == "poll-1"


class TestValidation:
    def test_publish_to_sqs_without_queue_url_raises(self, tmp_path):
        config = {
            "publish_to_sqs": True,
            "output_dir": str(tmp_path),
            "s3_output_path": "s3://bucket/prefix",
            "s3_bucket": "test-bucket",
        }
        with pytest.raises(ValueError, match="no queue_url configured"):
            SQSEventPublisher(config)

    @pytest.mark.asyncio
    async def test_corrupted_json_file_uploads_empty_array(self, tmp_path, mock_s3, mock_sqs, call_log):
        json_dir = tmp_path / "output" / "consolidated"
        json_dir.mkdir(parents=True)
        (json_dir / "test-campaign_all_cluster_analysis.json").write_text('{"truncated": ')

        config = _make_config(tmp_path, publish_to_sqs=True)
        pub = SQSEventPublisher(config, s3_client=mock_s3, sqs_client=mock_sqs)

        await pub.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=[], campaign_name="test-campaign"
        )
        s3_calls = [c for c in call_log if c[0] == "s3_put_object"]
        body = s3_calls[0][1]["Body"]
        assert json.loads(body) == [], "Corrupted JSON should fall back to empty array"

    def test_leading_slash_stripped_from_s3_key(self, tmp_path):
        config = _make_config(tmp_path, publish_to_sqs=False, s3_output_path="s3://mybucket")
        pub = SQSEventPublisher(config)
        loc = pub._compute_responses_location("test-campaign")
        assert not loc.startswith("/"), f"S3 key must not start with /: {loc}"
        assert loc == "consolidated/test-campaign_all_cluster_analysis.json"

    @pytest.mark.asyncio
    async def test_finds_json_when_output_dir_is_consolidated(self, tmp_path, mock_s3, mock_sqs, call_log):
        consolidated_dir = tmp_path / "output" / "consolidated"
        consolidated_dir.mkdir(parents=True)
        (consolidated_dir / "test-campaign_all_cluster_analysis.json").write_text(
            json.dumps([{"atomicId": "a1", "phoneNumber": "+1111"}])
        )

        config = _make_config(tmp_path, publish_to_sqs=True)
        config["output_dir"] = str(consolidated_dir)
        pub = SQSEventPublisher(config, s3_client=mock_s3, sqs_client=mock_sqs)

        await pub.publish_poll_completion(
            poll_ids=["poll-1"], unified_records=[], campaign_name="test-campaign"
        )
        s3_calls = [c for c in call_log if c[0] == "s3_put_object"]
        body = json.loads(s3_calls[0][1]["Body"])
        assert len(body) == 1, "Should upload real JSON, not empty array fallback"
        assert body[0]["atomicId"] == "a1"
