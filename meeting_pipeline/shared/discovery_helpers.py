"""
discovery_helpers.py — Common helper functions for source discovery.

Used across multiple discovery modules (search, probes, validation).
"""

import asyncio

import httpx


def make_candidate(
    url: str,
    platform: str,
    source: str,
    http_status: int = 0,
    display_url: str | None = None,
    config: dict | None = None,
    notes: str = "",
    body: str = "",
) -> dict:
    """Create a standardized discovery candidate dict."""
    return {
        "url": url,
        "platform": platform,
        "source": source,
        "http_status": http_status,
        "display_url": display_url or url,
        "config": config or {},
        "freshness": None,
        "most_recent_date": None,
        "days_since_update": None,
        "date_source": None,
        "notes": notes,
        "rank": None,
        "_body": body,  # cached response body for freshness verification
    }


async def safe_fetch(
    client: httpx.AsyncClient,
    url: str,
    timeout: float = 15.0,
    max_bytes: int = 200_000,
) -> tuple[int, str]:
    """
    Fetch URL and return (status_code, body).

    Negative status codes on network errors:
      -1  timeout (server likely exists)
      -2  connection error (DNS, SSL, refused)
      -5  SSL certificate error specifically
      -3  too many redirects
      -4  other error
    """
    try:
        resp = await asyncio.wait_for(
            client.get(url, follow_redirects=True),
            timeout=timeout,
        )
        body = resp.text[:max_bytes] if resp.text else ""
        return resp.status_code, body
    except TimeoutError:
        return -1, "timeout"
    except httpx.ConnectError as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg:
            return -5, f"ssl_error: {msg[:100]}"
        return -2, "connection_error"
    except httpx.TooManyRedirects:
        return -3, "too_many_redirects"
    except Exception as e:
        return -4, str(e)[:200]
