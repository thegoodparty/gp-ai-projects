"""Shared secrets loader — injects API keys from Secrets Manager into os.environ."""

import json
import os

from pydantic import BaseModel


class Secrets(BaseModel):
    GEMINI_API_KEY: str
    SERPER_API_KEY: str | None = None
    FIRECRAWL_API_KEY: str | None = None


_cache: Secrets | None = None


def _load_secrets() -> Secrets:
    global _cache
    if _cache is not None:
        return _cache

    import boto3

    environment = os.environ.get("ENVIRONMENT", "dev").upper()
    secret_id = os.environ.get("AI_SECRET_ID", f"AI_SECRETS_{environment}")

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    _cache = Secrets(**json.loads(response["SecretString"]))
    return _cache


def inject_secrets() -> None:
    secrets = _load_secrets()
    os.environ["GEMINI_API_KEY"] = secrets.GEMINI_API_KEY
    if secrets.SERPER_API_KEY:
        os.environ["SERPER_API_KEY"] = secrets.SERPER_API_KEY
    if secrets.FIRECRAWL_API_KEY:
        os.environ["FIRECRAWL_API_KEY"] = secrets.FIRECRAWL_API_KEY
