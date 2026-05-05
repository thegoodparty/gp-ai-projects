from __future__ import annotations


def get(url: str, purpose: str = "") -> dict:
    from .config import get_config

    response = get_config().client.post("/http/fetch", json={"url": url, "purpose": purpose})
    if response.status_code >= 400:
        try:
            data = response.json()
            detail = data.get("detail") if isinstance(data, dict) else str(data)
        except Exception:
            detail = response.text or f"HTTP {response.status_code}"
        raise ValueError(f"http.get failed: {detail}")

    return response.json()
