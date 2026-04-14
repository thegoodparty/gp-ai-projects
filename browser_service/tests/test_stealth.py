import re

from browser_service.app.stealth import (
    LOCALES,
    USER_AGENTS,
    VIEWPORTS,
    get_random_locale,
    get_random_user_agent,
    get_random_viewport,
    get_stealth_headers,
)

KNOWN_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
    {"width": 2560, "height": 1440},
    {"width": 1600, "height": 900},
]


class TestGetRandomUserAgent:
    def test_looks_like_real_browser(self):
        ua = get_random_user_agent()
        assert "Chrome/" in ua or "Firefox/" in ua

    def test_contains_version_number(self):
        ua = get_random_user_agent()
        assert re.search(r"\d+\.\d+", ua), f"No version number found in: {ua}"

    def test_variety(self):
        results = {get_random_user_agent() for _ in range(50)}
        assert len(results) > 1, "Expected variety in user agents"


class TestGetRandomViewport:
    def test_from_known_set(self):
        vp = get_random_viewport()
        assert vp in KNOWN_VIEWPORTS

    def test_variety(self):
        results = {(v["width"], v["height"]) for v in (get_random_viewport() for _ in range(50))}
        assert len(results) > 1, "Expected variety in viewports"


class TestGetStealthHeaders:
    def test_returns_exact_stealth_headers(self):
        headers = get_stealth_headers()
        assert headers == {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }


class TestGetRandomLocale:
    def test_returns_locale_from_known_set(self):
        locale = get_random_locale()
        assert locale in {"en-US", "en-GB", "en-CA", "en-AU"}

    def test_variety(self):
        results = {get_random_locale() for _ in range(50)}
        assert len(results) > 1, "Expected variety in locales"
