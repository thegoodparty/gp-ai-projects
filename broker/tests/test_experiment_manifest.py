import json
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from broker.dynamodb_client import ScopeTicket
from broker.endpoints import experiment_manifest as em
from broker.endpoints.experiment_manifest import (
    _reset_caches_for_test,
    _reset_fetch_executor_for_test,
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
    into the next. Also reset the module-level fetch executor so a test that
    swaps it doesn't leak into the next test."""
    _reset_caches_for_test()
    _reset_fetch_executor_for_test()
    yield
    _reset_caches_for_test()
    _reset_fetch_executor_for_test()


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


def _s3_body(payload: bytes | str, content_length: int | None = None) -> dict:
    """Build a synthetic S3 GetObject response.

    `content_length` lets a test pin `response["ContentLength"]` so size-cap
    guards can be exercised. When omitted, real S3 always returns one; the
    handler must also defend against a missing/lying value by checking the
    actual byte count after a bounded read.
    """
    raw = payload.encode() if isinstance(payload, str) else payload
    body = MagicMock()
    # Match boto3's StreamingBody.read(amt) semantics: if no amount is
    # requested, return all the bytes; if an amount is requested, return up
    # to that many. The handler probes for oversize bodies by reading
    # MAX+1 bytes — keep that path exercised.
    body.read.side_effect = lambda amt=None: raw if amt is None else raw[:amt]
    resp: dict = {"Body": body}
    if content_length is not None:
        resp["ContentLength"] = content_length
    else:
        resp["ContentLength"] = len(raw)
    return resp


def _default_index(
    experiment_ids: list[str] | None = None,
    attachment_keys: dict[str, list[str]] | None = None,
    qa_keys: dict[str, list[str]] | None = None,
    qa_manifest_key: dict[str, str] | None = None,
) -> dict:
    """Build a synthetic index.json.

    `attachment_keys` maps experiment_id → list of "<id>/attachments/<basename>"
    entries. The broker reads this list to decide which sidecars to fetch.

    `qa_keys` maps experiment_id → list of "<id>/qa/<basename>" entries
    (EXCLUDING manifest.json, which is carried separately as `qa_manifest_key`
    per contract F). `qa_manifest_key` maps experiment_id → "<id>/qa/manifest.json".
    Both are absent from the entry when not provided — that's the byte-identical
    no-qa shape every existing experiment has today.
    """
    ids = experiment_ids if experiment_ids is not None else ["voter_targeting", "walking_plan"]
    att = attachment_keys or {}
    qak = qa_keys or {}
    qam = qa_manifest_key or {}
    experiments = []
    for eid in ids:
        entry: dict = {"id": eid, "attachment_keys": att.get(eid, [])}
        if eid in qak:
            entry["qa_keys"] = qak[eid]
        if eid in qam:
            entry["qa_manifest_key"] = qam[eid]
        experiments.append(entry)
    return {"experiments": experiments}


def _make_s3_responder(manifest: dict | None = None, instruction: str | None = None,
                        manifest_error: Exception | None = None,
                        instruction_error: Exception | None = None,
                        manifest_body_override: bytes | None = None,
                        instruction_body_override: bytes | None = None,
                        recorded_calls: list | None = None,
                        index: dict | None = None,
                        index_error: Exception | None = None,
                        attachments: dict[str, bytes | str] | None = None,
                        attachment_version_ids: dict[str, str] | None = None,
                        attachment_errors: dict[str, Exception] | None = None,
                        attachment_content_lengths: dict[str, int] | None = None,
                        qa_files: dict[str, bytes | str] | None = None,
                        qa_version_ids: dict[str, str] | None = None,
                        qa_errors: dict[str, Exception] | None = None,
                        qa_content_lengths: dict[str, int] | None = None):
    """Returns a side_effect for s3_client.get_object that routes by Key suffix.

    If `recorded_calls` is passed, append (Key, kwargs_dict) tuples for tests
    that need to assert on whether VersionId was forwarded.

    Defaults: index.json lists voter_targeting + walking_plan so the orphan
    check passes for the common case.

    `attachments` is keyed by basename (matching the request/response shape
    the runner sees) — the responder routes any `<experiment_id>/attachments/<basename>`
    GET to the matching body. `attachment_version_ids` lets a test pin the
    VersionId surfaced in the GetObject response (so resolved_*_version_ids
    can be asserted on). `attachment_errors` injects a per-basename failure
    so we can exercise the missing-attachment path.
    """
    att_bodies = attachments or {}
    att_versions = attachment_version_ids or {}
    att_errors = attachment_errors or {}
    att_lengths = attachment_content_lengths or {}
    qa_bodies = qa_files or {}
    qa_versions = qa_version_ids or {}
    qa_errs = qa_errors or {}
    qa_lengths = qa_content_lengths or {}

    def _get_object(Bucket, Key, **kwargs):
        if recorded_calls is not None:
            recorded_calls.append((Key, dict(kwargs)))
        if Key == "index.json":
            if index_error:
                raise index_error
            return _s3_body(json.dumps(index if index is not None else _default_index()))
        # qa routing comes BEFORE the "/manifest.json" suffix check so a qa
        # manifest at "<id>/qa/manifest.json" routes here, not to the
        # experiment-manifest branch.
        if "/qa/" in Key:
            basename = Key.split("/qa/", 1)[1]
            if basename in qa_errs:
                raise qa_errs[basename]
            if basename not in qa_bodies:
                raise AssertionError(f"unexpected qa S3 key: {Key}")
            response = _s3_body(
                qa_bodies[basename],
                content_length=qa_lengths.get(basename),
            )
            if basename in qa_versions:
                response["VersionId"] = qa_versions[basename]
            return response
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
        if "/attachments/" in Key:
            basename = Key.split("/attachments/", 1)[1]
            if basename in att_errors:
                raise att_errors[basename]
            if basename not in att_bodies:
                raise AssertionError(f"unexpected attachment S3 key: {Key}")
            response = _s3_body(
                att_bodies[basename],
                content_length=att_lengths.get(basename),
            )
            if basename in att_versions:
                response["VersionId"] = att_versions[basename]
            return response
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
        # Use the responder's `recorded_calls` capture instead of reaching
        # into FastAPI's dependency_overrides. The helper exists for this
        # exact purpose — keeps the test agnostic to whether the broker
        # asks for the S3 client once, twice, or via a different mechanism.
        # We also wrap the responder to capture Bucket so we can assert
        # the configured bucket is what every GET goes to.
        recorded: list = []
        inner = _make_s3_responder(recorded_calls=recorded)
        called_buckets: list[str] = []

        def _bucket_recording_responder(Bucket, Key, **kwargs):
            called_buckets.append(Bucket)
            return inner(Bucket=Bucket, Key=Key, **kwargs)

        app = _create_app(
            s3_get_object=_bucket_recording_responder,
            bucket="agent-experiment-metadata-prod",
        )
        client = TestClient(app)
        client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        called_keys = {key for key, _ in recorded}
        assert called_keys == {
            "index.json",
            "voter_targeting/manifest.json",
            "voter_targeting/instruction.md",
        }
        assert called_buckets, "expected at least one S3 call"
        assert all(b == "agent-experiment-metadata-prod" for b in called_buckets)


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
        # Pinned to the exact substring from the handler's denial detail.
        # Disjunction-substring matches hide drift: if someone reworded the
        # detail, an `"x" in d or "y" in d` would pass on the wrong branch
        # and silently miss the rename.
        assert "manifest access denied" in resp.json()["detail"]


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
        # Detail labels which object is missing without leaking the full
        # S3 key (internal pathing). Manifest 404 must say "manifest".
        detail = resp.json()["detail"]
        assert "manifest not found" in detail
        assert "voter_targeting" not in detail

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
        # Exact substring from the handler's manifest-decode detail.
        assert "manifest decode error" in resp.json()["detail"]

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
        # Exact substring from the orphan-block handler detail.
        assert "experiment not currently registered" in resp.json()["detail"]

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


# ---------------------------------------------------------------------------
# Attachments — sidecar files the publisher ships next to instruction.md
# ---------------------------------------------------------------------------
#
# Contract: index.json's per-experiment entry carries `attachment_keys` listing
# every "<experiment_id>/attachments/<basename>". The broker fetches each one
# (parallel with manifest+instruction), exposes them in the response as
# `attachments[basename] = utf8_body`, and surfaces per-attachment VersionIds
# in `resolved_attachment_version_ids[basename]` for audit-trail symmetry
# with manifest_version_id / instruction_version_id.


class TestExperimentManifestAttachmentFetch:
    def test_attachments_in_index_are_fetched_and_returned_by_basename(self):
        index = _default_index(attachment_keys={
            "voter_targeting": [
                "voter_targeting/attachments/notes.md",
                "voter_targeting/attachments/lookup.csv",
            ],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={
                "notes.md": "# notes\nfoo\n",
                "lookup.csv": "k,v\n1,a\n",
            },
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        # Basename-keyed so the runner can write straight to /workspace/<basename>.
        assert body["attachments"] == {
            "notes.md": "# notes\nfoo\n",
            "lookup.csv": "k,v\n1,a\n",
        }

    def test_no_attachments_in_index_yields_empty_attachments_dict(self):
        """Existing experiments have no attachment_keys in their index entry.
        Default response shape must keep `attachments` as an empty dict — not
        absent, not None — so runners can iterate it unconditionally."""
        app = _create_app()  # _default_index has attachment_keys=[] by default
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.json()["attachments"] == {}
        assert resp.json()["resolved_attachment_version_ids"] == {}

    def test_runner_never_fetches_attachments_not_listed_in_index(self):
        """The index is the canonical list of what's published. A runner
        request that names an attachment not in the index must NOT trigger
        a speculative S3 GET on `<id>/attachments/<basename>` — that would
        be a vector for probing arbitrary keys via attachment_version_ids."""
        recorded: list = []
        index = _default_index(attachment_keys={"voter_targeting": []})
        responder = _make_s3_responder(index=index, recorded_calls=recorded)
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                # Request a pin for an attachment that's not in the index.
                # Broker must ignore it.
                "attachment_version_ids": {"phantom.md": "V-phantom-1"},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        fetched_keys = [Key for Key, _ in recorded]
        assert not any("phantom.md" in k for k in fetched_keys)

    def test_attachment_with_unsafe_prefix_skipped(self):
        """A hand-edited index entry with an attachment_key that doesn't
        start with `<experiment_id>/attachments/` would let a malformed
        index point at an unrelated S3 key. Broker must skip those."""
        recorded: list = []
        index = {
            "experiments": [{
                "id": "voter_targeting",
                "attachment_keys": [
                    "voter_targeting/attachments/legit.md",
                    "other_experiment/attachments/cross.md",  # wrong prefix
                    "../etc/passwd",                          # nonsense
                ],
            }],
        }
        responder = _make_s3_responder(
            index=index,
            attachments={"legit.md": "ok\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.json()["attachments"] == {"legit.md": "ok\n"}
        fetched = {k for k, _ in recorded}
        assert "other_experiment/attachments/cross.md" not in fetched
        assert "../etc/passwd" not in fetched


class TestExperimentManifestAttachmentVersionPinning:
    def test_resolved_attachment_version_ids_surfaced(self):
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/lookup.csv"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={"lookup.csv": "k,v\n"},
            attachment_version_ids={"lookup.csv": "V-lookup-abc-123"},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        assert resp.json()["resolved_attachment_version_ids"] == {
            "lookup.csv": "V-lookup-abc-123",
        }

    def test_attachment_version_ids_forwarded_to_s3(self):
        """Symmetric with manifest_version_id pinning: when the runner
        passes attachment_version_ids, those VersionIds must reach S3 so
        the pinned-replay path returns the same bytes Lambda saw."""
        recorded: list = []
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/notes.md"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={"notes.md": "frozen\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "attachment_version_ids": {"notes.md": "V-notes-pin-7"},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        att_call = next(c for c in recorded if c[0].endswith("/notes.md"))
        assert att_call[1].get("VersionId") == "V-notes-pin-7"

    def test_unpinned_attachment_omits_version_id_to_s3(self):
        """Without a pin, the broker must default to 'latest' (no
        VersionId in the GetObject kwargs). Sending an empty string would
        be rejected by S3."""
        recorded: list = []
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/notes.md"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={"notes.md": "latest\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        att_call = next(c for c in recorded if c[0].endswith("/notes.md"))
        assert "VersionId" not in att_call[1]


class TestExperimentManifestAttachmentValidation:
    @pytest.mark.parametrize("bad_key", [
        "has/slash.md",      # path separator in basename — not safe
        "..",                # traversal
        ".",                 # current-dir
        "spaces in name.md", # whitespace not in basename pattern
        "",                  # empty
    ])
    def test_rejects_unsafe_attachment_version_id_keys(self, bad_key):
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "attachment_version_ids": {bad_key: "V-pinned"},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422

    def test_rejects_unsafe_attachment_version_id_values(self):
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "attachment_version_ids": {"notes.md": "has space"},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422


class TestExperimentManifestAttachmentDecodeError:
    def test_non_utf8_attachment_returns_500_and_emits_metric(self):
        """Binary attachments are explicitly unsupported. A publisher
        accident (e.g. uploading a PDF) must surface as a loud 500 with a
        metric, not corrupt the runner workspace by silently coercing."""
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/bad.bin"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={"bad.bin": b"\xff\xfe\xfd not utf-8"},
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 500
        assert "attachment" in resp.json()["detail"].lower()
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_attachment_decode_error" in names


# ---------------------------------------------------------------------------
# B1 — Attachment size cap + count cap + bounded executor (DoS defense)
# ---------------------------------------------------------------------------


class TestExperimentManifestSizeAndCountCaps:
    def test_rejects_attachment_exceeding_size_cap(self):
        """Unbounded `response['Body'].read()` would OOM the broker on a
        publisher accident (e.g. someone uploads a 1 GB attachment). The
        handler must check ContentLength and short-circuit before reading
        the body."""
        oversize_bytes = b"a" * (em.MAX_ATTACHMENT_BYTES + 1)
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/huge.bin"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={"huge.bin": oversize_bytes},
            # ContentLength is what real S3 reports; the cap must trip on it.
            attachment_content_lengths={"huge.bin": em.MAX_ATTACHMENT_BYTES + 1},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        # 502 — broker refused to surface upstream object that violates
        # the size contract. Detail must mention "size cap".
        assert resp.status_code == 502
        assert "exceeds size cap" in resp.json()["detail"]

    def test_rejects_attachment_with_missing_content_length_but_oversize_body(self):
        """Defense in depth: even if ContentLength is missing or lying,
        a bounded read with `read(MAX+1)` catches the oversize case before
        the broker process holds gigabytes in memory."""
        oversize_bytes = b"b" * (em.MAX_ATTACHMENT_BYTES + 1)
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/huge.bin"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={"huge.bin": oversize_bytes},
            # ContentLength=0 lies about the size — handler must still
            # catch the oversize via the bounded read.
            attachment_content_lengths={"huge.bin": 0},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 502
        assert "exceeds size cap" in resp.json()["detail"]

    def test_caps_attachment_count_per_experiment_at_max(self):
        """An index entry with thousands of attachment_keys would spawn
        thousands of threads under the old per-request executor. After
        the cap, the handler truncates to MAX_ATTACHMENTS_PER_EXPERIMENT
        and emits a drift metric so operators see the publisher bug."""
        # Build an index that exceeds the cap.
        total = em.MAX_ATTACHMENTS_PER_EXPERIMENT + 10
        keys = [f"voter_targeting/attachments/a{i:04d}.md" for i in range(total)]
        bodies = {f"a{i:04d}.md": f"body-{i}\n" for i in range(total)}
        index = _default_index(attachment_keys={"voter_targeting": keys})
        recorded: list = []
        responder = _make_s3_responder(
            index=index,
            attachments=bodies,
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200
        # Count S3 GETs targeting `/attachments/`; expect at most MAX, not total.
        attachment_gets = [k for k, _ in recorded if "/attachments/" in k]
        assert len(attachment_gets) == em.MAX_ATTACHMENTS_PER_EXPERIMENT, (
            f"expected exactly {em.MAX_ATTACHMENTS_PER_EXPERIMENT} attachment "
            f"fetches after truncation, got {len(attachment_gets)}"
        )
        names = [call.args[0] for call in mock_metric.call_args_list]
        assert "broker_attachment_count_exceeded" in names


# ---------------------------------------------------------------------------
# B2 — LRU cache evicts least-recently-used (not a full flush)
# ---------------------------------------------------------------------------


class TestExperimentManifestLRUCache:
    def test_cache_evicts_lru_not_full_flush(self):
        """The cache must evict the oldest entry when it overflows, not
        wipe everything. Otherwise hot manifest/instruction entries get
        churned out whenever a different experiment is fetched."""
        # Pre-fill the cache to capacity with synthetic keys; touch
        # entries in a known order so we can assert LRU semantics.
        em._OBJECT_CACHE.clear()
        maxsize = em._OBJECT_CACHE_MAX

        # Inject `maxsize` distinct entries.
        for i in range(maxsize):
            em._OBJECT_CACHE.put(
                ("bucket", f"key-{i}", "v"),
                f"body-{i}".encode(),
                "v",
            )

        # Touch the oldest entry so it becomes MRU.
        em._OBJECT_CACHE.get(("bucket", "key-0", "v"), ttl=None)

        # Add one more entry — overflow. With LRU semantics, the
        # least-recently-used should be evicted. After touching key-0,
        # the LRU is key-1.
        em._OBJECT_CACHE.put(
            ("bucket", "new-key", "v"),
            b"new",
            "v",
        )

        # key-1 should be gone (it was the LRU after the touch).
        assert em._OBJECT_CACHE.get(("bucket", "key-1", "v"), ttl=None) is None
        # key-0 (touched) should still be present.
        assert em._OBJECT_CACHE.get(("bucket", "key-0", "v"), ttl=None) is not None
        # New key should be present.
        assert em._OBJECT_CACHE.get(("bucket", "new-key", "v"), ttl=None) is not None
        # Some other middle key should still be there too.
        assert em._OBJECT_CACHE.get(("bucket", f"key-{maxsize - 1}", "v"), ttl=None) is not None


# ---------------------------------------------------------------------------
# B3 — Unified basename safety helper (whitespace, etc.)
# ---------------------------------------------------------------------------


class TestExperimentManifestUnsafeBasename:
    def test_index_basename_with_whitespace_rejected(self):
        """The validator for `attachment_version_ids` already rejects
        whitespace basenames at the request boundary; the index-side
        loop must use the same helper so a malformed index entry can't
        slip a `spaces in name.md` basename past defense-in-depth."""
        recorded: list = []
        index = {
            "experiments": [{
                "id": "voter_targeting",
                "attachment_keys": [
                    "voter_targeting/attachments/legit.md",
                    "voter_targeting/attachments/spaces in name.md",
                ],
            }],
        }
        responder = _make_s3_responder(
            index=index,
            attachments={"legit.md": "ok\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200
        # The unsafe key was skipped; only the legit attachment came back.
        assert resp.json()["attachments"] == {"legit.md": "ok\n"}
        fetched = {k for k, _ in recorded}
        assert "voter_targeting/attachments/spaces in name.md" not in fetched
        # Drift metric must fire with the unsafe-basename label.
        drift_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_attachment_index_drift"
        ]
        assert drift_calls, "expected broker_attachment_index_drift metric"
        dims = [
            d for call in drift_calls for d in call.args[1]
            if d.get("Name") == "drift_kind"
        ]
        assert any(d.get("Value") == "unsafe_basename" for d in dims)


# ---------------------------------------------------------------------------
# B4 — Index-drift error reporting via metric
# ---------------------------------------------------------------------------


class TestExperimentManifestIndexDriftMetrics:
    def test_non_string_attachment_key_emits_drift_metric(self):
        """A non-string entry in attachment_keys is publisher-side drift.
        The old code silently `continue`d at WARNING — operators saw
        nothing in CloudWatch. Now it must fire a drift metric with
        kind=non_string_key."""
        recorded: list = []
        index = {
            "experiments": [{
                "id": "voter_targeting",
                "attachment_keys": [
                    "voter_targeting/attachments/legit.md",
                    42,  # non-string drift
                ],
            }],
        }
        responder = _make_s3_responder(
            index=index,
            attachments={"legit.md": "ok\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200
        # Only the legit one comes back.
        assert resp.json()["attachments"] == {"legit.md": "ok\n"}
        drift_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_attachment_index_drift"
        ]
        assert drift_calls
        kinds = {
            d.get("Value")
            for call in drift_calls
            for d in call.args[1]
            if d.get("Name") == "drift_kind"
        }
        assert "non_string_key" in kinds

    def test_wrong_prefix_attachment_key_emits_drift_metric(self):
        """The wrong-prefix path used to log WARNING with no metric —
        operators couldn't alert on it. Must emit the drift metric with
        kind=wrong_prefix."""
        index = {
            "experiments": [{
                "id": "voter_targeting",
                "attachment_keys": [
                    "voter_targeting/attachments/legit.md",
                    "other_experiment/attachments/cross.md",
                ],
            }],
        }
        responder = _make_s3_responder(
            index=index,
            attachments={"legit.md": "ok\n"},
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200
        drift_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_attachment_index_drift"
        ]
        assert drift_calls
        kinds = {
            d.get("Value")
            for call in drift_calls
            for d in call.args[1]
            if d.get("Name") == "drift_kind"
        }
        assert "wrong_prefix" in kinds


# ---------------------------------------------------------------------------
# B5 — Non-list attachment_keys in index drops + emits drift metric
# ---------------------------------------------------------------------------


class TestExperimentManifestNonListAttachmentKeys:
    def test_non_list_attachment_keys_in_index_drops_and_emits_drift(self):
        """If `attachment_keys` is a string instead of a list, iterating
        over it yields one S3 GET per *character* — a silent bug. The
        handler must treat non-list as drift, skip entirely, and emit
        a metric."""
        recorded: list = []
        index = {
            "experiments": [{
                "id": "voter_targeting",
                # Drift: string, not a list.
                "attachment_keys": "single.md",
            }],
        }
        responder = _make_s3_responder(
            index=index,
            attachments={},  # any attachment fetch raises AssertionError
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200
        # No attachment S3 GETs attempted — confirms the per-character
        # iteration bug is fixed.
        attachment_gets = [k for k, _ in recorded if "/attachments/" in k]
        assert attachment_gets == []
        assert resp.json()["attachments"] == {}
        drift_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_attachment_index_drift"
        ]
        assert drift_calls
        kinds = {
            d.get("Value")
            for call in drift_calls
            for d in call.args[1]
            if d.get("Name") == "drift_kind"
        }
        assert "non_list_attachment_keys" in kinds


# ---------------------------------------------------------------------------
# B6 — Index fetch failure emits a metric (empty fallback = total blackout)
# ---------------------------------------------------------------------------


class TestExperimentManifestIndexFetchFailureMetric:
    def test_index_fetch_failure_empty_fallback_emits_metric(self):
        """When index.json can't be fetched and there's no cached copy,
        the orphan check denies every manifest — a total broker
        blackout. Old code logged WARNING with no metric; operators
        would never alert. Must emit `broker_index_fetch_failure` with
        fallback=empty."""
        responder = _make_s3_responder(index_error=_other_s3_error())
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 404
        fetch_failure_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_index_fetch_failure"
        ]
        assert fetch_failure_calls
        fallbacks = {
            d.get("Value")
            for call in fetch_failure_calls
            for d in call.args[1]
            if d.get("Name") == "fallback"
        }
        assert "empty" in fallbacks


# ---------------------------------------------------------------------------
# B7 — Attachment 404 detail labels object correctly (not "manifest")
# ---------------------------------------------------------------------------


class TestExperimentManifestAttachmentMissingDetail:
    def test_attachment_404_detail_says_attachment_not_manifest(self):
        """If an attachment listed in index.json has been deleted from
        S3, the 404 detail must say `attachment not found`, not
        `manifest not found` (which would mislead the runner / operator
        into looking at the wrong S3 path)."""
        index = _default_index(attachment_keys={
            "voter_targeting": ["voter_targeting/attachments/gone.md"],
        })
        responder = _make_s3_responder(
            index=index,
            attachments={},
            attachment_errors={"gone.md": _no_such_key("voter_targeting/attachments/gone.md")},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)
        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "attachment not found" in detail
        assert "manifest" not in detail


# ---------------------------------------------------------------------------
# B9 — Max-length experiment_id is accepted (pin the boundary, not just over)
# ---------------------------------------------------------------------------


class TestExperimentManifestIdLengthBoundary:
    def test_accepts_max_length_experiment_id(self):
        """EXPERIMENT_ID_PATTERN allows `[a-z][a-z0-9_]{0,63}` — total
        max length 64. The existing parametrized test only pins 65
        (above-cap); pin 64 (at-cap) too so a future regex tweak that
        drops the cap to 63 fails loudly."""
        ticket_id = "v" + ("x" * 63)  # 64 chars total, first char a-z
        ticket = _make_ticket(experiment_id=ticket_id)
        # Make the index list the experiment so the orphan check passes.
        index = _default_index(experiment_ids=[ticket_id])
        responder = _make_s3_responder(index=index)
        app = _create_app(ticket=ticket, s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": ticket_id},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PMF QA gate (contract H, v1 observe-only): the broker serves the qa folder
# under a SEPARATE response envelope key `qa`, NOT merged into `attachments`
# (decision 4). The qa folder's manifest.json is carried separately (contract
# F / decision 3) at `<id>/qa/manifest.json`; the other entrypoints
# (main.py / eval.md) come through `qa_keys`. The runner forwards its
# QA_VERSION_IDS env var as the request's `qa_version_ids` field, mirroring
# attachment_version_ids. When the experiment publishes no qa folder, the
# response omits `qa` entirely so the no-qa path is byte-identical (decision 10).
# ---------------------------------------------------------------------------


class TestExperimentManifestQaNoFolder:
    def test_no_qa_folder_omits_qa_key_entirely(self):
        """Every existing experiment has no qa_keys / qa_manifest_key in its
        index entry. The response must NOT carry a `qa` key at all (not None
        in the wire either) so the no-qa containerOverrides stay byte-identical
        to today and older runners parse unchanged."""
        app = _create_app()  # _default_index has no qa keys
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        # `qa` omitted: the route returns a JSONResponse with a manual
        # payload.pop("qa", None) when no qa folder ran (NOT FastAPI's
        # response_model_exclude_none), keeping the no-qa wire identical to the
        # pre-gate shape. Pin the FULL top-level key set so an accidental new
        # always-serialized field (or a leaked `qa: null`) is caught — not just
        # that `qa` is absent.
        assert set(body.keys()) == {
            "manifest",
            "instruction",
            "resolved_manifest_version_id",
            "resolved_instruction_version_id",
            "attachments",
            "resolved_attachment_version_ids",
        }
        assert "qa" not in body
        # And the attachments machinery is untouched.
        assert body["attachments"] == {}
        assert body["resolved_attachment_version_ids"] == {}

    def test_qa_folder_does_not_trigger_attachment_fetches(self):
        """A qa folder must NOT leak into attachment GETs — the two surfaces
        are disjoint. With only qa keys in the index (no attachment_keys),
        zero `/attachments/` GETs fire."""
        recorded: list = []
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/main.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False}), "main.py": "print(1)\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        attachment_gets = [k for k, _ in recorded if "/attachments/" in k]
        assert attachment_gets == []


class TestExperimentManifestQaFetch:
    def test_qa_folder_packaged_under_separate_envelope_key(self):
        """When the experiment publishes a qa folder, the response carries a
        `qa` envelope: {manifest: <decoded json>, files: {basename: body},
        resolved_qa_version_ids: {basename: vid}}. The qa manifest is parsed
        JSON; the entrypoints are utf-8 strings keyed by basename."""
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": [
                "voter_targeting/qa/main.py",
                "voter_targeting/qa/eval.md",
            ]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={
                "manifest.json": json.dumps({"blocking": False}),
                "main.py": "import sys\nprint('[]')\n",
                "eval.md": "# Evaluator\nGrade faithfulness.\n",
            },
            qa_version_ids={
                "manifest.json": "V-qa-man-1",
                "main.py": "V-qa-main-1",
                "eval.md": "V-qa-eval-1",
            },
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        qa = resp.json()["qa"]
        # Manifest decoded as JSON (a dict), not a raw string.
        assert qa["manifest"] == {"blocking": False}
        # Entrypoints keyed by basename, utf-8 decoded.
        assert qa["files"] == {
            "main.py": "import sys\nprint('[]')\n",
            "eval.md": "# Evaluator\nGrade faithfulness.\n",
        }
        # VersionIds for every fetched qa object (manifest + entrypoints).
        assert qa["resolved_qa_version_ids"] == {
            "manifest.json": "V-qa-man-1",
            "main.py": "V-qa-main-1",
            "eval.md": "V-qa-eval-1",
        }

    def test_qa_envelope_is_disjoint_from_attachments(self):
        """qa files live under their own key and never appear in attachments,
        even when the same experiment ships BOTH attachments and a qa folder
        (decision 4: separate envelope, never merged)."""
        index = _default_index(
            attachment_keys={"voter_targeting": ["voter_targeting/attachments/notes.md"]},
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/main.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            attachments={"notes.md": "attachment body\n"},
            qa_files={
                "manifest.json": json.dumps({"blocking": False}),
                "main.py": "qa body\n",
            },
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        # Attachments only carry the attachment; the qa file is NOT in there.
        assert body["attachments"] == {"notes.md": "attachment body\n"}
        assert "main.py" not in body["attachments"]
        # qa carries only the qa file; the attachment is NOT in there.
        assert body["qa"]["files"] == {"main.py": "qa body\n"}
        assert "notes.md" not in body["qa"]["files"]

    def test_qa_keys_excludes_manifest_but_manifest_still_fetched(self):
        """Contract F: qa_keys excludes manifest.json (carried separately as
        qa_manifest_key). The broker must still fetch the manifest from
        qa_manifest_key, decode it, and place it under qa.manifest — even
        though it never appears in qa_keys."""
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": []},  # no entrypoints, just the manifest
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False, "agent": {"model": "sonnet"}})},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        qa = resp.json()["qa"]
        assert qa["manifest"] == {"blocking": False, "agent": {"model": "sonnet"}}
        assert qa["files"] == {}

    def test_qa_files_fetched_under_qa_prefix(self):
        """The broker enumerates the qa folder under `<id>/qa/` — never a
        runner-supplied basename — exactly like attachments use
        `<id>/attachments/`."""
        recorded: list = []
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/main.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False}), "main.py": "x\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        qa_gets = {k for k, _ in recorded if "/qa/" in k}
        assert qa_gets == {
            "voter_targeting/qa/manifest.json",
            "voter_targeting/qa/main.py",
        }


class TestExperimentManifestQaVersionPinning:
    def test_qa_version_ids_forwarded_to_s3(self):
        """Symmetric with attachment_version_ids: when the runner forwards
        QA_VERSION_IDS, those VersionIds must reach S3 so the pinned-replay
        path returns the same bytes Lambda saw."""
        recorded: list = []
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/main.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False}), "main.py": "x\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "qa_version_ids": {
                    "manifest.json": "V-qa-man-pin",
                    "main.py": "V-qa-main-pin",
                },
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        man_call = next(c for c in recorded if c[0].endswith("/qa/manifest.json"))
        main_call = next(c for c in recorded if c[0].endswith("/qa/main.py"))
        assert man_call[1].get("VersionId") == "V-qa-man-pin"
        assert main_call[1].get("VersionId") == "V-qa-main-pin"

    def test_unpinned_qa_omits_version_id_to_s3(self):
        """Without a pin, the broker defaults to 'latest' (no VersionId in
        the GetObject kwargs) — identical to the attachment fall-through."""
        recorded: list = []
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/main.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False}), "main.py": "x\n"},
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        for key, kwargs in recorded:
            if "/qa/" in key:
                assert "VersionId" not in kwargs


class TestExperimentManifestQaVersionIdValidation:
    @pytest.mark.parametrize("bad_key", [
        "has/slash.py",       # path separator in basename — not safe
        "..",                 # traversal
        ".",                  # current-dir
        "spaces in name.md",  # whitespace not in basename pattern
        "",                   # empty
    ])
    def test_rejects_unsafe_qa_version_id_keys(self, bad_key):
        """qa_version_ids is validated IDENTICALLY to attachment_version_ids:
        the key must be a safe basename (defends the `<id>/qa/<basename>` S3
        key construction against traversal)."""
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "qa_version_ids": {bad_key: "V-pinned"},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422

    def test_rejects_unsafe_qa_version_id_values(self):
        app = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "qa_version_ids": {"main.py": "has space"},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 422

    def test_accepts_manifest_json_basename_and_valid_version_id(self):
        """The qa folder's required file is `manifest.json` — the validator
        must accept that basename (it matches the basename pattern) paired
        with a realistic S3 VersionId."""
        good = "Mxl3K7LqXxL.Rq4yE9P_zN8HtY.Bd2W-"
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": []},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False})},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={
                "experiment_id": "voter_targeting",
                "qa_version_ids": {"manifest.json": good},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200


class TestExperimentManifestQaSafetyGuards:
    def test_qa_key_with_wrong_prefix_skipped_and_emits_drift(self):
        """A qa_keys entry that doesn't start with `<id>/qa/` is index drift —
        the broker must skip it (never fetch an unrelated S3 key) and emit a
        drift metric, mirroring the attachment wrong-prefix guard."""
        index = {
            "experiments": [{
                "id": "voter_targeting",
                "attachment_keys": [],
                "qa_manifest_key": "voter_targeting/qa/manifest.json",
                "qa_keys": [
                    "voter_targeting/qa/legit.py",
                    "other_experiment/qa/cross.py",  # wrong prefix
                ],
            }],
        }
        responder = _make_s3_responder(
            index=index,
            qa_files={"manifest.json": json.dumps({"blocking": False}), "legit.py": "ok\n"},
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200
        # Only the legit qa file came back; the cross-prefix one was skipped.
        assert resp.json()["qa"]["files"] == {"legit.py": "ok\n"}
        drift_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_attachment_index_drift"
        ]
        assert drift_calls, "expected a drift metric for the wrong-prefix qa key"
        # Pin the drift_kind DIMENSION value (like the attachment siblings pin
        # unsafe_basename / non_string_key / wrong_prefix) so the metric can't
        # silently mislabel the cause.
        kinds = {
            d.get("Value")
            for call in drift_calls
            for d in call.args[1]
            if d.get("Name") == "drift_kind"
        }
        assert "qa_wrong_prefix" in kinds

    def test_non_utf8_qa_file_returns_500(self):
        """Binary qa files are unsupported (utf-8 only, mirroring attachments).
        A non-utf-8 qa file surfaces as a loud 500, not a silent corruption."""
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/bad.bin"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={
                "manifest.json": json.dumps({"blocking": False}),
                "bad.bin": b"\xff\xfe\xfd not utf-8",
            },
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 500

    def test_oversize_qa_file_returns_502(self):
        """Per-object size cap applies to qa files (separate 1 MiB budget from
        the 5 MiB attachment cap). An oversize qa file trips the cap with 502,
        mirroring the attachment size-cap guard."""
        oversize = b"a" * (em.MAX_QA_BYTES + 1)
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/huge.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={
                "manifest.json": json.dumps({"blocking": False}),
                "huge.py": oversize,
            },
            qa_content_lengths={"huge.py": em.MAX_QA_BYTES + 1},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 502
        assert "exceeds size cap" in resp.json()["detail"]

    def test_qa_per_object_cap_is_exactly_1_mib(self):
        """Pin the per-object qa cap to its ABSOLUTE value (1 MiB), not just to
        the imported constant. The oversize test above sizes its payload off
        em.MAX_QA_BYTES, so widening the constant would silently keep it green
        (just a bigger payload). This catches a cap-widening regression: a qa
        file one byte over 1 MiB MUST 502, regardless of the constant's value.
        (Mutant M13: cap-widening.)"""
        cap = 1 * 1024 * 1024
        # The constant itself must equal the documented per-object byte budget.
        assert em.MAX_QA_BYTES == cap

        oversize = b"a" * (cap + 1)
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": ["voter_targeting/qa/huge.py"]},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={
                "manifest.json": json.dumps({"blocking": False}),
                "huge.py": oversize,
            },
            qa_content_lengths={"huge.py": cap + 1},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 502
        assert "exceeds size cap" in resp.json()["detail"]

    def test_missing_qa_manifest_returns_404_labeled_qa(self):
        """If qa_manifest_key points at a manifest that's been deleted from
        S3, the 404 detail must label it as a qa object, not leak the S3 key."""
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": []},
        )
        responder = _make_s3_responder(
            index=index,
            qa_files={},
            qa_errors={"manifest.json": _no_such_key("voter_targeting/qa/manifest.json")},
        )
        app = _create_app(s3_get_object=responder)
        client = TestClient(app)

        resp = client.post(
            "/experiment/manifest",
            json={"experiment_id": "voter_targeting"},
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "qa" in detail.lower()
        assert "voter_targeting" not in detail


class TestExperimentManifestQaManifestDedupe:
    """Contract F: qa_keys EXCLUDES manifest.json (it's carried separately as
    qa_manifest_key). But a publisher bug / manual S3 edit could list the qa
    manifest in BOTH qa_manifest_key and qa_keys. The broker must HEAD/fetch it
    exactly ONCE (mirroring the loader's seen-set) and emit an index-drift
    metric for the overlap — never fetch the same object twice or place it in
    both qa.manifest and qa.files."""

    def test_qa_manifest_in_both_keys_fetched_once_and_emits_drift(self):
        recorded: list = []
        index = {
            "experiments": [{
                "id": "voter_targeting",
                "attachment_keys": [],
                "qa_manifest_key": "voter_targeting/qa/manifest.json",
                # Drift: manifest.json also listed in qa_keys (should be excluded).
                "qa_keys": [
                    "voter_targeting/qa/manifest.json",
                    "voter_targeting/qa/main.py",
                ],
            }],
        }
        responder = _make_s3_responder(
            index=index,
            qa_files={
                "manifest.json": json.dumps({"blocking": False}),
                "main.py": "print(1)\n",
            },
            recorded_calls=recorded,
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 200, resp.text
        # The qa manifest object must be fetched exactly ONCE despite appearing
        # in both qa_manifest_key and qa_keys.
        man_gets = [k for k, _ in recorded if k == "voter_targeting/qa/manifest.json"]
        assert man_gets == ["voter_targeting/qa/manifest.json"], (
            f"qa manifest must be fetched exactly once, got {man_gets}"
        )
        qa = resp.json()["qa"]
        # The manifest stays under qa.manifest (decoded), NOT duplicated into files.
        assert qa["manifest"] == {"blocking": False}
        assert "manifest.json" not in qa["files"]
        assert qa["files"] == {"main.py": "print(1)\n"}
        # A drift metric fires for the overlap.
        drift_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_attachment_index_drift"
        ]
        assert drift_calls, "expected a drift metric for the qa-manifest overlap"

    def test_non_dict_qa_manifest_returns_500_with_decode_metric(self):
        """The decoded qa manifest must be a JSON OBJECT (dict). A bare JSON
        scalar/array (e.g. `[]` or `"x"`) is malformed — the engine expects
        an object with a `blocking` field. Surface a loud 500 reusing the
        existing qa-manifest decode metric, not a silent shape corruption."""
        index = _default_index(
            qa_manifest_key={"voter_targeting": "voter_targeting/qa/manifest.json"},
            qa_keys={"voter_targeting": []},
        )
        responder = _make_s3_responder(
            index=index,
            # Valid JSON, but a list — NOT a JSON object.
            qa_files={"manifest.json": json.dumps([1, 2, 3])},
        )
        app = _create_app(s3_get_object=responder)
        with patch("broker.endpoints.experiment_manifest._emit_metric") as mock_metric:
            client = TestClient(app)
            resp = client.post(
                "/experiment/manifest",
                json={"experiment_id": "voter_targeting"},
                headers={"X-Broker-Token": BROKER_TOKEN},
            )

        assert resp.status_code == 500
        assert "qa manifest" in resp.json()["detail"].lower()
        decode_calls = [
            call for call in mock_metric.call_args_list
            if call.args[0] == "broker_qa_manifest_decode_error"
        ]
        assert decode_calls, "expected the qa-manifest decode metric to fire on a non-dict manifest"
