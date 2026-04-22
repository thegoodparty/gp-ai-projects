import json
import httpx
import pytest

from pmf_engine.runner.pmf_runtime.config import init_config
from pmf_engine.runner.pmf_runtime.publish import publish, report_status, upload_logs


def _inject_client(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://broker")
    cfg = init_config("http://broker", "tok")
    cfg._client = client
    return cfg


class TestPublish:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_publish_success(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["artifact"]["score"] == 0.95
            return httpx.Response(200, json={"id": "art-123", "status": "accepted"})

        _inject_client(handler)
        result = publish({"score": 0.95})
        assert result["id"] == "art-123"
        assert result["status"] == "accepted"

    def test_publish_400_raises_value_error(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "schema mismatch"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="Artifact rejected: schema mismatch"):
            publish({"bad": "data"})

    def test_publish_400_error_key_fallback(self):
        def handler(request):
            return httpx.Response(400, json={"error": "too large"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="Artifact rejected: too large"):
            publish({})

    def test_publish_400_unknown_fallback(self):
        def handler(request):
            return httpx.Response(400, json={})

        _inject_client(handler)
        with pytest.raises(ValueError, match="Artifact rejected: unknown"):
            publish({})


class TestReportStatus:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_report_status_success(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["status"] == "running"
            assert body["progress"] == 50
            return httpx.Response(200, json={"ack": True})

        _inject_client(handler)
        result = report_status("running", progress=50)
        assert result["ack"] is True

    def test_report_status_minimal(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ack": True})

        _inject_client(handler)
        report_status("complete")
        assert captured["body"] == {"status": "complete"}

    def test_report_status_forwards_duration_seconds_and_cost_usd(self):
        """Failure callbacks must carry real duration/cost so gp-api's
        ExperimentRun.durationSeconds / .costUsd aren't 0 for every failed run.
        Wire format is snake_case at the runner→broker HTTP boundary."""
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ack": True})

        _inject_client(handler)
        report_status(
            "failed",
            reason_code="x",
            detail="y",
            duration_seconds=42.5,
            cost_usd=0.37,
        )
        assert captured["body"]["status"] == "failed"
        assert captured["body"]["reason_code"] == "x"
        assert captured["body"]["detail"] == "y"
        assert captured["body"]["duration_seconds"] == 42.5
        assert captured["body"]["cost_usd"] == 0.37


class TestUploadLogs:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_upload_logs_success(self):
        captured = {}

        def handler(request):
            assert b"stdout.log" in request.content
            assert b"hello world" in request.content
            return httpx.Response(200, json={"uploaded": 1})

        _inject_client(handler)
        result = upload_logs({"stdout.log": b"hello world"})
        assert result["uploaded"] == 1

    def test_upload_multiple_files(self):
        def handler(request):
            content = request.content
            assert b"stdout.log" in content
            assert b"stderr.log" in content
            return httpx.Response(200, json={"uploaded": 2})

        _inject_client(handler)
        result = upload_logs({
            "stdout.log": b"out data",
            "stderr.log": b"err data",
        })
        assert result["uploaded"] == 2

    def test_upload_logs_uses_files_form_field_name(self):
        """Every multipart part must use form field name 'files'.

        The broker declares `files: list[UploadFile] = File(...)` which binds
        to a form field literally named `files`. If the runner sends each part
        under its filename as the field name (e.g. `stdout.log`), FastAPI
        cannot bind the parts and returns 422 Unprocessable Entity.
        Regression guard for that bug.
        """
        captured = {}

        def handler(request):
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            return httpx.Response(200, json={"uploaded": 2})

        _inject_client(handler)
        upload_logs({"stdout.log": b"out", "stderr.log": b"err"})

        assert "multipart/form-data" in captured["content_type"]
        # Every Content-Disposition part must name the field "files".
        body = captured["body"]
        name_headers = [line for line in body.split(b"\r\n") if line.startswith(b"Content-Disposition:")]
        assert len(name_headers) == 2, f"expected 2 parts, got {len(name_headers)}: {name_headers}"
        for header in name_headers:
            assert b'name="files"' in header, (
                f"part uses wrong form field name (broker expects 'files'): {header!r}"
            )
