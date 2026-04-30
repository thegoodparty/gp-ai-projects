"""Routing metadata for the dispatch Lambda (projection of EXPERIMENT_REGISTRY).

This module exists because the Lambda zip cannot import the full experiment
modules (they load instruction markdown at import time, pulling in ~100 KB of
prose per experiment). The Lambda only needs four fields per experiment:
`harness`, `model`, `timeout_seconds`, and `contract.s3_key_template`.

In the source tree this module imports EXPERIMENT_REGISTRY directly and
derives DISPATCH_REGISTRY at import time — single source of truth.

For Lambda deploy, `scripts/generate_flat_dispatch_registry.py` renders a flat,
import-free copy of DISPATCH_REGISTRY into `.lambda_build/dispatch_registry.py`.
The dispatch_handler's `try/except ImportError` fallback picks up whichever
version is on the path.
"""

from __future__ import annotations

try:
    from .registry import EXPERIMENT_REGISTRY
except ImportError:
    from registry import EXPERIMENT_REGISTRY  # type: ignore[no-redef]


def _derive(full: dict[str, dict]) -> dict[str, dict]:
    return {
        name: {
            "harness": exp["harness"],
            "model": exp["model"],
            "timeout_seconds": exp.get("timeout_seconds", 600),
            "required_params": exp.get("required_params", []),
            "contract": {"s3_key_template": exp["contract"]["s3_key_template"]},
        }
        for name, exp in full.items()
    }


DISPATCH_REGISTRY: dict[str, dict] = _derive(EXPERIMENT_REGISTRY)
