"""Unit tests for the e2e_local orchestration helpers.

The e2e_local script itself runs against a live gp-api + AWS, but the polling
helper it uses to close the loop on the callback path must be verified in
isolation — otherwise a broken callback integration can produce a green 'E2E
TEST PASSED' log while the run is actually stuck in PENDING forever.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# `test_e2e_local_full` hosts the live-gp-api runner + `wait_for_run_completion`.
# It is not present in every checkout (gated behind `E2E_LIVE=1` workflows).
# Without this importorskip, pytest fails at collection on a clean tree and
# hides every other test in the file. Day-to-day regression coverage for the
# dispatch→callback spine lives in `tests/smoke/` — run those in CI.
wait_for_run_completion = pytest.importorskip(
    "pmf_engine.tests.test_e2e_local_full",
    reason="live e2e runner module not present — covered by tests/smoke/",
).wait_for_run_completion


class FakeHttpxResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class TestWaitForRunCompletion:
    def test_returns_when_run_reaches_success(self):
        sequence = [
            FakeHttpxResponse(200, [{"runId": "run-001", "status": "PENDING"}]),
            FakeHttpxResponse(200, [{"runId": "run-001", "status": "RUNNING"}]),
            FakeHttpxResponse(200, [{"runId": "run-001", "status": "SUCCESS"}]),
        ]
        mock_client = MagicMock()
        mock_client.get.side_effect = sequence

        with patch("pmf_engine.tests.test_e2e_local_full.httpx.Client", return_value=mock_client):
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=None)
            with patch("pmf_engine.tests.test_e2e_local_full.time.sleep"):
                wait_for_run_completion(
                    token="fake-token",
                    run_id="run-001",
                    timeout=30,
                    poll_interval=0.01,
                )

        assert mock_client.get.call_count == 3

    def test_raises_on_timeout_when_run_never_terminal(self):
        mock_client = MagicMock()
        mock_client.get.return_value = FakeHttpxResponse(
            200, [{"runId": "run-001", "status": "PENDING"}]
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with patch("pmf_engine.tests.test_e2e_local_full.httpx.Client", return_value=mock_client):
            with patch("pmf_engine.tests.test_e2e_local_full.time.sleep"):
                with pytest.raises(TimeoutError, match="run-001"):
                    wait_for_run_completion(
                        token="fake-token",
                        run_id="run-001",
                        timeout=0.05,
                        poll_interval=0.01,
                    )

    def test_raises_on_terminal_failed_status(self):
        mock_client = MagicMock()
        mock_client.get.return_value = FakeHttpxResponse(
            200,
            [{"runId": "run-001", "status": "FAILED", "error": "harness crashed"}],
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with patch("pmf_engine.tests.test_e2e_local_full.httpx.Client", return_value=mock_client):
            with patch("pmf_engine.tests.test_e2e_local_full.time.sleep"):
                with pytest.raises(RuntimeError, match="FAILED"):
                    wait_for_run_completion(
                        token="fake-token",
                        run_id="run-001",
                        timeout=5,
                        poll_interval=0.01,
                    )

    def test_raises_on_contract_violation_status(self):
        mock_client = MagicMock()
        mock_client.get.return_value = FakeHttpxResponse(
            200, [{"runId": "run-001", "status": "CONTRACT_VIOLATION"}]
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with patch("pmf_engine.tests.test_e2e_local_full.httpx.Client", return_value=mock_client):
            with patch("pmf_engine.tests.test_e2e_local_full.time.sleep"):
                with pytest.raises(RuntimeError, match="CONTRACT_VIOLATION"):
                    wait_for_run_completion(
                        token="fake-token",
                        run_id="run-001",
                        timeout=5,
                        poll_interval=0.01,
                    )

    def test_raises_on_timeout_when_run_id_not_in_list(self):
        mock_client = MagicMock()
        mock_client.get.return_value = FakeHttpxResponse(
            200, [{"runId": "other-run", "status": "SUCCESS"}]
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with patch("pmf_engine.tests.test_e2e_local_full.httpx.Client", return_value=mock_client):
            with patch("pmf_engine.tests.test_e2e_local_full.time.sleep"):
                with pytest.raises(TimeoutError, match="run-001"):
                    wait_for_run_completion(
                        token="fake-token",
                        run_id="run-001",
                        timeout=0.05,
                        poll_interval=0.01,
                    )

    def test_sends_bearer_token_in_authorization_header(self):
        mock_client = MagicMock()
        mock_client.get.return_value = FakeHttpxResponse(
            200, [{"runId": "run-001", "status": "SUCCESS"}]
        )
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        with patch("pmf_engine.tests.test_e2e_local_full.httpx.Client", return_value=mock_client):
            with patch("pmf_engine.tests.test_e2e_local_full.time.sleep"):
                wait_for_run_completion(
                    token="my-jwt",
                    run_id="run-001",
                    timeout=5,
                    poll_interval=0.01,
                )

        call_kwargs = mock_client.get.call_args
        headers = call_kwargs.kwargs.get("headers") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
        )
        assert headers.get("Authorization") == "Bearer my-jwt"
