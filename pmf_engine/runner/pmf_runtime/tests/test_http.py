import json
import os

import httpx
import pytest

from pmf_engine.runner.pmf_runtime.config import init_config
from pmf_engine.runner.pmf_runtime.http import download, get, head


def _inject_client(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://broker")
    cfg = init_config("http://broker", "tok")
    cfg._client = client
    return cfg


def _reset_config():
    import pmf_engine.runner.pmf_runtime.config as config_mod
    config_mod._config = None


class TestGet:
    def setup_method(self):
        _reset_config()

    def test_returns_text_body_for_html(self):
        payload = b"<html><body>hi</body></html>"

        def handler(request):
            assert request.url.path == "/http/fetch"
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "x-upstream-status": "200",
                    "x-source-url": "https://example.com/",
                    "x-byte-size": str(len(payload)),
                },
            )

        _inject_client(handler)
        result = get("https://example.com/")

        assert result["status"] == 200
        assert result["content_type"] == "text/html; charset=utf-8"
        assert result["body"] == "<html><body>hi</body></html>"
        assert result["source_url"] == "https://example.com/"
        assert result["byte_size"] == len(payload)

    def test_returns_text_body_for_json_content_type(self):
        payload = b'[{"EventId": 1}]'

        def handler(request):
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "application/json",
                    "x-upstream-status": "200",
                    "x-source-url": "https://api.example.com/events",
                    "x-byte-size": str(len(payload)),
                },
            )

        _inject_client(handler)
        result = get("https://api.example.com/events")
        assert result["body"] == '[{"EventId": 1}]'
        assert result["content_type"] == "application/json"

    def test_returns_text_body_for_csv(self):
        payload = b"a,b\n1,2\n"

        def handler(request):
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "text/csv",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/data.csv",
                    "x-byte-size": str(len(payload)),
                },
            )

        _inject_client(handler)
        result = get("https://x.gov/data.csv")
        assert result["body"] == "a,b\n1,2\n"

    def test_returns_text_body_for_xml(self):
        payload = b"<root><item/></root>"

        def handler(request):
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "application/xml",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/feed.xml",
                    "x-byte-size": str(len(payload)),
                },
            )

        _inject_client(handler)
        result = get("https://x.gov/feed.xml")
        assert result["body"] == "<root><item/></root>"

    def test_raises_for_pdf_content_type(self):
        def handler(request):
            return httpx.Response(
                200,
                content=b"%PDF-1.4 ...",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/budget.pdf",
                    "x-byte-size": "12",
                },
            )

        _inject_client(handler)
        with pytest.raises(ValueError, match="cannot decode binary content-type"):
            get("https://x.gov/budget.pdf")

    def test_raises_for_docx_content_type(self):
        def handler(request):
            return httpx.Response(
                200,
                content=b"PK\x03\x04",
                headers={
                    "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/doc.docx",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        with pytest.raises(ValueError, match="cannot decode binary content-type"):
            get("https://x.gov/doc.docx")

    def test_status_reflects_x_upstream_status_header(self):
        def handler(request):
            return httpx.Response(
                200,
                content=b"not found",
                headers={
                    "content-type": "text/plain",
                    "x-upstream-status": "404",
                    "x-source-url": "https://api.example.com/missing",
                    "x-byte-size": "9",
                },
            )

        _inject_client(handler)
        result = get("https://api.example.com/missing")
        assert result["status"] == 404
        assert result["body"] == "not found"

    def test_byte_size_counted_from_stream(self):
        payload = b"hello world"

        def handler(request):
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "text/plain",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/x",
                },
            )

        _inject_client(handler)
        result = get("https://x.gov/x")
        assert result["byte_size"] == len(payload)

    def test_broker_403_raises_value_error_with_detail(self):
        def handler(request):
            return httpx.Response(403, json={"detail": "not on the broker allowlist"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="allowlist"):
            get("https://badsite.com/exfil")

    def test_broker_400_raises_value_error(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "URL must use https scheme"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="https"):
            get("http://insecure.gov/x")

    def test_broker_500_raises_value_error(self):
        def handler(request):
            return httpx.Response(500, json={"detail": "upstream error"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="upstream error"):
            get("https://x.gov/x")

    def test_passes_purpose_to_broker(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=b"[]",
                headers={
                    "content-type": "application/json",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/x",
                    "x-byte-size": "2",
                },
            )

        _inject_client(handler)
        get("https://x.gov/x", purpose="list events")
        assert captured["body"]["purpose"] == "list events"

    def test_posts_to_http_fetch_path(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["method"] = request.method
            return httpx.Response(
                200,
                content=b"ok",
                headers={
                    "content-type": "text/plain",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/x",
                    "x-byte-size": "2",
                },
            )

        _inject_client(handler)
        get("https://x.gov/x")
        assert seen["path"] == "/http/fetch"
        assert seen["method"] == "POST"


class TestDownload:
    def setup_method(self):
        _reset_config()

    def test_writes_bytes_to_dest_and_returns_metadata(self, tmp_path):
        payload = b"%PDF-1.4 body"

        def handler(request):
            assert request.url.path == "/http/fetch"
            body = json.loads(request.content)
            assert body["url"] == "https://city.gov/budget.pdf"
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
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
        assert result["content_type"] == "application/pdf"

    def test_default_dest_uses_workspace_downloads_with_pdf_ext(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))
        payload = b"%PDF bytes"

        def handler(request):
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/staff_report.pdf",
                    "x-byte-size": str(len(payload)),
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/staff_report.pdf")

        assert result["path"].startswith(str(tmp_path / "downloads"))
        assert result["path"].endswith(".pdf")
        assert os.path.exists(result["path"])

    def test_default_dest_uses_docx_ext_for_docx_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"PK\x03\x04docx",
                headers={
                    "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/agenda",
                    "x-byte-size": "8",
                },
            )

        _inject_client(handler)
        # URL path has no extension; default should append .docx
        result = download("https://city.gov/agenda")
        assert result["path"].endswith(".docx")
        assert os.path.exists(result["path"])

    def test_default_dest_uses_xlsx_ext_for_xlsx_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"PK\x03\x04xlsx",
                headers={
                    "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/budget",
                    "x-byte-size": "8",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/budget")
        assert result["path"].endswith(".xlsx")

    def test_default_dest_uses_zip_ext_for_zip_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"PK\x03\x04zip",
                headers={
                    "content-type": "application/zip",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/bundle",
                    "x-byte-size": "7",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/bundle")
        assert result["path"].endswith(".zip")

    def test_default_dest_uses_csv_ext_for_csv_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"a,b\n1,2\n",
                headers={
                    "content-type": "text/csv",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/data",
                    "x-byte-size": "8",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/data")
        assert result["path"].endswith(".csv")

    def test_default_dest_uses_bin_ext_for_unknown_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"\x00\x01\x02",
                headers={
                    "content-type": "application/octet-stream",
                    "x-upstream-status": "200",
                    "x-source-url": "https://x.gov/blob",
                    "x-byte-size": "3",
                },
            )

        _inject_client(handler)
        result = download("https://x.gov/blob")
        assert result["path"].endswith(".bin")

    def test_does_not_double_suffix_when_url_already_has_extension(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"%PDF",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/budget.pdf",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/budget.pdf")
        # basename is "budget.pdf"; should NOT become "budget.pdf.pdf"
        assert result["path"].endswith("budget.pdf")
        assert not result["path"].endswith(".pdf.pdf")

    def test_does_not_double_suffix_case_insensitive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"%PDF",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/Budget.PDF",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/Budget.PDF")
        # Existing .PDF suffix should be treated as matching .pdf
        assert result["path"].endswith("Budget.PDF")
        assert ".PDF.pdf" not in result["path"]

    def test_sanitizes_basename_special_chars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"%PDF",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/weird name (v2).pdf",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/weird name (v2).pdf")
        basename = os.path.basename(result["path"])
        # spaces and parens must be replaced
        assert " " not in basename
        assert "(" not in basename
        assert ")" not in basename

    def test_generates_basename_when_url_path_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))

        def handler(request):
            return httpx.Response(
                200,
                content=b"%PDF",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/")
        basename = os.path.basename(result["path"])
        assert basename.startswith("file-")
        assert basename.endswith(".pdf")

    def test_honors_explicit_dest(self, tmp_path):
        def handler(request):
            return httpx.Response(
                200,
                content=b"%PDF",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/budget.pdf",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        explicit = str(tmp_path / "custom" / "name.pdf")
        result = download("https://city.gov/budget.pdf", dest=explicit)
        assert result["path"] == explicit
        assert os.path.exists(explicit)

    def test_error_status_raises_with_detail(self):
        def handler(request):
            return httpx.Response(413, json={"detail": "response exceeded 10485760 bytes"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="exceeded"):
            download("https://big.pdf")

    def test_400_raises_with_detail(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "URL must use https scheme"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="https"):
            download("http://insecure.pdf")

    def test_streams_large_payload_without_loading_to_memory(self, tmp_path):
        big = b"\x00" * (5 * 1024 * 1024)

        def handler(request):
            return httpx.Response(
                200,
                content=big,
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/big.pdf",
                    "x-byte-size": str(len(big)),
                },
            )

        _inject_client(handler)
        dest = str(tmp_path / "big.pdf")
        result = download("https://city.gov/big.pdf", dest=dest)

        assert result["byte_size"] == len(big)
        assert os.path.getsize(dest) == len(big)

    def test_passes_purpose_to_broker(self, tmp_path):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=b"%PDF",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/x.pdf",
                    "x-byte-size": "4",
                },
            )

        _inject_client(handler)
        download("https://city.gov/x.pdf", dest=str(tmp_path / "x.pdf"), purpose="staff report")
        assert captured["body"]["purpose"] == "staff report"

    def test_prefers_x_byte_size_header_when_larger(self, tmp_path):
        # If broker reports more bytes than we received (rare; defensive),
        # take the header value to match prior pdf.download behavior.
        def handler(request):
            return httpx.Response(
                200,
                content=b"hello",
                headers={
                    "content-type": "application/pdf",
                    "x-upstream-status": "200",
                    "x-source-url": "https://city.gov/x.pdf",
                    "x-byte-size": "9999",
                },
            )

        _inject_client(handler)
        result = download("https://city.gov/x.pdf", dest=str(tmp_path / "x.pdf"))
        assert result["byte_size"] == 9999


class TestHead:
    def setup_method(self):
        _reset_config()

    def test_returns_status_and_final_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/http/head"
            assert json.loads(request.content)["url"] == "https://example.gov/p"
            return httpx.Response(200, json={"status": 200, "final_url": "https://example.gov/p"})

        _inject_client(handler)
        result = head("https://example.gov/p")
        assert result["status"] == 200
        assert result["final_url"] == "https://example.gov/p"

    def test_passes_through_non_200_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": 404, "final_url": "https://example.gov/missing"})

        _inject_client(handler)
        result = head("https://example.gov/missing")
        assert result["status"] == 404

    def test_raises_value_error_on_broker_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": "SSRF blocked"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="http.head failed: SSRF blocked"):
            head("http://10.0.0.5/internal")

    def test_raises_value_error_on_malformed_200_body_missing_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"final_url": "https://example.gov/p"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="malformed"):
            head("https://example.gov/p")
