import json
import time
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.callback_sender import CallbackSender
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore
from broker.endpoints.run_status import (
    get_artifact_bucket,
    get_broker_token_raw,
    get_callback_sender,
    get_s3_client,
    get_scope_ticket,
    get_ticket_store,
    router,
)

BROKER_TOKEN = "broker-token-test-abc123"


def _make_ticket(
    experiment_id: str = "voter_targeting",
    organization_slug: str = "org-42",
    run_id: str = "run-001",
) -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id=run_id,
        organization_slug=organization_slug,
        experiment_id=experiment_id,
        scope={},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _create_app(
    ticket: ScopeTicket | None = None,
) -> tuple[FastAPI, MagicMock, MagicMock, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    _ticket = ticket or _make_ticket()
    app.dependency_overrides[get_scope_ticket] = lambda: _ticket

    mock_s3 = MagicMock()
    app.dependency_overrides[get_s3_client] = lambda: mock_s3

    mock_sender = MagicMock(spec=CallbackSender)
    app.dependency_overrides[get_callback_sender] = lambda: mock_sender

    mock_store = MagicMock(spec=ScopeTicketStore)
    app.dependency_overrides[get_ticket_store] = lambda: mock_store

    app.dependency_overrides[get_broker_token_raw] = lambda: BROKER_TOKEN
    app.dependency_overrides[get_artifact_bucket] = lambda: "gp-agent-artifacts-dev"

    from broker.endpoints.run_status import get_data_query_tracker
    app.dependency_overrides[get_data_query_tracker] = lambda: MagicMock()

    return app, mock_s3, mock_sender, mock_store


class TestRunStatusRunningRejected:
    def test_running_rejected_at_pydantic_boundary(self):
        """gp-api's new contract drops `running` from the result status enum
        — the agent no longer reports it, and the broker rejects it at the
        Pydantic boundary before any callback or ticket mutation can occur.
        """
        app, _, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={"status": "running"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422
        mock_sender.send_result.assert_not_called()
        mock_store.delete_ticket_and_run_lock.assert_not_called()


class TestRunStatusFailed:
    def test_failed_sends_callback_and_deletes_ticket(self):
        app, _, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={
                "status": "failed",
                "reason_code": "timeout",
                "detail": "Exceeded time limit",
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.json()["callback_sent"] is True

        mock_sender.send_result.assert_called_once()
        call_kwargs = mock_sender.send_result.call_args[1]
        assert call_kwargs["status"] == "failed"
        assert call_kwargs["reason_code"] == "timeout"
        assert call_kwargs["detail"] == "Exceeded time limit"

        mock_store.delete_ticket_and_run_lock.assert_called_once_with(BROKER_TOKEN, "run-001")


class TestRunStatusContractViolation:
    def test_accepts_and_forwards_duration_and_cost(self):
        """Runner reports real elapsed seconds and accrued cost on failure.
        Broker must accept snake_case and forward to callback_sender, which
        will emit camelCase on the SQS callback to gp-api.
        """
        app, _, mock_sender, _ = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={
                "status": "failed",
                "reason_code": "agent_error",
                "detail": "boom",
                "duration_seconds": 42.5,
                "cost_usd": 0.37,
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        mock_sender.send_result.assert_called_once()
        call_kwargs = mock_sender.send_result.call_args[1]
        assert call_kwargs["duration_seconds"] == 42.5
        assert call_kwargs["cost_usd"] == 0.37

    def test_quarantine_second_contract_violation_does_not_overwrite(self):
        """Quarantine put_object must use IfNoneMatch="*" so a retry cannot
        overwrite the first forensic record. When S3 returns
        PreconditionFailed, the endpoint must log-and-continue (200), not 500.
        """
        app, mock_s3, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        rejected_first = {"first_record": True}
        resp1 = client.post(
            "/internal/run-status",
            json={
                "status": "contract_violation",
                "reason_code": "schema_mismatch",
                "detail": "first attempt",
                "rejected_artifact": rejected_first,
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp1.status_code == 200
        first_call_kwargs = mock_s3.put_object.call_args[1]
        assert first_call_kwargs.get("IfNoneMatch") == "*", (
            "quarantine put_object must pass IfNoneMatch='*' to guarantee "
            "write-once semantics"
        )

        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed", "Message": "already exists"}},
            "PutObject",
        )

        rejected_second = {"second_record": True, "different": "data"}
        resp2 = client.post(
            "/internal/run-status",
            json={
                "status": "contract_violation",
                "reason_code": "schema_mismatch",
                "detail": "second attempt",
                "rejected_artifact": rejected_second,
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp2.status_code == 200, (
            "second contract_violation for same run_id must not 500 — "
            "quarantine already-exists is a soft failure (log + continue)"
        )
        assert resp2.json()["callback_sent"] is True

    def test_contract_violation_with_rejected_artifact_writes_to_s3_quarantine(self):
        app, mock_s3, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        rejected = {"bad_field": "wrong type"}
        resp = client.post(
            "/internal/run-status",
            json={
                "status": "contract_violation",
                "reason_code": "schema_mismatch",
                "detail": "Missing required field: summary",
                "rejected_artifact": rejected,
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.json()["callback_sent"] is True

        mock_s3.put_object.assert_called_once()
        s3_call_kwargs = mock_s3.put_object.call_args[1]
        assert "rejected/" in s3_call_kwargs["Key"]
        assert "run-001" in s3_call_kwargs["Key"]

        stored_body = json.loads(s3_call_kwargs["Body"])
        assert stored_body == rejected

        mock_store.delete_ticket_and_run_lock.assert_called_once_with(BROKER_TOKEN, "run-001")


class TestRunStatusSuccessRejected:
    """`status=success` via /run-status is NOT allowed — success must only
    flow through /artifact/publish, which is the only path that uploads to
    S3 and emits the success callback. Without this restriction, an agent
    could POST `{"status":"success"}` and fire a SUCCESS callback to gp-api
    with no artifact actually persisted.
    """

    def test_success_rejected_at_pydantic_boundary(self):
        """Literal enum on RunStatusRequest.status excludes `success`, so
        FastAPI returns 422 before the handler runs — neither callback nor
        ticket delete execute.
        """
        app, _, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={
                "status": "success",
                "duration_seconds": 300.5,
                "cost_usd": 0.12,
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422
        mock_sender.send_result.assert_not_called()
        mock_store.delete_ticket_and_run_lock.assert_not_called()


class TestRunStatusTimeoutTranslated:
    """gp-api's Zod schema accepts `success/failed/contract_violation` — NOT
    `timeout`. A literal `timeout` callback hits gp-api's DLQ and the run
    stays stuck RUNNING forever. Broker must translate `timeout` → `failed`
    so the callback parses.
    """

    def test_timeout_translates_to_failed_with_reason_code(self):
        app, _, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={
                "status": "timeout",
                "detail": "Experiment exceeded 3000s limit",
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        mock_sender.send_result.assert_called_once()
        call_kwargs = mock_sender.send_result.call_args[1]
        # Translated at the broker boundary
        assert call_kwargs["status"] == "failed"
        assert call_kwargs["reason_code"] == "timeout"
        assert call_kwargs["detail"] == "Experiment exceeded 3000s limit"
        # Still terminal — ticket deleted
        mock_store.delete_ticket_and_run_lock.assert_called_once_with(BROKER_TOKEN, "run-001")


class TestRunStatusRejectsUnknownStatus:
    """Enforce a Literal at the Pydantic boundary. Anything outside the
    allowed enum should 422 at validation, not silently forward to gp-api.
    """

    def test_unknown_status_returns_422(self):
        app, _, mock_sender, mock_store = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={"status": "stale"},  # stale is a gp-api-only state, not reported by agent
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422
        mock_sender.send_result.assert_not_called()
        mock_store.delete_ticket_and_run_lock.assert_not_called()


class TestRunStatusClearsRunLock:
    """Terminal run-status must delete BOTH the ticket row and the run-lock
    row so a legitimate re-dispatch of the same run_id isn't blocked by a
    stale lock until TTL. Verified end-to-end against moto DynamoDB — mock
    assertions in the sibling tests prove the method was called, this
    proves the rows are actually gone."""

    def test_failed_status_clears_both_ticket_and_run_lock_in_ddb(self):
        import boto3
        from moto import mock_aws

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName="scope-tickets-term",
                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST",
            )
            real_store = ScopeTicketStore("scope-tickets-term", dynamodb_client=ddb)
            ticket = _make_ticket(run_id="run-term-001")
            real_store.put_ticket(ticket)

            assert "Item" in ddb.get_item(
                TableName="scope-tickets-term", Key={"pk": {"S": ticket.pk}}
            )
            assert "Item" in ddb.get_item(
                TableName="scope-tickets-term", Key={"pk": {"S": f"run:{ticket.run_id}"}}
            )

            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_scope_ticket] = lambda: ticket
            app.dependency_overrides[get_s3_client] = lambda: MagicMock()
            app.dependency_overrides[get_callback_sender] = lambda: MagicMock(spec=CallbackSender)
            app.dependency_overrides[get_ticket_store] = lambda: real_store
            app.dependency_overrides[get_broker_token_raw] = lambda: BROKER_TOKEN
            app.dependency_overrides[get_artifact_bucket] = lambda: "bucket"
            from broker.endpoints.run_status import get_data_query_tracker
            app.dependency_overrides[get_data_query_tracker] = lambda: MagicMock()

            client = TestClient(app)
            resp = client.post(
                "/internal/run-status",
                json={"status": "failed", "reason_code": "TestFail"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            assert resp.status_code == 200

            assert "Item" not in ddb.get_item(
                TableName="scope-tickets-term", Key={"pk": {"S": ticket.pk}}
            ), "ticket row must be gone after terminal status"
            assert "Item" not in ddb.get_item(
                TableName="scope-tickets-term", Key={"pk": {"S": f"run:{ticket.run_id}"}}
            ), (
                "run-lock row must also be gone — otherwise re-dispatching the "
                "same run_id hits 409 against the stale lock until TTL expires"
            )


class TestRunStatusTerminalFailureAlerting:
    """Every terminal /run-status (failed | contract_violation | timeout) must
    emit a structured log line AND a CloudWatch metric so ops gets a Slack
    alert. Without this, failures land silently in gp-api's experiment_run
    table and only surface when a user complains. The metric is alarmed in
    infrastructure/modules/broker/main.tf -> SNS -> shared-slack-notifier.
    """

    def _post(self, status: str, reason_code: str | None = None, ticket=None):
        from unittest.mock import patch

        from broker.endpoints.run_status import _reset_cw_client_for_tests

        _reset_cw_client_for_tests()
        app, _, _, _ = _create_app(ticket=ticket)
        body: dict = {"status": status, "duration_seconds": 12.5}
        if reason_code:
            body["reason_code"] = reason_code
        with patch("broker.endpoints.run_status._get_cw_client") as mock_cw:
            client = TestClient(app)
            resp = client.post(
                "/internal/run-status",
                json=body,
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
            return resp, mock_cw

    def test_failed_emits_structured_log_with_run_id_and_experiment(self, caplog):
        caplog.set_level("WARNING", logger="broker.endpoints.run_status")
        ticket = _make_ticket(
            experiment_id="meeting_briefing", run_id="run-failed-001",
            organization_slug="demo-org",
        )
        resp, _ = self._post("failed", reason_code="agent_crashed", ticket=ticket)
        assert resp.status_code == 200
        records = [r for r in caplog.records if "run_status terminal" in r.message]
        assert records, "expected a 'run_status terminal' warning log"
        msg = records[0].getMessage()
        assert "status=failed" in msg
        assert "reason_code=agent_crashed" in msg
        assert "run_id=run-failed-001" in msg
        assert "experiment_id=meeting_briefing" in msg

    def test_contract_violation_emits_structured_log(self, caplog):
        caplog.set_level("WARNING", logger="broker.endpoints.run_status")
        ticket = _make_ticket(experiment_id="meeting_briefing", run_id="run-cv-001")
        resp, _ = self._post("contract_violation", ticket=ticket)
        assert resp.status_code == 200
        assert any(
            "run_status terminal" in r.message and "status=contract_violation" in r.message
            for r in caplog.records
        )

    def test_timeout_emits_structured_log_with_status_timeout(self, caplog):
        """The wire callback translates timeout -> failed, but the log must
        record the original 'timeout' so ops can distinguish."""
        caplog.set_level("WARNING", logger="broker.endpoints.run_status")
        ticket = _make_ticket(run_id="run-timeout-001")
        resp, _ = self._post("timeout", ticket=ticket)
        assert resp.status_code == 200
        assert any(
            "run_status terminal" in r.message and "status=timeout" in r.message
            for r in caplog.records
        )

    def test_failed_emits_cloudwatch_metric_with_dimensions(self):
        ticket = _make_ticket(experiment_id="meeting_briefing", run_id="run-metric-001")
        resp, mock_cw = self._post("failed", ticket=ticket)
        assert resp.status_code == 200
        mock_cw.return_value.put_metric_data.assert_called_once()
        call = mock_cw.return_value.put_metric_data.call_args.kwargs
        assert call["Namespace"] == "PMFEngine"
        data = call["MetricData"][0]
        assert data["MetricName"] == "ExperimentTerminalFailure"
        assert data["Value"] == 1
        dims = {d["Name"]: d["Value"] for d in data["Dimensions"]}
        assert dims["ExperimentId"] == "meeting_briefing"
        assert dims["Status"] == "failed"
        assert "Environment" in dims

    def test_contract_violation_metric_status_value(self):
        ticket = _make_ticket(experiment_id="district_intel", run_id="run-cv-002")
        resp, mock_cw = self._post("contract_violation", ticket=ticket)
        assert resp.status_code == 200
        data = mock_cw.return_value.put_metric_data.call_args.kwargs["MetricData"][0]
        dims = {d["Name"]: d["Value"] for d in data["Dimensions"]}
        assert dims["Status"] == "contract_violation"
        assert dims["ExperimentId"] == "district_intel"

    def test_metric_emission_failure_does_not_break_response(self):
        """CloudWatch hiccup must never fail the broker response."""
        from unittest.mock import patch

        from broker.endpoints.run_status import _reset_cw_client_for_tests
        _reset_cw_client_for_tests()
        app, _, mock_sender, _ = _create_app()
        with patch(
            "broker.endpoints.run_status._get_cw_client",
            side_effect=RuntimeError("cw outage"),
        ):
            client = TestClient(app)
            resp = client.post(
                "/internal/run-status",
                json={"status": "failed", "reason_code": "test"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
        assert resp.status_code == 200
        mock_sender.send_result.assert_called_once()


class TestRunStatusQaEvalTranscriptField:
    def test_run_status_accepts_qa_eval_transcript_field_without_durable_write(self):
        """run-status is the FAILURE-only path: it declares qa_eval_transcript
        for forward-compat (so pydantic's extra='ignore' doesn't trip a future
        sender) but performs NO durable QA write — there is no verdict to couple
        a transcript to here. Pin that posting the field succeeds (200,
        callback_sent=True) and that NO eval_transcript.jsonl is written."""
        app, mock_s3, mock_sender, _ = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/internal/run-status",
            json={
                "status": "failed",
                "reason_code": "agent_error",
                "qa_eval_transcript": '{"turn":1,"kind":"assistant"}\n{"turn":0,"kind":"result"}',
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["callback_sent"] is True
        transcript_puts = [
            c for c in mock_s3.put_object.call_args_list
            if c.kwargs.get("Key", "").endswith("eval_transcript.jsonl")
        ]
        assert not transcript_puts, (
            "run-status must not perform a durable transcript write — it is a "
            "failure-only path with no verdict to couple to"
        )
        mock_sender.send_result.assert_called_once()
