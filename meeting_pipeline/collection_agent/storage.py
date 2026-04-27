"""
storage.py — StorageBackend Protocol + S3StorageBackend.

All file I/O in the collection agent goes through this interface.
S3 is the only supported storage backend.

Keys are always forward-slash paths relative to the bucket root, e.g.:
    "meeting_pipeline/sources/loveland-OH/source.json"
    "meeting_pipeline/output/loveland-OH/civicplus/events.json"

Keys map directly to S3 object keys within the configured bucket.
Logs are not written to S3 — append_line() is a no-op on S3StorageBackend.
"""

import json
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
    def delete(self, key: str) -> None: ...
    def get_size(self, key: str) -> int: ...
    def append_line(self, key: str, line: str) -> None: ...


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

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def append_line(self, key: str, line: str) -> None:
        # Logs are not written to S3 — no-op intentionally.
        pass

    def get_presigned_url(self, key: str, expiry_seconds: int = 300) -> str:
        """Return a presigned URL for reading an S3 object."""
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expiry_seconds,
        )
