import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from engineer_agent.agent.github_auth import (
    GITHUB_APP_ID,
    GITHUB_APP_INSTALLATION_ID,
    normalize_private_key,
    setup_github_auth,
)


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return pem, public_pem


def make_github_fake(minted_token: str, captured: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": minted_token, "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")


def flattened_secrets_manager_pem(pem: str) -> str:
    body = pem.replace("-----BEGIN RSA PRIVATE KEY-----", "").replace("-----END RSA PRIVATE KEY-----", "")
    return "-----BEGIN RSA PRIVATE KEY----- " + " ".join(body.split()) + " -----END RSA PRIVATE KEY-----"


class TestNormalizePrivateKey:
    def test_flattened_single_line_pem_is_restored_to_loadable_pem(self, rsa_key):
        pem, _ = rsa_key
        normalized = normalize_private_key(flattened_secrets_manager_pem(pem))
        loaded = serialization.load_pem_private_key(normalized.encode(), password=None)
        assert loaded.key_size == 2048

    def test_well_formed_pem_stays_loadable(self, rsa_key):
        pem, _ = rsa_key
        loaded = serialization.load_pem_private_key(normalize_private_key(pem).encode(), password=None)
        assert loaded.key_size == 2048


class TestSetupGithubAuth:
    def test_app_key_mints_installation_token_and_exports_github_token(self, rsa_key):
        pem, public_pem = rsa_key
        captured: list[httpx.Request] = []
        env = {"GITHUB_APP_PRIVATE_KEY": flattened_secrets_manager_pem(pem)}

        now = int(time.time())
        mode = setup_github_auth(env, client=make_github_fake("ghs_minted123", captured))

        assert mode == "app"
        assert env["GITHUB_TOKEN"] == "ghs_minted123"
        assert captured[0].url.path == f"/app/installations/{GITHUB_APP_INSTALLATION_ID}/access_tokens"
        bearer = captured[0].headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(bearer, public_pem, algorithms=["RS256"])
        assert claims["iss"] == GITHUB_APP_ID
        assert claims["exp"] - claims["iat"] == 600
        assert abs(claims["iat"] - (now - 60)) <= 5
        assert claims["iat"] < now

    def test_app_id_and_installation_id_overridable_via_env(self, rsa_key):
        pem, public_pem = rsa_key
        captured: list[httpx.Request] = []
        env = {
            "GITHUB_APP_PRIVATE_KEY": pem,
            "GITHUB_APP_ID": "999",
            "GITHUB_APP_INSTALLATION_ID": "888",
        }

        setup_github_auth(env, client=make_github_fake("ghs_x", captured))

        assert captured[0].url.path == "/app/installations/888/access_tokens"
        bearer = captured[0].headers["Authorization"].removeprefix("Bearer ")
        assert jwt.decode(bearer, public_pem, algorithms=["RS256"])["iss"] == "999"

    def test_no_app_key_keeps_existing_pat(self):
        env = {"GITHUB_TOKEN": "ghp_existing"}

        mode = setup_github_auth(env, client=make_github_fake("ghs_never", []))

        assert mode == "pat"
        assert env["GITHUB_TOKEN"] == "ghp_existing"

    def test_mint_failure_falls_back_to_pat(self, rsa_key):
        pem, _ = rsa_key

        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "bad jwt"})

        client = httpx.Client(transport=httpx.MockTransport(failing), base_url="https://api.github.com")
        env = {"GITHUB_APP_PRIVATE_KEY": pem, "GITHUB_TOKEN": "ghp_fallback"}

        mode = setup_github_auth(env, client=client)

        assert mode == "pat"
        assert env["GITHUB_TOKEN"] == "ghp_fallback"

    def test_mint_failure_without_pat_returns_error_mode(self, rsa_key):
        pem, _ = rsa_key

        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "bad jwt"})

        client = httpx.Client(transport=httpx.MockTransport(failing), base_url="https://api.github.com")
        env = {"GITHUB_APP_PRIVATE_KEY": pem}

        mode = setup_github_auth(env, client=client)

        assert mode == "error"
        assert "GITHUB_TOKEN" not in env

    def test_no_credentials_at_all_returns_none_mode(self):
        env: dict[str, str] = {}

        mode = setup_github_auth(env, client=make_github_fake("ghs_never", []))

        assert mode == "none"
        assert "GITHUB_TOKEN" not in env

    def test_mint_request_asks_github_api(self, rsa_key):
        pem, _ = rsa_key
        captured: list[httpx.Request] = []
        env = {"GITHUB_APP_PRIVATE_KEY": pem}

        setup_github_auth(env, client=make_github_fake("ghs_t", captured))

        req = captured[0]
        assert req.method == "POST"
        assert req.headers["Accept"] == "application/vnd.github+json"
        assert json.loads(req.content or b"{}") == {}
