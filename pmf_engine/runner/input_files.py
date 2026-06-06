"""Pre-fetch user-supplied input files into the workspace before the agent boots.

The dispatch handler ships `INPUT_FILES_JSON` as an env var carrying enumerated
S3 refs (`[{bucket, key, dest}, ...]`). The runner reads it here, fetches each
ref via the broker's `/inputs/read` endpoint (which authorizes against the
ScopeTicket's `input_files` allowlist), and writes the bytes to
`/workspace/input/<dest>`. The agent then reads from disk via the canonical
path documented in `instruction.md` — it never sees S3 refs or broker URLs.

This indirection avoids the IAM-rotation failure mode that killed presigned-URL
flows: the broker's long-lived ECS task role does the S3 read on every call,
so a gp-api ECS roll mid-run doesn't invalidate anything.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


# Mirrors the dest pattern enforced by dispatch_handler._INPUT_FILE_DEST_RE
# and broker InputFileRef.dest — defense in depth. The runner is the final
# line before bytes hit disk under /workspace/input/<dest>, so a leaked
# unsafe basename here would escape the workspace despite upstream checks.
# `{0,254}` after the leading char bounds total length at 255.
_DEST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")


def prefetch_input_files(
    workspace_dir: str,
    broker_url: str,
    broker_token: str,
    *,
    client: httpx.Client | None = None,
) -> None:
    """Read INPUT_FILES_JSON, fetch each ref via broker, write to workspace/input/.

    No-op when the env var is absent or empty — most dispatches don't carry
    user-uploaded files. Failures bubble up: pre-fetch failure means the agent
    cannot read its inputs, so the run is doomed; fail fast so main()'s outer
    handler reports FAILED to gp-api cleanly rather than letting the agent hit
    FileNotFoundError mid-stride.

    A `client` arg is accepted for tests (httpx.MockTransport) — when omitted,
    the helper owns the httpx.Client lifecycle.
    """
    raw = os.environ.get("INPUT_FILES_JSON", "").strip()
    if not raw:
        return

    entries = json.loads(raw)
    if not isinstance(entries, list) or not entries:
        return

    input_dir = Path(workspace_dir) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            base_url=broker_url,
            headers={"X-Broker-Token": broker_token},
            timeout=30.0,
        )

    # Track files we've written so we can roll back on partial failure.
    # If iteration N fails after iterations 1..N-1 succeeded, the already-
    # written files would otherwise linger under /workspace/input/ — they're
    # user-uploaded PDFs that may contain PII, and the agent would see a
    # partial set of inputs which is worse than seeing none.
    written: list[Path] = []
    try:
        for entry in entries:
            bucket = entry["bucket"]
            key = entry["key"]
            dest = entry["dest"]
            if not _DEST_RE.fullmatch(dest):
                raise ValueError(f"unsafe input_files dest: {dest!r}")

            response = client.post(
                "/inputs/read",
                json={"bucket": bucket, "key": key},
            )
            response.raise_for_status()

            target = input_dir / dest
            # Exclusive-create: refuse to silently clobber a file that a prior
            # iteration of this loop (or a future workspace-setup step) wrote.
            # Duplicate `dest` across entries is a dispatch-side bug — surface
            # it loudly here rather than racing on bytes.
            with open(target, "xb") as f:
                f.write(response.content)
            written.append(target)
            logger.info(
                "prefetched_input_file dest=%r bucket=%r key=%r bytes=%d",
                dest, bucket, key, len(response.content),
            )
    except BaseException:
        # On ANY failure (broker error, unsafe dest, FileExistsError on
        # duplicate dest, KeyError on malformed entry, KeyboardInterrupt),
        # remove the files we already wrote so the agent never starts up
        # against a partial input set. Use BaseException so SystemExit /
        # KeyboardInterrupt also trigger cleanup. Unlink errors are
        # swallowed — we're already on a failure path; raising here would
        # mask the original exception.
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        if owns_client:
            client.close()
