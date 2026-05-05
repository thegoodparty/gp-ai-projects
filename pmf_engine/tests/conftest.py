"""Shared pytest config for pmf_engine tests.

Registers the `e2e` marker so the full-stack local e2e test (which needs a
running gp-api, AWS creds, and a real Claude agent run) is excluded by
default. Run it explicitly with `pytest -m e2e`.
"""
from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: full-stack local end-to-end test "
        "(requires local gp-api, AWS creds, Claude agent; run with -m e2e)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") == "e2e":
        return
    skip_e2e = __import__("pytest").mark.skip(reason="e2e test — run with `pytest -m e2e`")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
