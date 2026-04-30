import io
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.upload_logs import (
    router,
    get_scope_ticket,
    get_s3_client,
    get_artifact_bucket,
)

BROKER_TOKEN = "broker-token-test-abc123"


def _make_ticket(
    experiment_id: str = "voter_targeting",
    organization_slug: str = "org-7",
    run_id: str = "run-upload-test",
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
        issued_by="test",
    )


def _create_app(
    ticket: ScopeTicket | None = None,
    s3_error: Exception | None = None,
    bucket: str = "gp-agent-artifacts-dev",
) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    _ticket = ticket or _make_ticket()
    app.dependency_overrides[get_scope_ticket] = lambda: _ticket

    mock_s3 = MagicMock()
    if s3_error:
        mock_s3.put_object.side_effect = s3_error
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    app.dependency_overrides[get_artifact_bucket] = lambda: bucket

    return app, mock_s3


class TestUploadLogsSuccess:
    def test_single_file_uploaded_to_correct_s3_path(self):
        app, mock_s3 = _create_app(
            ticket=_make_ticket(
                experiment_id="voter_targeting",
                run_id="run-777",
            )
        )
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("conversation.jsonl", b"line1\nline2\n"))],
        )

        assert response.status_code == 200
        body = response.json()
        assert body["uploaded"] == ["voter_targeting/run-777/logs/conversation.jsonl"]

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "gp-agent-artifacts-dev"
        assert call_kwargs["Key"] == "voter_targeting/run-777/logs/conversation.jsonl"
        assert call_kwargs["Body"] == b"line1\nline2\n"

    def test_multiple_files_all_uploaded(self):
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[
                ("files", ("conversation.jsonl", b"conv-content")),
                ("files", ("stdout.log", b"stdout-content")),
                ("files", ("stderr.log", b"stderr-content")),
            ],
        )

        assert response.status_code == 200
        assert len(response.json()["uploaded"]) == 3
        assert mock_s3.put_object.call_count == 3


class TestUploadLogsValidation:
    def test_rejects_path_traversal_in_filename(self):
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("../etc/passwd", b"evil"))],
        )

        assert response.status_code == 400
        assert "filename" in response.json()["detail"].lower()
        mock_s3.put_object.assert_not_called()

    def test_allows_subdir_path_in_filename(self):
        """Runner collects log files recursively and sends them under paths like
        `workspace/api_responses/voter_scores.json`. The broker must accept
        non-traversal subpaths (the S3 key is just `{experiment_id}/{run_id}/logs/{path}`)."""
        app, mock_s3 = _create_app(
            ticket=_make_ticket(experiment_id="meeting_briefing", run_id="run-sub"),
        )
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("workspace/api_responses/voter_scores.json", b"{}"))],
        )

        assert response.status_code == 200
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Key"] == "meeting_briefing/run-sub/logs/workspace/api_responses/voter_scores.json"

    def test_rejects_absolute_path_in_filename(self):
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("/etc/passwd", b"evil"))],
        )

        assert response.status_code == 400
        mock_s3.put_object.assert_not_called()

    def test_rejects_empty_filename(self):
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("", b"x"))],
        )

        assert response.status_code in (400, 422)
        mock_s3.put_object.assert_not_called()

    def test_accepts_large_file(self):
        """Broker does not cap per-file size — the runner's _collect_workspace_files
        already caps at 50MB per file / 200MB total. The broker just transports
        whatever the authenticated run chose to send. Historical 10MB cap caused
        silent drops of municipal meeting packets (typically 40-60 MB)."""
        app, mock_s3 = _create_app()
        client = TestClient(app)

        # 15 MB file — would have been rejected by old 10 MB cap, should now upload.
        big = b"x" * (15 * 1024 * 1024)
        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("march_2026_packet.pdf", big))],
        )

        assert response.status_code == 200
        mock_s3.put_object.assert_called_once()

    def test_rejects_no_files(self):
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post("/internal/upload-logs", files=[])

        assert response.status_code == 422  # FastAPI multipart validation


class TestUploadLogsUnicodeAndStrictValidation:
    def test_rejects_unicode_two_dot_leader_in_filename(self):
        """U+2025 (‥) is a Unicode confusable for `..` — the loose ASCII-only `..`
        check misses this and would let an attacker compose an S3 key like
        `{exp}/{run}/logs/‥/evil.log`. Strict allowlist regex rejects."""
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("‥/evil.log", b"evil"))],
        )

        assert response.status_code == 400
        assert "filename" in response.json()["detail"].lower()
        mock_s3.put_object.assert_not_called()

    def test_rejects_fullwidth_dots_in_filename(self):
        """U+FF0E (．) fullwidth full-stop is another `..` confusable."""
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("．．/evil.log", b"evil"))],
        )

        assert response.status_code == 400
        mock_s3.put_object.assert_not_called()

    def test_rejects_non_ascii_filename(self):
        """Strict ASCII-only regex — non-ASCII characters always rejected."""
        app, mock_s3 = _create_app()
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("hello_世界.log", b"data"))],
        )

        assert response.status_code == 400
        mock_s3.put_object.assert_not_called()

    def test_accepts_legitimate_nested_filename(self):
        """Regression guard: legit subdirs with ASCII chars still upload."""
        app, mock_s3 = _create_app(
            ticket=_make_ticket(experiment_id="voter_targeting", run_id="run-nest"),
        )
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("logs/run-1/output.jsonl", b"{}"))],
        )

        assert response.status_code == 200
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Key"] == "voter_targeting/run-nest/logs/logs/run-1/output.jsonl"

    def test_rejects_filename_over_200_chars(self):
        """Filename length capped at 200 chars to bound S3 key length."""
        app, mock_s3 = _create_app()
        client = TestClient(app)

        oversized = "a" * 201
        response = client.post(
            "/internal/upload-logs",
            files=[("files", (oversized, b"data"))],
        )

        assert response.status_code == 400
        mock_s3.put_object.assert_not_called()


class TestUploadLogsS3Failure:
    def test_s3_error_returns_500(self):
        app, mock_s3 = _create_app(s3_error=RuntimeError("s3 down"))
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("conversation.jsonl", b"x"))],
        )

        assert response.status_code == 500


class TestUploadLogsScopeTicketDrivesPath:
    def test_path_uses_ticket_experiment_id_and_run_id_not_client_input(self):
        """Agents can't forge run_id — the path comes from the scope ticket only."""
        app, mock_s3 = _create_app(
            ticket=_make_ticket(
                experiment_id="district_intel",
                run_id="the-real-run-id",
            )
        )
        client = TestClient(app)

        response = client.post(
            "/internal/upload-logs",
            files=[("files", ("attacker.jsonl", b"data"))],
        )

        assert response.status_code == 200
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Key"] == "district_intel/the-real-run-id/logs/attacker.jsonl"
