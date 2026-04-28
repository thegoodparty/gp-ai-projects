import os

import httpx


class PMFRuntimeConfig:
    def __init__(self, broker_url: str | None = None, broker_token: str | None = None):
        self.broker_url = broker_url or os.environ.get("BROKER_URL", "")
        self.broker_token = broker_token or os.environ.get("BROKER_TOKEN", "")
        if not self.broker_url:
            raise ValueError("BROKER_URL is required")
        if not self.broker_token:
            raise ValueError("BROKER_TOKEN is required")
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.broker_url,
                headers={"X-Broker-Token": self.broker_token},
                timeout=120.0,
            )
        return self._client

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


_config: PMFRuntimeConfig | None = None


def get_config() -> PMFRuntimeConfig:
    global _config
    if _config is None:
        _config = PMFRuntimeConfig()
    return _config


def init_config(broker_url: str, broker_token: str) -> PMFRuntimeConfig:
    global _config
    _config = PMFRuntimeConfig(broker_url=broker_url, broker_token=broker_token)
    return _config
