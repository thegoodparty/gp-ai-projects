import json
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.experiment_manifest import (
    _reset_caches_for_test,
    get_experiment_metadata_bucket,
    get_s3_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-test-xyz789"
BUCKET = "agent-experiment-metadata-dev"


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """Module-level VersionId-pinned object cache + index.json TTL cache must
    be reset between tests; otherwise warm-cache reads from one test bleed
    into the next."""
    _reset_caches_for_test()
    yield
    _reset_caches_for_test()


@pytest.fixture(autouse=True)
def _silence_metrics():
    """Don't try to call CloudWatch in unit tests — _emit_metric swallows
    errors anyway, but suppressing the boto call keeps test output clean and
    avoids accidental network/AWS calls."""
    with patch("broker.endpoints.experiment_manifest._emit_metric"):
        yield


def _make_ticket(experiment_id: str = "voter_targeting", organization_slug: str = "org-7") -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-001",
        organization_slug=organization_slug,
        experiment_id=experiment_id,
        scope={},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _s3_body(payload: bytes | str) -> dict:
    body = MagicMock()
    body.read.return_value = payload.encode() if isinstance(payload, str) else payload
    return {"Body": body}


def _default_index(experiment_ids: list[str] | None = None) -> dict:
    ids = experiment_ids if experiment_ids is not None else ["voter_targeting", "walking_plan"]
    return {"experiments": [{"id": eid} for eid in ids]}


def _make_s3_responder(manifest: dict | None = None, instruction: str | None = None,
                        manifest_error: Exception | None = None,
                        instruction_error: Exception | None = None,
                        manifest_body_override: bytes | None = None,
                        instruction_body_override: bytes | None = None,
                        recorded_calls: list | None = None,
                        index: dict | None = None,
                        index_error: Exception | None = None):
    """Returns a side_effect for s3_client.get_object that routes by Key suffix.

    If `recorded_calls` is passed, append (Key, kwargs_dict) tuples for tests
    that need to assert on whether VersionId was forwarded.

    Defaults: index.json lists voter_targeting + walking_plan so the orphan
    check passes for the common case.
    """
    def _get_object(Bucket, Key, **kwargs):
        if recorded_calls is not None:
            recorded_calls.append((Key, dict(kwargs)))
        if Key == "index.json":
            if index_error:
                raise index_error
            return _s3_body(json.dumps(index if index is not None else _default_index()))
        if Key.endswith("/manifest.json"):
            if manifest_error:
                raise manifest_error
            if manifest_body_override is not None:
                return _s3_body(manifest_body_override)
            return _s3_body(json.dumps(manifest or {"id": "voter_targeting", "version": 1}))
        if Key.endswith("/instruction.md"):
            if instruction_error:
                raise instruction_error
            if instruction_body_override is not None:
                return _s3_body(instruction_body_override)
            return _s3_body(instruction or "# voter targeting instruction\n\nstep 1: foo")
        raise AssertionError(f"unexpected S3 key: {Key}")
    return _get_object


def _create_app(
    ticket: ScopeTicket | None = None,
    s3_get_object=None,
    bucket: str = BUCKET,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_scope_ticket] = lambda: ticket or _make_ticket()
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = s3_get_object or _make_s3_responder()
    app.dependency_overrides[get_s3_client] = lambda: mock_s3
    app.dependency_overrides[get_experiment_metadata_bucket] = lambda: bucket
    return app


def _no_such_key(key: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "NoSuchKey", "Message": f"key not found: {key}"}},
        operation_name="GetObject",
    )


def _other_s3_error() -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "InternalError", "Message": "S3 hiccup"}},
        operation_name="GetObject",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestExperimentManifestSuccess:
    def test_returns_manifest_and_instruction_for_matching_ticket(self):
        manifest = {
            "id": "voter_targeting",
            "version": 7,
            "mode": "win",
            "model": "sonnet",
            "max_turns": 50,
            "contract": {"schema": {"type": "object"}, "type": "json", "s3_key_template": "{experiment_id}/{run_id}/x.json"},
        }
        instruction = "# Voter Targeting\n\nStep 1: query Databricks"
        app = _create_app(s3_get_object=_make_s3_responder(manifest=manifest, instruction=instruction))
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["manifest"] == manifest
        assert body["instruction"] == instruction

    def test_calls_s3_with_correct_keys_under_configured_bucket(self):
        app = _create_app(bucket="agent-experiment-metadata-prod")
        client = TestClient(app)
        client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        s3_mock = app.dependency_overrides[get_s3_client]()
        called = {call.kwargs["Key"] for call in s3_mock.get_object.call_args_list}
        assert called == {"index.json", "voter_targeting/manifest.json", "voter_targeting/instruction.md"}
        for call in s3_mock.get_object.call_args_list:
            assert call.kwargs["Bucket"] == "agent-experiment-metadata-prod"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestExperimentManifestAuthorization:
    def test_rejects_request_for_experiment_other_than_ticket(self):
        ticket = _make_ticket(experiment_id="walking_plan")
        app = _create_app(ticket=ticket)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 403
        assert "denied" in resp.json()["detail"].lower() or "forbidden" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------


class TestExperimentManifestValidation:
    @pytest.mark.parametrize("bad_id", ["", "voter targeting", "../etc/passwd", "x" * 65])
    def test_rejects_invalid_experiment_id_format(self, bad_id):
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": bad_id},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# S3 errors
# ---------------------------------------------------------------------------


class TestExperimentManifestS3Errors:
    def test_404_when_manifest_missing(self):
        app = _create_app(s3_get_object=_make_s3_responder(manifest_error=_no_such_key("voter_targeting/manifest.json")))
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 404
        assert "voter_targeting" in resp.json()["detail"].lower()

    def test_404_when_instruction_missing(self):
        app = _create_app(s3_get_object=_make_s3_responder(instruction_error=_no_such_key("voter_targeting/instruction.md")))
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 404

    def test_500_on_other_s3_error(self):
        app = _create_app(s3_get_object=_make_s3_responder(manifest_error=_other_s3_error()))
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 500
        # User-facing message must not leak the raw boto3 error code (ARNs, account IDs, etc).
        assert "InternalError" not in resp.json().get("detail", "")
        assert "S3 hiccup" not in resp.json().get("detail", "")


# ---------------------------------------------------------------------------
# Deterministic version pinning (S3 VersionId pass-through)
# ---------------------------------------------------------------------------


class TestExperimentManifestVersionPinning:
    def test_passes_version_ids_to_s3_when_provided(self):
        recorded = []
        app = _create_app(s3_get_object=_make_s3_responder(recorded_calls=recorded))
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "manifest_version_id": "Mxxx-pinned",
                "instruction_version_id": "Iyyy-pinned",
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        manifest_call = next(c for c in recorded if c[0].endswith("manifest.json"))
        instruction_call = next(c for c in recorded if c[0].endswith("instruction.md"))
        assert manifest_call[1].get("VersionId") == "Mxxx-pinned"
        assert instruction_call[1].get("VersionId") == "Iyyy-pinned"

    def test_omits_version_id_when_not_provided(self):
        recorded = []
        app = _create_app(s3_get_object=_make_s3_responder(recorded_calls=recorded))
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        for _, kwargs in recorded:
            assert "VersionId" not in kwargs, "must not pass empty VersionId — defaults to latest"

    def test_returns_resolved_version_ids_in_response(self):
        """S3 GetObject returns VersionId in response. Surface it back to the
        caller so the runner can log what it actually got (audit trail)."""
        def _get_with_versions(Bucket, Key, **kwargs):
            if Key == "index.json":
                return _s3_body(json.dumps(_default_index()))
            payload = (
                json.dumps({"id": "voter_targeting", "version": 1}).encode()
                if Key.endswith("manifest.json")
                else b"# instruction"
            )
            response = _s3_body(payload)
            response["VersionId"] = "M-resolved-abc" if Key.endswith("manifest.json") else "I-resolved-def"
            return response

        s3 = MagicMock()
        s3.get_object.side_effect = _get_with_versions
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_scope_ticket] = lambda: _make_ticket()
        app.dependency_overrides[get_s3_client] = lambda: s3
        app.dependency_overrides[get_experiment_metadata_bucket] = lambda: BUCKET

        resp = TestClient(app).post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body_json = resp.json()
        assert body_json["resolved_manifest_version_id"] == "M-resolved-abc"
        assert body_json["resolved_instruction_version_id"] == "I-resolved-def"


# ---------------------------------------------------------------------------
# Corrupt manifest body
# ---------------------------------------------------------------------------


class TestExperimentManifestCorruptBody:
    def test_500_when_manifest_is_not_valid_json(self):
        app = _create_app(s3_get_object=_make_s3_responder(manifest_body_override=b"this is not json {"))
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 500
        assert "decode" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()

    def test_500_when_instruction_is_not_valid_utf8(self):
        # 0xff is invalid as a UTF-8 start byte. Without explicit handling, the
        # raw .decode() raises UnicodeDecodeError → uncaught 500 with no
        # structured log line. The endpoint must catch this and return a
        # well-formed 500.
        bad = b"\xff\xfe\xfd not valid utf-8"
        app = _create_app(s3_get_object=_make_s3_responder(instruction_body_override=bad))
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 500
        assert "instruction" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Orphan manifest blocking (L6 — defense in depth via index.json)
# ---------------------------------------------------------------------------


class TestExperimentManifestOrphanBlocking:
    def test_404_when_experiment_not_in_index(self):
        """A scope ticket minted before an experiment was removed from runbooks
        could still try to read the orphan manifest. The broker must consult
        index.json and refuse."""
        responder = _make_s3_responder(index={"experiments": [{"id": "walking_plan"}]})
        ticket = _make_ticket(experiment_id="voter_targeting")
        app = _create_app(ticket=ticket, s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 404
        assert "registered" in resp.json()["detail"].lower() or "not currently" in resp.json()["detail"].lower()

    def test_index_fetch_failure_falls_back_to_deny(self):
        """If index.json itself can't be fetched and there's no cached value,
        the orphan check denies all manifests. Safer than allowing them."""
        responder = _make_s3_responder(index_error=_other_s3_error())
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# VersionId pattern validation (M11)
# ---------------------------------------------------------------------------


class TestExperimentManifestVersionIdValidation:
    @pytest.mark.parametrize("bad_version", [
        "",                       # empty string
        "has spaces",             # space disallowed
        "has/slash",              # slash disallowed
        "x" * 1025,               # too long
        "weird;injection",        # semicolon disallowed
    ])
    def test_rejects_invalid_manifest_version_id(self, bad_version):
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting", "manifest_version_id": bad_version},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize("bad_version", ["x" * 1025, "spaces in id"])
    def test_rejects_invalid_instruction_version_id(self, bad_version):
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting", "instruction_version_id": bad_version},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 422

    def test_accepts_realistic_s3_version_id(self):
        # Real S3 VersionIds look like this — 32+ char base62-ish strings.
        good = "Mxl3K7LqXxL.Rq4yE9P_zN8HtY.Bd2W-"
        app = _create_app()
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "manifest_version_id": good,
                "instruction_version_id": good,
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Object cache (H10): repeated reads with the same VersionId hit the cache
# ---------------------------------------------------------------------------


class TestExperimentManifestObjectCache:
    def test_pinned_version_reads_are_cached_across_requests(self):
        """When the manifest+instruction VersionIds are pinned, the second
        request should hit the in-process cache and skip S3 entirely for those
        two objects (index.json may or may not be re-fetched depending on
        TTL — we only assert manifest/instruction were cached)."""
        recorded = []
        responder = _make_s3_responder(recorded_calls=recorded)
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        body = {
            "experiment_id": "voter_targeting",
            "manifest_version_id": "M-pinned-1",
            "instruction_version_id": "I-pinned-1",
        }
        r1 = client.post("/experiment/manifest", json=body, headers={"X-Broker-Token": BROKER_TOKEN})
        r2 = client.post("/experiment/manifest", json=body, headers={"X-Broker-Token": BROKER_TOKEN})

        assert r1.status_code == 200
        assert r2.status_code == 200

        manifest_calls = [c for c in recorded if c[0].endswith("/manifest.json")]
        instruction_calls = [c for c in recorded if c[0].endswith("/instruction.md")]
        assert len(manifest_calls) == 1, "second request must reuse cached manifest body"
        assert len(instruction_calls) == 1, "second request must reuse cached instruction body"

    def test_unpinned_version_reads_skip_cache(self):
        """Without VersionIds, "latest" can change under us — never cache."""
        recorded = []
        responder = _make_s3_responder(recorded_calls=recorded)
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        body = {"experiment_id": "voter_targeting"}
        client.post("/experiment/manifest", json=body, headers={"X-Broker-Token": BROKER_TOKEN})
        client.post("/experiment/manifest", json=body, headers={"X-Broker-Token": BROKER_TOKEN})

        manifest_calls = [c for c in recorded if c[0].endswith("/manifest.json")]
        instruction_calls = [c for c in recorded if c[0].endswith("/instruction.md")]
        assert len(manifest_calls) == 2
        assert len(instruction_calls) == 2


# ---------------------------------------------------------------------------
# Metric emission for security-relevant failures (H12, H13)
# ---------------------------------------------------------------------------


class TestExperimentManifestMetrics:
    def test_cross_experiment_denial_emits_metric(self):
        ticket = _make_ticket(experiment_id="walking_plan")
        app = _create_app(ticket=ticket)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_scope_violation_attempt" in names

    def test_orphan_block_emits_metric(self):
        responder = _make_s3_responder(index={"experiments": [{"id": "walking_plan"}]})
        ticket = _make_ticket(experiment_id="voter_targeting")
        app = _create_app(ticket=ticket, s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_orphan_manifest_blocked" in names

    def test_manifest_decode_error_emits_metric(self):
        responder = _make_s3_responder(manifest_body_override=b"not json {")
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_manifest_decode_error" in names

    def test_instruction_decode_error_emits_metric(self):
        responder = _make_s3_responder(instruction_body_override=b"\xff\xfe bad utf-8")
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_instruction_decode_error" in names

    def test_s3_other_error_emits_metric(self):
        responder = _make_s3_responder(manifest_error=_other_s3_error())
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_s3_manifest_fetch_failure" in names
