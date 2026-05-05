"""Contract test: dispatch Lambda error callbacks must match the same wire
format the broker's CallbackSender emits, which gp-api's Zod consumer accepts.

Before Fix P1: dispatch's send_error_callback wrote flat snake_case. gp-api
rejected it at zod boundary → DLQ → run stuck PENDING. Every dispatch-layer
failure (unknown experiment, missing param, broker 4xx, ECS RunTask failure)
was invisible to the webapp.
"""

import json
from unittest.mock import MagicMock, patch

import pmf_engine.control_plane.dispatch_handler as dh
from broker.tests.test_callback_wire_contract import (
    GpApiAgentExperimentResultMessage,
)


def _captured_body(sqs_mock) -> dict:
    sqs_mock.send_message.assert_called_once()
    return json.loads(sqs_mock.send_message.call_args[1]["MessageBody"])


class TestSendErrorCallbackWireFormat:
    """The body MUST be `{type:"agentExperimentResult", data:{experimentId,
    runId, organizationSlug, status, error, ...}}` — camelCase, enveloped, matching
    what gp-api's AgentExperimentResultSchema parses.
    """

    def test_error_callback_wraps_in_envelope(self):
        sqs = MagicMock()
        with patch.object(dh, "get_sqs_client", return_value=sqs):
            dh.send_error_callback(
                message={
                    "experiment_type": "voter_targeting",
                    "run_id": "run-err-1",
                    "organization_slug": "org-1",
                },
                error="Missing required params: state",
                callback_queue_url="https://sqs.example.com/q.fifo",
                dedup_id="run-err-1-terminal",
            )
        body = _captured_body(sqs)
        # Envelope
        assert body["type"] == "agentExperimentResult"
        # Data shape: camelCase, gp-api-compatible
        data = body["data"]
        assert data["experimentId"] == "voter_targeting"
        assert data["runId"] == "run-err-1"
        assert data["organizationSlug"] == "org-1"
        assert data["status"] == "failed"
        assert data["error"] == "Missing required params: state"

    def test_error_callback_parses_through_gp_api_schema(self):
        """Direct validation against the Pydantic mirror of gp-api's Zod
        schema — if this parses cleanly, gp-api's consumer accepts the body.
        """
        sqs = MagicMock()
        with patch.object(dh, "get_sqs_client", return_value=sqs):
            dh.send_error_callback(
                message={
                    "experiment_type": "district_intel",
                    "run_id": "run-err-2",
                    "organization_slug": "org-2",
                },
                error="Broker rejected: scope predicate override not allowed",
                callback_queue_url="https://sqs.example.com/q.fifo",
                dedup_id="run-err-2-terminal",
            )
        body = _captured_body(sqs)
        # Should not raise.
        msg = GpApiAgentExperimentResultMessage.model_validate(body)
        assert msg.data.status == "failed"
        assert "scope predicate" in (msg.data.error or "")


class TestSendErrorCallbackDedupKey:
    """Dispatch errors and broker callbacks both target the gp-api results
    FIFO. If they use different MessageDeduplicationId conventions, a race
    where dispatch sends `failed` after ECS RunTask timeout AND the runner
    sends `success` both land on gp-api as DIFFERENT dedup IDs → no dedup,
    two deliveries. Align on `{run_id}-{status}` so both sides are eligible
    for SQS FIFO deduplication.
    """

    def test_dedup_id_uses_run_id_status_pattern(self):
        sqs = MagicMock()
        with patch.object(dh, "get_sqs_client", return_value=sqs):
            dh.send_error_callback(
                message={
                    "experiment_type": "voter_targeting",
                    "run_id": "run-xyz",
                    "organization_slug": "org-1",
                },
                error="whatever",
                callback_queue_url="https://sqs.example.com/q.fifo",
                # NOTE: no dedup_id passed — handler should derive it.
            )
        call_kwargs = sqs.send_message.call_args[1]
        # Same convention the broker's CallbackSender uses: {run_id}-{status}.
        assert call_kwargs["MessageDeduplicationId"] == "run-xyz-failed"
        # Group should be stable-per-run (or shared with the broker pattern)
        # so FIFO per-run ordering works consistently.
        assert call_kwargs["MessageGroupId"] == "agentExperiments"
