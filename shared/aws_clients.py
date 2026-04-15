"""Memoized boto3 client factory.

Consolidates the "_xxx_client = None + def get_xxx_client()" pattern that was
copy-pasted across dispatch_handler, callback_handler, runner/main, and
campaign_plan_lambda. Call `reset_client_cache()` between tests that need
isolated state.
"""

from __future__ import annotations

from typing import Any

import boto3

_clients: dict[str, Any] = {}


def get_client(service: str, **kwargs):
    cache_key = service
    if kwargs:
        cache_key = f"{service}:{sorted(kwargs.items())}"
    if cache_key not in _clients:
        _clients[cache_key] = boto3.client(service, **kwargs)
    return _clients[cache_key]


def reset_client_cache() -> None:
    _clients.clear()
