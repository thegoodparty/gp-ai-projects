from __future__ import annotations

# Both `get()` and `download()` route through the broker's `/http/fetch`,
# which is backed by a shared Playwright/Chromium pool (broker/browser_fetcher.py,
# max_concurrent=30 per broker task, ~30-35 MB resident per browser context
# under typical government-site workloads — measured 2026-05-16 in dev at
# 300 concurrent requests against Granicus pages: 13% of 8 GB peak memory
# across all 30 contexts plus baseline, so ~26-32 MB per context). Heavier
# pages (JS-heavy SPAs, browser-rendered PDFs) can push per-context memory
# 5-10× higher, so don't treat this number as a hard ceiling. When the pool
# is degraded — Chromium crashed, target site is hammering Cloudflare's bot
# wall, broker task scaling out from a cold start, page-load timeouts piling
# up — the failure mode is 5xx / timeouts / repeated empty bodies, NOT a
# clean error we can backoff on per-request.
#
# DO NOT retry tightly. Short retries make it worse: each retry occupies a
# concurrency permit on the broker, blocks scale-out, and starves other
# in-flight agents. If `/http/fetch` looks degraded (3+ consecutive failures
# on different URLs, or 5xx with no clear per-URL cause), wait MINUTES — not
# seconds — before retrying. 5-15 minutes is reasonable; the broker autoscaler
# needs time to add a task and Chromium needs ~10s of warmup on the new pool.
#
# Better still: pivot the agent's plan to a deterministic-URL probe or a
# WebSearch path that doesn't go through Playwright, and only return to
# `/http/fetch` once you have a single high-value URL to fetch.
import os
import re
import uuid
from urllib.parse import urlparse

TEXTUAL_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-www-form-urlencoded",
        "application/xhtml+xml",
    }
)

CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/zip": ".zip",
    "text/csv": ".csv",
    "text/calendar": ".ics",
    "text/html": ".html",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
}


def _is_textual(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    if base.startswith("text/"):
        return True
    return base in TEXTUAL_CONTENT_TYPES


def _extension_for(content_type: str) -> str:
    base = content_type.split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_EXTENSIONS.get(base, ".bin")


def _default_dest(url: str, content_type: str) -> str:
    workspace = os.environ.get("PMF_WORKSPACE", "/workspace")
    downloads = os.path.join(workspace, "downloads")
    basename = os.path.basename(urlparse(url).path)
    if not basename:
        basename = f"file-{uuid.uuid4().hex[:8]}"
    basename = re.sub(r"[^A-Za-z0-9._-]", "_", basename)
    ext = _extension_for(content_type)
    if not basename.lower().endswith(ext.lower()):
        basename += ext
    return os.path.join(downloads, basename)


def _raise_from_error(resp, operation: str) -> None:
    resp.read()
    try:
        data = resp.json()
        detail = data.get("detail") if isinstance(data, dict) else str(data)
    except Exception:
        detail = resp.text or f"HTTP {resp.status_code}"
    raise ValueError(f"{operation} failed: {detail}")


def get(url: str, purpose: str = "") -> dict:
    from .config import get_config

    client = get_config().client
    with client.stream("POST", "/http/fetch", json={"url": url, "purpose": purpose}) as resp:
        if resp.status_code >= 400:
            _raise_from_error(resp, "http.get")

        content_type = resp.headers.get("content-type", "")
        source_url = resp.headers.get("x-source-url", url)
        upstream_status_raw = resp.headers.get("x-upstream-status", "")
        try:
            upstream_status = int(upstream_status_raw)
        except (ValueError, TypeError):
            upstream_status = resp.status_code

        if not _is_textual(content_type):
            raise ValueError(
                f"http.get cannot decode binary content-type {content_type!r}; "
                "use http.download instead"
            )

        chunks: list[bytes] = []
        byte_size = 0
        for chunk in resp.iter_bytes():
            if chunk:
                chunks.append(chunk)
                byte_size += len(chunk)

        body = b"".join(chunks).decode("utf-8", errors="replace")

        return {
            "status": upstream_status,
            "content_type": content_type,
            "body": body,
            "source_url": source_url,
            "byte_size": byte_size,
        }


def download(url: str, dest: str | None = None, purpose: str = "") -> dict:
    from .config import get_config

    client = get_config().client
    with client.stream("POST", "/http/fetch", json={"url": url, "purpose": purpose}) as resp:
        if resp.status_code >= 400:
            _raise_from_error(resp, "http.download")

        content_type = resp.headers.get("content-type", "")
        source_url = resp.headers.get("x-source-url", url)

        final_dest = dest or _default_dest(url, content_type)
        os.makedirs(os.path.dirname(final_dest) or ".", exist_ok=True)

        byte_size = 0
        with open(final_dest, "wb") as f:
            for chunk in resp.iter_bytes():
                if chunk:
                    f.write(chunk)
                    byte_size += len(chunk)

        declared = resp.headers.get("x-byte-size")
        if declared and declared.isdigit():
            byte_size = max(byte_size, int(declared))

    return {
        "path": final_dest,
        "byte_size": byte_size,
        "source_url": source_url,
        "content_type": content_type,
    }
