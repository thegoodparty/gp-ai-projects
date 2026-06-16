import time
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import InputFileRef, ScopeTicket
from broker.endpoints.inputs_read import (
    MAX_INPUT_BYTES,
    get_s3_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-test-abc123"
INPUT_BUCKET = "gp-agent-run-inputs-dev"
INPUT_KEY = "uploads/org-42/run-001/agenda.pdf"


def _make_ticket(input_files: list[InputFileRef] | None = None) -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-001",
        organization_slug="org-42",
        experiment_id="meeting_briefing",
        scope={},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
        input_files=input_files,
    )


def _make_s3_response(body_bytes: bytes, content_length: int | None = None) -> dict:
    body = MagicMock()
    body.read.return_value = body_bytes
    resp = {"Body": body}
    if content_length is not None:
        resp["ContentLength"] = content_length
    return resp


def _create_app(
    ticket: ScopeTicket | None = None,
    s3_response: dict | None = None,
    s3_error: Exception | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    _ticket = ticket or _make_ticket(
        input_files=[
            InputFileRef(bucket=INPUT_BUCKET, key=INPUT_KEY, dest="agenda.pdf")
        ]
    )
    app.dependency_overrides[get_scope_ticket] = lambda: _ticket

    mock_s3 = MagicMock()
    if s3_error:
        mock_s3.get_object.side_effect = s3_error
    elif s3_response:
        mock_s3.get_object.return_value = s3_response
    else:
        mock_s3.get_object.return_value = _make_s3_response(
            b"%PDF-1.4 dummy content", content_length=22
        )
    app.dependency_overrides[get_s3_client] = lambda: mock_s3

    return app


class TestInputsReadSuccess:
    def test_returns_bytes_for_authorized_ref(self):
        body_bytes = b"%PDF-1.4 fake agenda body"
        app = _create_app(
            s3_response=_make_s3_response(body_bytes, content_length=len(body_bytes))
        )
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.content == body_bytes
        assert resp.headers["content-type"] == "application/octet-stream"

    def test_get_object_called_with_request_bucket_and_key(self):
        app = _create_app()
        from broker.endpoints import inputs_read as mod

        mock_s3 = app.dependency_overrides[mod.get_s3_client]()
        client = TestClient(app)

        client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        mock_s3.get_object.assert_called_once_with(Bucket=INPUT_BUCKET, Key=INPUT_KEY)


class TestInputsReadAuth:
    def test_ticket_without_input_files_returns_403(self):
        ticket = _make_ticket(input_files=None)
        app = _create_app(ticket=ticket)
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 403
        assert "no input files authorized" in resp.json()["detail"].lower()

    def test_ticket_with_empty_input_files_returns_403(self):
        ticket = _make_ticket(input_files=[])
        app = _create_app(ticket=ticket)
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 403

    def test_wrong_bucket_returns_403(self):
        ticket = _make_ticket(
            input_files=[
                InputFileRef(bucket=INPUT_BUCKET, key=INPUT_KEY, dest="agenda.pdf")
            ]
        )
        app = _create_app(ticket=ticket)
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": "some-other-bucket", "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 403

    def test_wrong_key_returns_403(self):
        ticket = _make_ticket(
            input_files=[
                InputFileRef(bucket=INPUT_BUCKET, key=INPUT_KEY, dest="agenda.pdf")
            ]
        )
        app = _create_app(ticket=ticket)
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={
                "bucket": INPUT_BUCKET,
                "key": "uploads/other-org/other-run/agenda.pdf",
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 403

    def test_does_not_call_s3_when_unauthorized(self):
        ticket = _make_ticket(input_files=None)
        app = _create_app(ticket=ticket)
        from broker.endpoints import inputs_read as mod

        mock_s3 = app.dependency_overrides[mod.get_s3_client]()
        client = TestClient(app)

        client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        mock_s3.get_object.assert_not_called()


class TestInputsReadS3Errors:
    def test_s3_not_found_returns_404(self):
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        app = _create_app(s3_error=error)
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 404

    def test_other_s3_error_returns_500(self):
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetObject",
        )
        app = _create_app(s3_error=error)
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 500

    def test_non_404_s3_error_is_logged(self, caplog):
        import logging

        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetObject",
        )
        app = _create_app(s3_error=error)
        client = TestClient(app)

        with caplog.at_level(logging.ERROR, logger="broker.endpoints.inputs_read"):
            resp = client.post(
                "/inputs/read",
                json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 500
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected ERROR log for non-404 S3 failure"
        assert any("run-001" in r.getMessage() for r in error_records)


class TestInputsReadSizeCap:
    def test_content_length_above_cap_returns_413_without_reading(self):
        oversize = MAX_INPUT_BYTES + 1
        # Body read should never be called when ContentLength tripwires the cap.
        body = MagicMock()
        body.read.side_effect = AssertionError("should not be read when oversize")
        app = _create_app(
            s3_response={"Body": body, "ContentLength": oversize}
        )
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 413

    def test_body_length_above_cap_returns_413_when_content_length_absent(self):
        oversize_body = b"x" * (MAX_INPUT_BYTES + 1)
        app = _create_app(
            s3_response=_make_s3_response(oversize_body, content_length=None)
        )
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 413


class TestInputsReadRequestValidation:
    def test_rejects_missing_bucket(self):
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422

    def test_rejects_empty_bucket(self):
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": "", "key": INPUT_KEY},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422

    def test_rejects_empty_key(self):
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/inputs/read",
            json={"bucket": INPUT_BUCKET, "key": ""},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422
