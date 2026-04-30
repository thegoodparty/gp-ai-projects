import os
import tempfile

import httpx
import pytest

from pmf_engine.runner.pmf_runtime.config import init_config
from pmf_engine.runner.pmf_runtime.pdf import download


def _inject_client(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://broker")
    cfg = init_config("http://broker", "tok")
    cfg._client = client
    return cfg


class TestDownload:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_writes_bytes_to_dest_and_returns_metadata(self, tmp_path):
        payload = b"%PDF-1.4 body"

        def handler(request):
            assert request.url.path == "/pdf/fetch"
            body = request.content.decode()
            assert "https://city.gov/budget.pdf" in body
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "application/pdf",
                    "x-source-url": "https://city.gov/budget.pdf",
                    "x-byte-size": str(len(payload)),
                },
            )

        _inject_client(handler)
        dest = str(tmp_path / "downloads" / "budget.pdf")
        result = download("https://city.gov/budget.pdf", dest=dest)

        assert os.path.exists(dest)
        with open(dest, "rb") as f:
            assert f.read() == payload
        assert result["path"] == dest
        assert result["byte_size"] == len(payload)
        assert result["source_url"] == "https://city.gov/budget.pdf"

    def test_default_dest_uses_workspace_downloads(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))
        payload = b"%PDF bytes"

        def handler(request):
            return httpx.Response(
                200,
                content=payload,
                headers={"content-type": "application/pdf"},
            )

        _inject_client(handler)
        result = download("https://city.gov/staff_report.pdf")

        assert result["path"].startswith(str(tmp_path / "downloads"))
        assert result["path"].endswith(".pdf")
        assert os.path.exists(result["path"])

    def test_error_status_raises(self):
        def handler(request):
            return httpx.Response(413, json={"detail": "PDF too large"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="PDF too large"):
            download("https://big.pdf")

    def test_400_raises_with_detail(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "URL must use https scheme"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="https"):
            download("http://insecure.pdf")

    def test_streams_large_payload_without_loading_to_memory(self, tmp_path):
        # emit 5 MB in chunks
        big = b"\x00" * (5 * 1024 * 1024)

        def handler(request):
            return httpx.Response(200, content=big, headers={"content-type": "application/pdf"})

        _inject_client(handler)
        dest = str(tmp_path / "big.pdf")
        result = download("https://city.gov/big.pdf", dest=dest)

        assert result["byte_size"] == len(big)
        assert os.path.getsize(dest) == len(big)
