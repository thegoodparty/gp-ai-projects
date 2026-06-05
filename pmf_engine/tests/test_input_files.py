"""Behavioral tests for runner/input_files.py.

Uses httpx.MockTransport to exercise the real client + response parsing path
without hitting any network or boto3. Workspace I/O goes through tmp_path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pmf_engine.runner.input_files import prefetch_input_files

BROKER_URL = "https://broker-dev.test"
BROKER_TOKEN = "broker-token-test-123"


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(
        base_url=BROKER_URL,
        headers={"X-Broker-Token": BROKER_TOKEN},
        transport=httpx.MockTransport(handler),
    )


def _set_input_files_env(monkeypatch, entries):
    monkeypatch.setenv("INPUT_FILES_JSON", json.dumps(entries))


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


class TestPrefetchNoOp:
    def test_absent_env_var_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INPUT_FILES_JSON", raising=False)

        prefetch_input_files(str(tmp_path), BROKER_URL, BROKER_TOKEN)

        assert not (tmp_path / "input").exists()

    def test_empty_env_var_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INPUT_FILES_JSON", "")

        prefetch_input_files(str(tmp_path), BROKER_URL, BROKER_TOKEN)

        assert not (tmp_path / "input").exists()

    def test_whitespace_env_var_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INPUT_FILES_JSON", "   \n")

        prefetch_input_files(str(tmp_path), BROKER_URL, BROKER_TOKEN)

        assert not (tmp_path / "input").exists()

    def test_empty_list_is_no_op(self, tmp_path, monkeypatch):
        _set_input_files_env(monkeypatch, [])

        prefetch_input_files(str(tmp_path), BROKER_URL, BROKER_TOKEN)

        assert not (tmp_path / "input").exists()

    def test_non_list_value_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INPUT_FILES_JSON", json.dumps({"not": "a list"}))

        prefetch_input_files(str(tmp_path), BROKER_URL, BROKER_TOKEN)

        assert not (tmp_path / "input").exists()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPrefetchFetchesAndWrites:
    def test_single_entry_writes_file_to_workspace(self, tmp_path, monkeypatch):
        entry = {
            "bucket": "gp-agent-run-inputs-dev",
            "key": "uploads/org/run/agenda.pdf",
            "dest": "agenda.pdf",
        }
        _set_input_files_env(monkeypatch, [entry])

        pdf_bytes = b"%PDF-1.4 fake content"

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["x-broker-token"] = request.headers["x-broker-token"]
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=pdf_bytes)

        prefetch_input_files(
            str(tmp_path),
            BROKER_URL,
            BROKER_TOKEN,
            client=_client_returning(handler),
        )

        target = tmp_path / "input" / "agenda.pdf"
        assert target.exists()
        assert target.read_bytes() == pdf_bytes
        assert captured["url"].endswith("/inputs/read")
        assert captured["x-broker-token"] == BROKER_TOKEN
        assert captured["body"] == {
            "bucket": entry["bucket"],
            "key": entry["key"],
        }

    def test_multiple_entries_each_written(self, tmp_path, monkeypatch):
        entries = [
            {"bucket": "b1", "key": "k1", "dest": "agenda.pdf"},
            {"bucket": "b1", "key": "k2", "dest": "appendix.pdf"},
            {"bucket": "b2", "key": "k3", "dest": "minutes.pdf"},
        ]
        _set_input_files_env(monkeypatch, entries)

        bytes_by_key = {
            "k1": b"agenda body",
            "k2": b"appendix body",
            "k3": b"minutes body",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, content=bytes_by_key[body["key"]])

        prefetch_input_files(
            str(tmp_path),
            BROKER_URL,
            BROKER_TOKEN,
            client=_client_returning(handler),
        )

        assert (tmp_path / "input" / "agenda.pdf").read_bytes() == b"agenda body"
        assert (tmp_path / "input" / "appendix.pdf").read_bytes() == b"appendix body"
        assert (tmp_path / "input" / "minutes.pdf").read_bytes() == b"minutes body"


# ---------------------------------------------------------------------------
# Broker errors propagate
# ---------------------------------------------------------------------------


class TestPrefetchBrokerErrors:
    def test_broker_403_raises_http_status_error(self, tmp_path, monkeypatch):
        _set_input_files_env(
            monkeypatch,
            [{"bucket": "b", "key": "k", "dest": "agenda.pdf"}],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "Input file not authorized"})

        with pytest.raises(httpx.HTTPStatusError):
            prefetch_input_files(
                str(tmp_path),
                BROKER_URL,
                BROKER_TOKEN,
                client=_client_returning(handler),
            )

        # Don't leave a partial file behind when fetch fails.
        assert not (tmp_path / "input" / "agenda.pdf").exists()

    def test_broker_500_raises_http_status_error(self, tmp_path, monkeypatch):
        _set_input_files_env(
            monkeypatch,
            [{"bucket": "b", "key": "k", "dest": "agenda.pdf"}],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "S3 read error"})

        with pytest.raises(httpx.HTTPStatusError):
            prefetch_input_files(
                str(tmp_path),
                BROKER_URL,
                BROKER_TOKEN,
                client=_client_returning(handler),
            )

    def test_broker_404_raises(self, tmp_path, monkeypatch):
        _set_input_files_env(
            monkeypatch,
            [{"bucket": "b", "key": "k", "dest": "agenda.pdf"}],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "Input file not found"})

        with pytest.raises(httpx.HTTPStatusError):
            prefetch_input_files(
                str(tmp_path),
                BROKER_URL,
                BROKER_TOKEN,
                client=_client_returning(handler),
            )


# ---------------------------------------------------------------------------
# Local-defense path-traversal guard
# ---------------------------------------------------------------------------


class TestPrefetchUnsafeDestRejected:
    """Even if dispatch_handler and broker checks were bypassed, the runner
    must refuse to write a basename that would escape /workspace/input/."""

    @pytest.mark.parametrize(
        "unsafe_dest",
        [
            "../escape.pdf",
            "sub/file.pdf",
            ".hidden",
            "..",
            "",
            "name with space",
            "name\nwith\nnewline",
            "a" * 256,  # one past the 255-char cap
        ],
    )
    def test_rejects_unsafe_dest(self, tmp_path, monkeypatch, unsafe_dest):
        _set_input_files_env(
            monkeypatch,
            [{"bucket": "b", "key": "k", "dest": unsafe_dest}],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"should not be written")

        with pytest.raises(ValueError, match="unsafe input_files dest"):
            prefetch_input_files(
                str(tmp_path),
                BROKER_URL,
                BROKER_TOKEN,
                client=_client_returning(handler),
            )


class TestPrefetchDuplicateDestRejected:
    """Two entries with the same dest is a dispatch-side bug. Surface it
    rather than silently overwriting bytes."""

    def test_duplicate_dest_raises_on_second_write(self, tmp_path, monkeypatch):
        _set_input_files_env(
            monkeypatch,
            [
                {"bucket": "b", "key": "k1", "dest": "agenda.pdf"},
                {"bucket": "b", "key": "k2", "dest": "agenda.pdf"},
            ],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"some content")

        with pytest.raises(FileExistsError):
            prefetch_input_files(
                str(tmp_path),
                BROKER_URL,
                BROKER_TOKEN,
                client=_client_returning(handler),
            )
