"""Schema contract test for the SQS callback that gp-api consumes.

Mirrors the Zod schema at gp-api/src/queue/queue.types.ts (the
`AgentExperimentResultSchema`). Every CallbackSender.send_result body MUST
parse cleanly through this Pydantic model — if it doesn't, gp-api's queue
consumer will throw and the message will dead-letter, leaving the run in
PENDING/RUNNING forever.

When gp-api's schema changes, this Pydantic mirror must be updated in
lock-step. Keeping the duplicate is the cost; the alternative is silent prod
breakage from a unit-test green build.
"""

import json
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from typing import Literal

from broker.callback_sender import CallbackSender


class GpApiAgentExperimentResultData(BaseModel):
    """Mirror of gp-api's AgentExperimentResultSchema (zod). Required vs
    optional, status enum, and field naming MUST match exactly. Last synced
    from gp-api/src/queue/queue.types.ts on 2026-04-21.
    """

    experimentId: str
    runId: str
    organizationSlug: str
    status: Literal["running", "success", "failed", "contract_violation", "stale"]
    artifactKey: str | None = None
    artifactBucket: str | None = None
    durationSeconds: float | None = None
    costUsd: float | None = None
    reasonCode: str | None = None
    detail: str | None = None
    error: str | None = None

    model_config = ConfigDict(extra="allow")


class GpApiAgentExperimentResultMessage(BaseModel):
    type: Literal["agentExperimentResult"]
    data: GpApiAgentExperimentResultData


def _send_and_parse(**kwargs) -> GpApiAgentExperimentResultMessage:
    sqs = MagicMock()
    sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/q.fifo")
    sender.send_result(**kwargs)
    body = json.loads(sqs.send_message.call_args[1]["MessageBody"])
    return GpApiAgentExperimentResultMessage.model_validate(body)


class TestSuccessCallbackParsesAtGpApi:
    def test_minimal_success(self):
        msg = _send_and_parse(
            run_id="run-1",
            organization_slug="org-1",
            experiment_id="voter_targeting",
            status="success",
        )
        assert msg.type == "agentExperimentResult"
        assert msg.data.status == "success"
        assert msg.data.runId == "run-1"

    def test_success_with_artifact_metadata(self):
        msg = _send_and_parse(
            run_id="run-2",
            organization_slug="org-2",
            experiment_id="district_intel",
            status="success",
            artifact_key="district_intel/run-2/artifact.json",
            artifact_bucket="gp-agent-artifacts-dev",
            duration_seconds=183.4,
            cost_usd=0.42,
        )
        assert msg.data.artifactKey == "district_intel/run-2/artifact.json"
        assert msg.data.artifactBucket == "gp-agent-artifacts-dev"
        assert msg.data.durationSeconds == 183.4
        assert msg.data.costUsd == 0.42


class TestFailureCallbackParsesAtGpApi:
    def test_failed_with_reason_and_detail(self):
        msg = _send_and_parse(
            run_id="run-3",
            organization_slug="org-3",
            experiment_id="walking_plan",
            status="failed",
            reason_code="Timeout",
            detail="Agent exceeded 600s limit",
        )
        assert msg.data.status == "failed"
        assert msg.data.reasonCode == "Timeout"
        assert msg.data.detail == "Agent exceeded 600s limit"
        # error field carries detail for back-compat — must parse.
        assert msg.data.error == "Agent exceeded 600s limit"

    def test_contract_violation_parses(self):
        msg = _send_and_parse(
            run_id="run-4",
            organization_slug="org-4",
            experiment_id="voter_targeting",
            status="contract_violation",
            reason_code="ContractViolation",
            detail="Missing required field: voters[0].address",
        )
        assert msg.data.status == "contract_violation"
        assert msg.data.error == "Missing required field: voters[0].address"


class TestRunningCallbackParsesAtGpApi:
    def test_running_status(self):
        msg = _send_and_parse(
            run_id="run-5",
            organization_slug="org-5",
            experiment_id="meeting_briefing",
            status="running",
        )
        assert msg.data.status == "running"


class TestSchemaRejectsInvalid:
    """Locks in what gp-api WOULD reject, so a code change that emits these
    shapes fails fast in CI rather than silently dead-lettering in prod.
    """

    def test_unknown_status_rejected(self):
        sqs = MagicMock()
        sender = CallbackSender(sqs_client=sqs, queue_url="https://sqs.example.com/q.fifo")
        # Broker's TERMINAL_STATUSES includes "timeout" but gp-api's enum does
        # NOT. If we ever start sending "timeout" directly, gp-api throws and
        # the message dead-letters. This test pins the constraint.
        sender.send_result(
            run_id="run-6",
            organization_slug="org-6",
            experiment_id="voter_targeting",
            status="timeout",
        )
        body = json.loads(sqs.send_message.call_args[1]["MessageBody"])
        with pytest.raises(ValidationError):
            GpApiAgentExperimentResultMessage.model_validate(body)

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            GpApiAgentExperimentResultMessage.model_validate({
                "type": "agentExperimentResult",
                "data": {
                    "runId": "x", "organizationSlug": "y", "status": "success",
                },  # missing experimentId
            })
