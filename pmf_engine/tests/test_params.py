"""Tests for runner/params.py and the PARAMS_VIA_BROKER branch of from_env.

Uses httpx.MockTransport to exercise the real client + response parsing path
without hitting any network, mirroring test_input_files.py.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from pmf_engine.runner.config import RunnerConfig
from pmf_engine.runner.params import fetch_params_from_broker

BROKER_URL = "https://broker-dev.test"
BROKER_TOKEN = "broker-token-test-123"


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(
        base_url=BROKER_URL,
        headers={"X-Broker-Token": BROKER_TOKEN},
        transport=httpx.MockTransport(handler),
    )


class TestFetchParamsFromBroker:
    def test_returns_params_dict(self):
        params = {"opponent": {"full_name": "Jane Doe"}, "issues": "x" * 18000}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/params/read"
            assert request.headers["X-Broker-Token"] == BROKER_TOKEN
            return httpx.Response(200, json=params)

        out = fetch_params_from_broker(
            broker_url=BROKER_URL,
            broker_token=BROKER_TOKEN,
            client=_client_returning(handler),
        )

        assert out == params

    def test_non_object_response_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "an", "object"])

        with pytest.raises(ValueError, match="must return an object"):
            fetch_params_from_broker(
                broker_url=BROKER_URL,
                broker_token=BROKER_TOKEN,
                client=_client_returning(handler),
            )

    def test_http_error_bubbles_up(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad token")

        with pytest.raises(httpx.HTTPStatusError):
            fetch_params_from_broker(
                broker_url=BROKER_URL,
                broker_token=BROKER_TOKEN,
                client=_client_returning(handler),
            )


class TestFromEnvParamsViaBroker:
    def _base_env(self, monkeypatch):
        # No EXPERIMENT_ID → from_env skips the manifest broker fetch, isolating
        # the params path.
        monkeypatch.delenv("EXPERIMENT_ID", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        monkeypatch.setenv("BROKER_URL", BROKER_URL)
        monkeypatch.setenv("BROKER_TOKEN", BROKER_TOKEN)

    def test_fetches_from_broker_and_ignores_inline_params(self, monkeypatch):
        self._base_env(monkeypatch)
        monkeypatch.setenv("PARAMS_VIA_BROKER", "1")
        # An inline value must NOT win when the broker path is active.
        monkeypatch.setenv("PARAMS_JSON", json.dumps({"stale": "inline"}))
        fetched = {"candidate_platform": {"issues": "y" * 18000}}

        with patch(
            "pmf_engine.runner.params.fetch_params_from_broker",
            return_value=fetched,
        ) as mock_fetch:
            config = RunnerConfig.from_env()

        assert config.params == fetched
        mock_fetch.assert_called_once_with(
            broker_url=BROKER_URL, broker_token=BROKER_TOKEN
        )

    def test_missing_broker_token_raises(self, monkeypatch):
        self._base_env(monkeypatch)
        monkeypatch.setenv("PARAMS_VIA_BROKER", "1")
        monkeypatch.delenv("BROKER_TOKEN", raising=False)

        with pytest.raises(RuntimeError, match="BROKER_URL and BROKER_TOKEN"):
            RunnerConfig.from_env()

    def test_inline_params_used_when_flag_absent(self, monkeypatch):
        self._base_env(monkeypatch)
        monkeypatch.delenv("PARAMS_VIA_BROKER", raising=False)
        monkeypatch.setenv("PARAMS_JSON", json.dumps({"district": "CA-12"}))

        with patch(
            "pmf_engine.runner.params.fetch_params_from_broker"
        ) as mock_fetch:
            config = RunnerConfig.from_env()

        assert config.params == {"district": "CA-12"}
        mock_fetch.assert_not_called()
