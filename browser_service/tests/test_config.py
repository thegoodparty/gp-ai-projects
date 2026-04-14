import os

from browser_service.app.config import Settings


class TestSettings:
    def test_defaults(self):
        settings = Settings()
        assert settings.MAX_CONCURRENT_CONTEXTS == 5
        assert settings.DEFAULT_TIMEOUT_MS == 30000
        assert settings.PORT == 8000

    def test_override_via_env(self, monkeypatch):
        monkeypatch.setenv("MAX_CONCURRENT_CONTEXTS", "10")
        monkeypatch.setenv("DEFAULT_TIMEOUT_MS", "60000")
        monkeypatch.setenv("PORT", "9000")
        settings = Settings()
        assert settings.MAX_CONCURRENT_CONTEXTS == 10
        assert settings.DEFAULT_TIMEOUT_MS == 60000
        assert settings.PORT == 9000
