import os
import pytest
import httpx

from pmf_engine.runner.pmf_runtime.config import (
    PMFRuntimeConfig,
    get_config,
    init_config,
    _config,
)


class TestPMFRuntimeConfig:
    def test_config_from_explicit_args(self):
        cfg = PMFRuntimeConfig(broker_url="http://broker:8080", broker_token="tok-123")
        assert cfg.broker_url == "http://broker:8080"
        assert cfg.broker_token == "tok-123"

    def test_config_from_env_vars(self, monkeypatch):
        monkeypatch.setenv("BROKER_URL", "http://env-broker:9090")
        monkeypatch.setenv("BROKER_TOKEN", "env-tok")
        cfg = PMFRuntimeConfig()
        assert cfg.broker_url == "http://env-broker:9090"
        assert cfg.broker_token == "env-tok"

    def test_missing_broker_url_raises(self, monkeypatch):
        monkeypatch.delenv("BROKER_URL", raising=False)
        monkeypatch.delenv("BROKER_TOKEN", raising=False)
        with pytest.raises(ValueError, match="BROKER_URL is required"):
            PMFRuntimeConfig()

    def test_missing_broker_token_raises(self, monkeypatch):
        monkeypatch.setenv("BROKER_URL", "http://broker")
        monkeypatch.delenv("BROKER_TOKEN", raising=False)
        with pytest.raises(ValueError, match="BROKER_TOKEN is required"):
            PMFRuntimeConfig()

    def test_client_property_returns_httpx_client(self):
        cfg = PMFRuntimeConfig(broker_url="http://broker:8080", broker_token="tok-123")
        client = cfg.client
        assert isinstance(client, httpx.Client)
        assert client is cfg.client
        cfg.close()

    def test_client_has_correct_headers(self):
        cfg = PMFRuntimeConfig(broker_url="http://broker:8080", broker_token="tok-123")
        client = cfg.client
        assert client.headers["x-broker-token"] == "tok-123"
        cfg.close()

    def test_close_clears_client(self):
        cfg = PMFRuntimeConfig(broker_url="http://broker:8080", broker_token="tok-123")
        _ = cfg.client
        cfg.close()
        assert cfg._client is None

    def test_close_idempotent(self):
        cfg = PMFRuntimeConfig(broker_url="http://broker:8080", broker_token="tok-123")
        cfg.close()
        cfg.close()


class TestModuleFunctions:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_init_config_creates_singleton(self):
        cfg = init_config("http://broker", "tok")
        assert cfg.broker_url == "http://broker"
        assert get_config() is cfg

    def test_get_config_uses_env_when_no_init(self, monkeypatch):
        monkeypatch.setenv("BROKER_URL", "http://auto")
        monkeypatch.setenv("BROKER_TOKEN", "auto-tok")
        cfg = get_config()
        assert cfg.broker_url == "http://auto"
        assert cfg.broker_token == "auto-tok"

    def test_get_config_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("BROKER_URL", "http://auto")
        monkeypatch.setenv("BROKER_TOKEN", "auto-tok")
        assert get_config() is get_config()
