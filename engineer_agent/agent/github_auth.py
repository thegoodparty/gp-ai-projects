import textwrap
import time
from collections.abc import MutableMapping
from typing import Literal

import httpx
import jwt

from shared.logger import get_logger

logger = get_logger(__name__)

GITHUB_APP_ID = "3107048"
GITHUB_APP_INSTALLATION_ID = "117364330"

PEM_HEADER = "-----BEGIN RSA PRIVATE KEY-----"
PEM_FOOTER = "-----END RSA PRIVATE KEY-----"

AuthMode = Literal["app", "pat", "error", "none"]


def normalize_private_key(raw: str) -> str:
    body = raw.replace(PEM_HEADER, "").replace(PEM_FOOTER, "")
    body = "".join(body.split())
    return PEM_HEADER + "\n" + "\n".join(textwrap.wrap(body, 64)) + "\n" + PEM_FOOTER + "\n"


def _build_app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key_pem, algorithm="RS256")


def _mint_installation_token(env: MutableMapping[str, str], client: httpx.Client) -> str:
    app_id = env.get("GITHUB_APP_ID", GITHUB_APP_ID)
    installation_id = env.get("GITHUB_APP_INSTALLATION_ID", GITHUB_APP_INSTALLATION_ID)
    app_jwt = _build_app_jwt(app_id, normalize_private_key(env["GITHUB_APP_PRIVATE_KEY"]))
    response = client.post(
        f"/app/installations/{installation_id}/access_tokens",
        json={},
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
    )
    response.raise_for_status()
    token = response.json()["token"]
    if not token:
        raise ValueError("GitHub returned an empty installation token")
    return str(token)


def setup_github_auth(env: MutableMapping[str, str], client: httpx.Client | None = None) -> AuthMode:
    """Resolve GITHUB_TOKEN for the agent run.

    Prefers minting a short-lived GitHub App installation token from
    GITHUB_APP_PRIVATE_KEY (the delegate App), falling back to a
    pre-provisioned GITHUB_TOKEN. Installation tokens expire after one
    hour, which covers the clone-heavy start of a run.
    """
    if env.get("GITHUB_APP_PRIVATE_KEY"):
        owns_client = client is None
        if client is None:
            client = httpx.Client(base_url="https://api.github.com", timeout=30)
        try:
            env["GITHUB_TOKEN"] = _mint_installation_token(env, client)
            logger.info("GitHub App installation token acquired")
            return "app"
        except Exception as e:
            logger.error(f"GitHub App token minting failed: {e}")
        finally:
            if owns_client:
                client.close()
        if not env.get("GITHUB_TOKEN"):
            return "error"

    if env.get("GITHUB_TOKEN"):
        logger.warning("Using pre-provisioned GITHUB_TOKEN (PAT) for GitHub auth")
        return "pat"

    logger.warning("No GitHub credentials available; private repo access will fail")
    return "none"
