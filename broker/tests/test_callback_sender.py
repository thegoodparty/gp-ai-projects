import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from broker.callback_sender import CallbackSender


class TestCallbackSenderMessageBody:
    def test_send_result_constructs_correct_body(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-001",
            organization_slug="org-42",
            experiment_id="voter_targeting",
            status="success",
            artifact_key="voter_targeting/org-42/latest.json",
            artifact_bucket="gp-agent-artifacts-dev",
            duration_seconds=120.5,
            cost_usd=0.03,
        )

        sqs.send_message.assert_called_once()
        call_kwargs = sqs.send_message.call_args[1]
        body = json.loads(call_kwargs["MessageBody"])

        assert body["type"] == "agentExperimentResult"
        assert body["data"]["experimentId"] == "voter_targeting"
        assert body["data"]["runId"] == "run-001"
        assert body["data"]["organizationSlug"] == "org-42"
        assert body["data"]["status"] == "success"
        assert body["data"]["artifactKey"] == "voter_targeting/org-42/latest.json"
        assert body["data"]["artifactBucket"] == "gp-agent-artifacts-dev"
        assert body["data"]["durationSeconds"] == 120.5
        assert body["data"]["costUsd"] == 0.03
        assert body["data"]["reasonCode"] == ""
        assert body["data"]["detail"] == ""


class TestCallbackSenderDedupId:
    def test_dedup_id_format(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-001",
            organization_slug="org-42",
            experiment_id="voter_targeting",
            status="failed",
        )

        call_kwargs = sqs.send_message.call_args[1]
        assert call_kwargs["MessageDeduplicationId"] == "run-001-failed"
        assert call_kwargs["MessageGroupId"] == "run-001"
        assert call_kwargs["QueueUrl"] == "https://sqs.example.com/queue.fifo"


class TestCallbackSenderErrorPropagation:
    def test_sqs_error_propagates(self):
        sqs = MagicMock()
        sqs.send_message.side_effect = Exception("SQS connection refused")
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        with pytest.raises(Exception, match="SQS connection refused"):
            sender.send_result(
                run_id="run-001",
                organization_slug="org-42",
                experiment_id="voter_targeting",
                status="success",
            )


class TestCallbackSenderFailureFields:
    def test_send_result_with_failure_fields(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-002",
            organization_slug="org-99",
            experiment_id="district_intel",
            status="failed",
            reason_code="timeout",
            detail="Agent exceeded 4h limit",
        )

        call_kwargs = sqs.send_message.call_args[1]
        body = json.loads(call_kwargs["MessageBody"])
        assert body["data"]["status"] == "failed"
        assert body["data"]["reasonCode"] == "timeout"
        assert body["data"]["detail"] == "Agent exceeded 4h limit"


class TestCallbackSenderFailureCarriesDurationAndCost:
    """gp-api's ExperimentRun.durationSeconds and .costUsd were always 0 for
    failed runs because the runner didn't forward the numbers and the broker
    defaulted them to 0. Lock in that when the broker passes real values to
    send_result, they land on the SQS envelope as camelCase for gp-api's zod
    schema.
    """

    def test_failed_callback_includes_duration_and_cost(self):
        sqs = MagicMock()
        sender = CallbackSender(
            sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo"
        )

        sender.send_result(
            run_id="run-fail-dc",
            organization_slug="org-7",
            experiment_id="voter_targeting",
            status="failed",
            reason_code="Timeout",
            detail="Agent exceeded limit",
            duration_seconds=42.5,
            cost_usd=0.37,
        )

        body = json.loads(sqs.send_message.call_args[1]["MessageBody"])
        assert body["data"]["status"] == "failed"
        assert body["data"]["durationSeconds"] == 42.5
        assert body["data"]["costUsd"] == 0.37


class TestCallbackSenderErrorFieldBackCompat:
    """gp-api's queue consumer still reads `data.error` to populate the
    ExperimentRun.error column (the only user-visible failure message in the
    webapp). The runner stopped sending `error` when it switched to
    reason_code/detail — every failure callback now loses its error text in
    the UI. This test locks in that the callback body always carries `error`
    for back-compat until gp-api is updated to read `detail`.
    """

    def test_failed_callback_includes_error_field_for_backcompat(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-003",
            organization_slug="org-7",
            experiment_id="voter_targeting",
            status="failed",
            reason_code="Timeout",
            detail="Agent exceeded 600s limit",
        )

        body = json.loads(sqs.send_message.call_args[1]["MessageBody"])
        # gp-api reads data.error; keep populated with the same text as detail.
        assert body["data"]["error"] == "Agent exceeded 600s limit"
        # New structured fields still present for when gp-api upgrades.
        assert body["data"]["reasonCode"] == "Timeout"
        assert body["data"]["detail"] == "Agent exceeded 600s limit"

    def test_success_callback_has_empty_error_field(self):
        """Success runs carry an empty error — gp-api's schema treats missing
        as undefined, which throws under strict zod parsing. Always present."""
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-004",
            organization_slug="org-7",
            experiment_id="voter_targeting",
            status="success",
        )

        body = json.loads(sqs.send_message.call_args[1]["MessageBody"])
        assert body["data"]["error"] == ""

    def test_contract_violation_callback_includes_error_field(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-005",
            organization_slug="org-7",
            experiment_id="voter_targeting",
            status="contract_violation",
            reason_code="ContractViolation",
            detail="Missing required field: voters[0].address",
        )

        body = json.loads(sqs.send_message.call_args[1]["MessageBody"])
        assert body["data"]["error"] == "Missing required field: voters[0].address"


class TestCallbackSenderMessageGroupId:
    """FIFO queues serialize by MessageGroupId. Using a single static group
    ("agentExperiments") means one poison-pill message blocks every other
    callback. Per-run_id groups keep ordering within a run (running ->
    success/failed) but isolate runs from each other."""

    def test_message_group_id_is_run_id(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-abc-123",
            organization_slug="org-42",
            experiment_id="voter_targeting",
            status="success",
        )

        call_kwargs = sqs.send_message.call_args[1]
        assert call_kwargs["MessageGroupId"] == "run-abc-123"
        assert call_kwargs["MessageGroupId"] != "agentExperiments"

    def test_two_different_runs_use_different_group_ids(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/queue.fifo")

        sender.send_result(
            run_id="run-alpha",
            organization_slug="org-1",
            experiment_id="voter_targeting",
            status="success",
        )
        sender.send_result(
            run_id="run-beta",
            organization_slug="org-2",
            experiment_id="voter_targeting",
            status="success",
        )

        assert sqs.send_message.call_count == 2
        first_group = sqs.send_message.call_args_list[0][1]["MessageGroupId"]
        second_group = sqs.send_message.call_args_list[1][1]["MessageGroupId"]
        assert first_group == "run-alpha"
        assert second_group == "run-beta"
        assert first_group != second_group


class TestCallbackSenderSqsFailureLogging:
    def test_sqs_send_failure_logs_and_reraises(self, caplog):
        queue_url = "https://sqs.example.com/queue.fifo"
        sqs = MagicMock()
        sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
            "SendMessage",
        )
        sender = CallbackSender(sqs_client=sqs, queue_url=queue_url)

        with caplog.at_level(logging.ERROR, logger="broker.callback_sender"):
            with pytest.raises(ClientError):
                sender.send_result(
                    run_id="run-abc",
                    organization_slug="org-42",
                    experiment_id="voter_targeting",
                    status="failed",
                    reason_code="AgentError",
                    detail="agent crashed",
                )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) >= 1, "expected an ERROR-level log record for SQS failure"
        record = error_records[0]
        message = record.getMessage()
        assert "run-abc" in message
        assert "failed" in message
        assert queue_url in message or "SendMessage" in message
        assert record.exc_info is not None
