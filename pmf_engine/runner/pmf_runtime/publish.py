import logging
import time

import httpx

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ),
    )


def _with_retry(call_name: str, fn):
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == _MAX_ATTEMPTS - 1 or not _is_retryable(exc):
                raise
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning(
                f"{call_name} failed (attempt {attempt + 1}/{_MAX_ATTEMPTS}): "
                f"{type(exc).__name__}: {exc}; retrying in {delay}s"
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def publish(artifact: dict) -> dict:
    from .config import get_config
    response = get_config().client.post("/artifact/publish", json={"artifact": artifact})
    if response.status_code == 400:
        data = response.json()
        raise ValueError(f"Artifact rejected: {data.get('detail', data.get('error', 'unknown'))}")
    response.raise_for_status()
    return response.json()


def report_status(
    status: str,
    duration_seconds: float | None = None,
    cost_usd: float | None = None,
    **kwargs,
) -> dict:
    from .config import get_config
    body: dict = {"status": status, **kwargs}
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    if cost_usd is not None:
        body["cost_usd"] = cost_usd

    def _call() -> dict:
        response = get_config().client.post("/internal/run-status", json=body)
        response.raise_for_status()
        return response.json()

    return _with_retry("report_status", _call)


def upload_logs(files: dict[str, bytes]) -> dict:
    from .config import get_config
    file_parts = [("files", (name, data)) for name, data in files.items()]
    response = get_config().client.post("/internal/upload-logs", files=file_parts)
    response.raise_for_status()
    return response.json()
