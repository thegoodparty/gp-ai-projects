"""
storage.py — StorageBackend Protocol + LocalStorageBackend + S3StorageBackend.

All file I/O in the collection agent goes through this interface so that
swapping LocalStorageBackend for S3StorageBackend requires zero changes
to business logic.

Keys are always forward-slash paths relative to the backend root, e.g.:
    "meeting_pipeline/sources/loveland-OH/source.json"
    "meeting_pipeline/logs/collection_agent.jsonl"
    "meeting_pipeline/output/loveland-OH/civicplus/events.json"

For S3, keys map directly to S3 object keys within the configured bucket.
Logs are not written to S3 — append_line() is a no-op on S3StorageBackend.
"""

import json
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Interface for all persistent storage operations."""

    def read_json(self, key: str) -> dict: ...
    def write_json(self, key: str, data: dict) -> None: ...
    def write_bytes(self, key: str, data: bytes) -> None: ...
    def read_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def get_size(self, key: str) -> int: ...
    def append_line(self, key: str, line: str) -> None: ...


class LocalStorageBackend:
    """
    File-system implementation of StorageBackend.

    base_dir: repo root (e.g. /path/to/gp-ai-projects)
    All keys are resolved relative to base_dir.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()

    def _path(self, key: str) -> Path:
        # Prevent path traversal attacks
        p = (self.base_dir / key).resolve()
        if not str(p).startswith(str(self.base_dir)):
            raise ValueError(f"Key '{key}' escapes base_dir")
        return p

    def read_json(self, key: str) -> dict:
        p = self._path(key)
        with open(p) as f:
            return json.load(f)

    def write_json(self, key: str, data: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def get_size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.base_dir))
            for p in base.rglob("*")
            if p.is_file()
        ]

    def append_line(self, key: str, line: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")

    def abs_path(self, key: str) -> Path:
        """Return the absolute filesystem path for a key (local use only)."""
        return self._path(key)


class S3StorageBackend:
    """
    S3 implementation of StorageBackend.

    bucket: S3 bucket name (e.g. "meeting-pipeline-dev")
    All keys map directly to S3 object keys within the bucket.

    Logs are not written to S3 — append_line() is intentionally a no-op.
    """

    def __init__(self, bucket: str, profile: str | None = None):
        import boto3
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.s3 = session.client("s3")
        self.bucket = bucket

    def read_json(self, key: str) -> dict:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))

    def write_json(self, key: str, data: dict) -> None:
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")

    def write_bytes(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def read_bytes(self, key: str) -> bytes:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def get_size(self, key: str) -> int:
        from botocore.exceptions import ClientError
        try:
            response = self.s3.head_object(Bucket=self.bucket, Key=key)
            return response["ContentLength"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return 0
            raise

    def list_keys(self, prefix: str) -> list[str]:
        keys = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def append_line(self, key: str, line: str) -> None:
        # Logs are not written to S3 — no-op intentionally.
        pass
