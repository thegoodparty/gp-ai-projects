from __future__ import annotations

import os
import re
import uuid
from urllib.parse import urlparse


def _default_dest(url: str) -> str:
    workspace = os.environ.get("PMF_WORKSPACE", "/workspace")
    downloads = os.path.join(workspace, "downloads")
    basename = os.path.basename(urlparse(url).path) or f"file-{uuid.uuid4().hex[:8]}.pdf"
    basename = re.sub(r"[^A-Za-z0-9._-]", "_", basename)
    if not basename.lower().endswith(".pdf"):
        basename += ".pdf"
    return os.path.join(downloads, basename)


def download(url: str, dest: str | None = None, purpose: str = "") -> dict:
    from .config import get_config

    dest = dest or _default_dest(url)

    client = get_config().client
    with client.stream("POST", "/pdf/fetch", json={"url": url, "purpose": purpose}) as resp:
        if resp.status_code >= 400:
            resp.read()
            try:
                data = resp.json()
                detail = data.get("detail") if isinstance(data, dict) else str(data)
            except Exception:
                detail = resp.text or f"HTTP {resp.status_code}"
            raise ValueError(f"pdf.download failed: {detail}")

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        byte_size = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes():
                if chunk:
                    f.write(chunk)
                    byte_size += len(chunk)

        source_url = resp.headers.get("x-source-url", url)
        declared = resp.headers.get("x-byte-size")
        if declared and declared.isdigit():
            byte_size = max(byte_size, int(declared))

    return {
        "path": dest,
        "byte_size": byte_size,
        "source_url": source_url,
    }
