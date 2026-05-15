"""Behavioral tests for control_plane/manifest_loader.py.

The dispatch Lambda fetches index.json from the metadata bucket on each warm
invocation (with /tmp TTL cache), then derives per-experiment routing fields.
This replaces the bundled DISPATCH_REGISTRY at runtime.
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from pmf_engine.control_plane.manifest_loader import (
    ManifestLoaderError,
    ManifestLoaderMalformedError,
    ManifestLoaderTransientError,
    ManifestRoutingLoader,
)

BUCKET = "agent-experiment-metadata-dev"


def _index_payload(experiments: list[dict]) -> dict:
    return {
        "published_at": "2026-04-30T00:00:00Z",
        "git_sha": "abc1234",
        "experiments": experiments,
    }


def _manifest_payload(experiment_id: str, **overrides) -> dict:
    base = {
        "id": experiment_id,
        "version": 1,
        "mode": "win",
        "model": "sonnet",
        "max_turns": 50,
        "timeout_seconds": 900,
        "input_schema": {
            "type": "object",
            "required": ["state", "city"],
            "properties": {
                "state": {"type": "string"},
                "city": {"type": "string"},
            },
        },
        "output_schema": {"type": "object"},
    }
    base.update(overrides)
    return base


class FakeS3:
    """Behavioral fake for the S3 client subset ManifestRoutingLoader uses.

    Records every call (op, bucket, key) so tests can assert *behavior*
    (was the manifest re-fetched? was head_object called?) without coupling
    to MagicMock internals like ``call_args_list``. Per-key responses can
    be set to either a body (with optional VersionId) or an error.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str, str]] = []

    # ---------- setup helpers ----------

    def set_object(self, bucket: str, key: str, body: bytes, version_id: str | None = "v1") -> None:
        self.objects[(bucket, key)] = {"body": body, "version_id": version_id, "error": None}

    def set_json(self, bucket: str, key: str, payload: dict, version_id: str | None = "v1") -> None:
        self.set_object(bucket, key, json.dumps(payload).encode(), version_id=version_id)

    def set_error(self, bucket: str, key: str, code: str, message: str = "", op: str = "GetObject") -> None:
        self.objects[(bucket, key)] = {
            "body": None,
            "version_id": None,
            "error": ClientError(
                {
                    "Error": {"Code": code, "Message": message},
                    "ResponseMetadata": {"RequestId": "fake-req-id"},
                },
                op,
            ),
        }

    # ---------- boto-like surface ----------

    def get_object(self, *, Bucket, Key, **_kw):
        self.calls.append(("get_object", Bucket, Key))
        entry = self.objects.get((Bucket, Key))
        if entry is None:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "Not found"},
                    "ResponseMetadata": {"RequestId": "fake-req-id"},
                },
                "GetObject",
            )
        if entry.get("error"):
            raise entry["error"]
        body = entry["body"]
        return {
            "Body": BytesIO(body),
            "VersionId": entry["version_id"],
            "ContentLength": len(body),
        }

    def head_object(self, *, Bucket, Key, **_kw):
        self.calls.append(("head_object", Bucket, Key))
        entry = self.objects.get((Bucket, Key))
        if entry is None:
            # boto3 head_object returns code "404" (HTTP status) on missing
            # keys, NOT "NoSuchKey" (which only appears in get_object's parsed
            # XML body). Faking this correctly is load-bearing — get it wrong
            # and tests pass against impossible-in-prod codes.
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"RequestId": "fake-req-id", "HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        if entry.get("error"):
            raise entry["error"]
        return {
            "VersionId": entry["version_id"],
            "ContentLength": len(entry["body"]) if entry["body"] is not None else 0,
        }

    # ---------- inspection helpers ----------

    @property
    def get_count(self) -> int:
        return sum(1 for c in self.calls if c[0] == "get_object")

    @property
    def head_count(self) -> int:
        return sum(1 for c in self.calls if c[0] == "head_object")

    def calls_for_key(self, key: str) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[2] == key]


def _fake_s3_with_index(index_payload: dict) -> FakeS3:
    fake = FakeS3()
    fake.set_json(BUCKET, "index.json", index_payload)
    return fake


# ---------------------------------------------------------------------------
# Happy path — index lookup + manifest fetch
# ---------------------------------------------------------------------------


class TestRoutingForSuccess:
    def test_returns_routing_fields_from_manifest(self):
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        manifest = _manifest_payload("smoke_test", timeout_seconds=900, model="sonnet")
        s3 = _fake_s3_with_index(index)
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["model"] == "sonnet"
        assert routing["timeout_seconds"] == 900
        # input_schema replaces required_params — Lambda projects whatever the
        # manifest declares so dispatch_handler can validate against it.
        assert routing["input_schema"] == manifest["input_schema"]

    def test_returns_none_when_experiment_not_in_index(self):
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        s3 = _fake_s3_with_index(index)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        assert loader.routing_for("nonexistent_experiment") is None

    def test_lists_all_known_experiments(self):
        """Every entry in index.json is dispatchable in the env it lives in.
        Per-experiment env gating was dropped — env scoping happens at the
        S3 bucket level (each env has its own metadata bucket)."""
        index = _index_payload(
            [
                {"id": "smoke_a", "version": 1, "mode": "win", "manifest_key": "smoke_a/manifest.json"},
                {"id": "smoke_b", "version": 1, "mode": "win", "manifest_key": "smoke_b/manifest.json"},
                {"id": "smoke_c", "version": 1, "mode": "serve", "manifest_key": "smoke_c/manifest.json"},
            ]
        )
        s3 = _fake_s3_with_index(index)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        assert sorted(loader.known_experiments()) == ["smoke_a", "smoke_b", "smoke_c"]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestRoutingForCaching:
    def test_index_fetched_once_within_ttl(self):
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        manifest = _manifest_payload("smoke_test")
        s3 = _fake_s3_with_index(index)
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3, ttl_seconds=60)

        loader.routing_for("smoke_test")
        loader.routing_for("smoke_test")
        loader.routing_for("smoke_test")

        index_calls = s3.calls_for_key("index.json")
        assert len(index_calls) == 1, f"expected index.json fetched once, got {len(index_calls)}"

    def test_manifest_cached_per_experiment(self):
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        manifest = _manifest_payload("smoke_test")
        s3 = _fake_s3_with_index(index)
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3, ttl_seconds=60)

        loader.routing_for("smoke_test")
        loader.routing_for("smoke_test")

        manifest_calls = s3.calls_for_key("smoke_test/manifest.json")
        assert len(manifest_calls) == 1

    def test_cache_expires_after_ttl(self, monkeypatch):
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        manifest = _manifest_payload("smoke_test")
        s3 = _fake_s3_with_index(index)
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)

        clock = [1000.0]
        monkeypatch.setattr("pmf_engine.control_plane.manifest_loader.time.monotonic", lambda: clock[0])

        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3, ttl_seconds=60)
        loader.routing_for("smoke_test")
        clock[0] += 61
        loader.routing_for("smoke_test")

        index_calls = s3.calls_for_key("index.json")
        assert len(index_calls) == 2


# ---------------------------------------------------------------------------
# Error handling
#
# The dispatch handler maps Transient vs Malformed to different SQS retry
# behavior + operator alarm severity. Tests assert on the SUBCLASS, not just
# the parent ManifestLoaderError, so a regression that drops or swaps the
# subclass surfaces here.
# ---------------------------------------------------------------------------


class TestRoutingForErrors:
    def test_index_missing_raises_malformed(self):
        """NoSuchKey on index.json is a publish-pipeline failure, not a
        transient S3 issue — the bucket exists, the file does not."""
        s3 = FakeS3()
        s3.set_error(BUCKET, "index.json", code="NoSuchKey")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderMalformedError), (
            f"NoSuchKey on index.json must raise Malformed (publish-pipeline bug), got {type(exc.value).__name__}"
        )
        assert "index" in str(exc.value).lower()

    def test_index_throttled_raises_transient(self):
        """SlowDown / 5xx on index.json must raise Transient so SQS retries
        instead of poisoning the queue with a permanent failure."""
        s3 = FakeS3()
        s3.set_error(BUCKET, "index.json", code="SlowDown")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderTransientError), (
            f"SlowDown on index.json must raise Transient (SQS will retry), got {type(exc.value).__name__}"
        )

    def test_manifest_missing_for_known_experiment_raises_malformed(self):
        """The index references a manifest that doesn't exist in S3 — this
        is a publish-pipeline bug: index.json was updated but the manifest
        upload failed or was deleted."""
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        s3 = _fake_s3_with_index(index)
        s3.set_error(BUCKET, "smoke_test/manifest.json", code="NoSuchKey")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderMalformedError), (
            f"NoSuchKey on a manifest the index references must raise Malformed, got {type(exc.value).__name__}"
        )
        assert "smoke_test" in str(exc.value).lower()

    def test_manifest_corrupt_raises_malformed(self):
        index = _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )
        s3 = _fake_s3_with_index(index)
        s3.set_object(BUCKET, "smoke_test/manifest.json", b"not json")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderMalformedError), (
            f"Corrupt JSON in manifest must raise Malformed, got {type(exc.value).__name__}"
        )

    def test_index_corrupt_json_raises_malformed(self):
        s3 = FakeS3()
        s3.set_object(BUCKET, "index.json", b"{not valid json")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderMalformedError):
            loader.routing_for("smoke_test")

    def test_index_missing_experiments_array_raises_malformed(self):
        s3 = FakeS3()
        s3.set_json(BUCKET, "index.json", {"published_at": "x", "git_sha": "y"})
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderMalformedError) as exc:
            loader.routing_for("smoke_test")

        assert "experiments" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Instruction VersionId pinning
#
# The whole point of capturing the instruction object's VersionId at dispatch
# time is to pin the runner's broker fetch to the same bytes Lambda routed
# against, closing the publish-during-run race. If HEAD fails for any reason
# OTHER than NoSuchKey (AccessDenied, throttling, transient 5xx) and the
# loader silently returns None, the runner reads "latest" — defeating the
# entire pinning system. Asymmetric vs `_fetch_manifest` which raises.
# ---------------------------------------------------------------------------


class TestInstructionVersionIdPinning:
    def _index(self):
        return _index_payload(
            [
                {
                    "id": "smoke_test",
                    "version": 1,
                    "mode": "win",
                    "manifest_key": "smoke_test/manifest.json",
                    "instruction_key": "smoke_test/instruction.md",
                },
            ]
        )

    def _s3_with_manifest(self) -> FakeS3:
        s3 = _fake_s3_with_index(self._index())
        s3.set_json(BUCKET, "smoke_test/manifest.json", _manifest_payload("smoke_test"))
        return s3

    def test_returns_version_id_on_head_success(self):
        """Happy path: HEAD succeeds → instruction_version_id flows through routing."""
        s3 = self._s3_with_manifest()
        s3.set_object(BUCKET, "smoke_test/instruction.md", b"# instr", version_id="instr_v_abc123")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["instruction_version_id"] == "instr_v_abc123", (
            "HEAD success must surface the VersionId — this is the contract the "
            "runner relies on to pin its broker fetch to the same bytes."
        )
        # Behavioral assertion: HEAD was actually called against the instruction key.
        assert s3.calls_for_key("smoke_test/instruction.md") == [
            ("head_object", BUCKET, "smoke_test/instruction.md"),
        ]

    def test_returns_none_when_instruction_genuinely_absent(self):
        """A missing instruction.md is the ONE legitimate case to swallow:
        instruction file was never published. Returning None lets dispatch
        proceed (and the runner will fail at fetch time with a clear 404 from
        the broker). boto3 head_object returns code '404' (not 'NoSuchKey')
        for missing keys; the loader must accept both spellings."""
        s3 = self._s3_with_manifest()
        s3.set_error(BUCKET, "smoke_test/instruction.md", code="404", op="HeadObject")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["instruction_version_id"] is None

    def test_raises_transient_on_access_denied(self):
        """AccessDenied means IAM drift — the role lost s3:GetObjectVersion or
        the bucket policy changed. NOT 'instruction missing'. Must raise the
        Transient subclass so SQS retries (giving the IAM fix a chance) and
        the operator alarm fires on the IAM regression."""
        s3 = self._s3_with_manifest()
        s3.set_error(BUCKET, "smoke_test/instruction.md", code="AccessDenied", op="HeadObject")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderTransientError), (
            f"AccessDenied on instruction HEAD must raise Transient, got {type(exc.value).__name__}"
        )
        assert "AccessDenied" in str(exc.value) or "access" in str(exc.value).lower()

    def test_raises_transient_on_service_unavailable(self):
        """ServiceUnavailable / SlowDown / 5xx are transient. They must raise
        Transient so SQS retries — silently proceeding without the version pin
        defeats the entire pinning system the loader was built to provide."""
        s3 = self._s3_with_manifest()
        s3.set_error(BUCKET, "smoke_test/instruction.md", code="ServiceUnavailable", op="HeadObject")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderTransientError), (
            f"ServiceUnavailable on instruction HEAD must raise Transient, got {type(exc.value).__name__}"
        )
        assert "serviceunavailable" in str(exc.value).lower() or "service" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Attachment VersionId pinning
#
# Index entries can declare `attachment_keys: list[str]` pointing at sidecar
# objects in S3 (e.g. lookup CSVs, reference markdown). To close the publish-
# during-run race the same way we do for manifest+instruction, Lambda must
# HEAD each attachment key, capture VersionIds, and forward them via the
# routing dict so the runner can pin its broker fetch.
#
# Symmetric error contract to _fetch_instruction_version_id: a 404 on a
# single attachment is benign (publish-pipeline bug, runner will discover it
# when broker fetch fails) and that basename is omitted from the dict; any
# other ClientError raises ManifestLoaderTransientError so SQS retries and
# we never silently fall through to "latest" for the published sidecars.
# ---------------------------------------------------------------------------


class TestAttachmentVersionIdPinning:
    def _index_with_attachments(self, attachment_keys: list[str]):
        return _index_payload(
            [
                {
                    "id": "smoke_test",
                    "version": 1,
                    "mode": "win",
                    "manifest_key": "smoke_test/manifest.json",
                    "instruction_key": "smoke_test/instruction.md",
                    "attachment_keys": attachment_keys,
                },
            ]
        )

    def _s3_with_manifest_and_instruction(self, attachment_keys: list[str]) -> FakeS3:
        s3 = _fake_s3_with_index(self._index_with_attachments(attachment_keys))
        s3.set_json(BUCKET, "smoke_test/manifest.json", _manifest_payload("smoke_test"))
        # Always make the instruction HEAD succeed so attachment behavior
        # is isolated from instruction-side variance.
        s3.set_object(BUCKET, "smoke_test/instruction.md", b"# instr", version_id="instr_v")
        return s3

    def test_routing_includes_attachment_version_ids_from_head(self):
        """Happy path: each attachment_key gets HEADed; the resulting
        VersionIds appear in routing['attachment_version_ids'] keyed by basename."""
        keys = ["smoke_test/attachments/lookup.csv", "smoke_test/attachments/notes.md"]
        s3 = self._s3_with_manifest_and_instruction(keys)
        s3.set_object(BUCKET, keys[0], b"k,v\n", version_id="lookup_v_abc")
        s3.set_object(BUCKET, keys[1], b"# notes", version_id="notes_v_xyz")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["attachment_version_ids"] == {
            "lookup.csv": "lookup_v_abc",
            "notes.md": "notes_v_xyz",
        }
        # Behavioral: HEAD was actually called against both attachment keys.
        for k in keys:
            assert s3.calls_for_key(k) == [("head_object", BUCKET, k)], (
                f"expected exactly one head_object call for {k}"
            )

    def test_routing_omits_missing_attachment_but_keeps_others(self):
        """A single attachment 404 is benign: that basename is omitted, other
        attachments still get pinned. Mirrors the instruction-absent contract:
        publish-pipeline bug, runner will surface the missing file at fetch
        time. Dispatch must NOT raise."""
        keys = ["smoke_test/attachments/lookup.csv", "smoke_test/attachments/notes.md"]
        s3 = self._s3_with_manifest_and_instruction(keys)
        # lookup.csv exists; notes.md is missing (404).
        s3.set_object(BUCKET, keys[0], b"k,v\n", version_id="lookup_v_abc")
        s3.set_error(BUCKET, keys[1], code="404", op="HeadObject")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["attachment_version_ids"] == {"lookup.csv": "lookup_v_abc"}, (
            "missing attachment must be omitted from the dict; others must still be pinned"
        )

    def test_routing_raises_transient_on_attachment_access_denied(self):
        """AccessDenied on an attachment HEAD means IAM drift — falling back
        to 'latest' would silently defeat the version-pin guarantee. Must raise
        Transient so SQS retries."""
        keys = ["smoke_test/attachments/lookup.csv"]
        s3 = self._s3_with_manifest_and_instruction(keys)
        s3.set_error(BUCKET, keys[0], code="AccessDenied", op="HeadObject")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderTransientError), (
            f"AccessDenied on attachment HEAD must raise Transient, got {type(exc.value).__name__}"
        )

    def test_routing_raises_transient_on_attachment_service_unavailable(self):
        """ServiceUnavailable on an attachment HEAD must raise Transient so
        SQS retries — silently dropping the version pin defeats the system."""
        keys = ["smoke_test/attachments/lookup.csv"]
        s3 = self._s3_with_manifest_and_instruction(keys)
        s3.set_error(BUCKET, keys[0], code="ServiceUnavailable", op="HeadObject")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        with pytest.raises(ManifestLoaderError) as exc:
            loader.routing_for("smoke_test")

        assert isinstance(exc.value, ManifestLoaderTransientError)

    def test_routing_returns_empty_dict_when_no_attachment_keys(self):
        """A manifest without attachment_keys produces an empty dict, not a
        missing key — the contract downstream (dispatch_handler) checks
        truthiness, so empty-vs-missing must not matter."""
        s3 = _fake_s3_with_index(
            _index_payload(
                [
                    {
                        "id": "smoke_test",
                        "version": 1,
                        "mode": "win",
                        "manifest_key": "smoke_test/manifest.json",
                        "instruction_key": "smoke_test/instruction.md",
                    },
                ]
            )
        )
        s3.set_json(BUCKET, "smoke_test/manifest.json", _manifest_payload("smoke_test"))
        s3.set_object(BUCKET, "smoke_test/instruction.md", b"# instr", version_id="iv")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["attachment_version_ids"] == {}

    def test_routing_for_with_attachment_keys_still_returns_other_routing_fields(self):
        """Pin: adding attachment handling must not alter the rest of the
        routing dict shape. model/timeout_seconds/input_schema/scope/
        manifest_version_id/instruction_version_id keep working."""
        keys = ["smoke_test/attachments/lookup.csv"]
        s3 = self._s3_with_manifest_and_instruction(keys)
        s3.set_object(BUCKET, keys[0], b"x", version_id="lv1")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        # Pre-existing fields untouched.
        assert routing["model"] == "sonnet"
        assert routing["timeout_seconds"] == 900
        assert "input_schema" in routing
        assert "scope" in routing
        assert "manifest_version_id" in routing
        assert routing["instruction_version_id"] == "instr_v"
        # New field present.
        assert routing["attachment_version_ids"] == {"lookup.csv": "lv1"}

    def test_routing_skips_attachment_key_with_wrong_prefix(self):
        """Defense-in-depth: an attachment_key not under the experiment's
        own attachments/ prefix is suspicious (publish-pipeline bug or
        manual S3 edit). Skip it with a WARN rather than including it under
        a misleading basename."""
        keys = [
            "smoke_test/attachments/good.md",
            "other_experiment/attachments/leaked.md",  # wrong prefix
        ]
        s3 = self._s3_with_manifest_and_instruction(keys)
        s3.set_object(BUCKET, keys[0], b"ok", version_id="good_v")
        s3.set_object(BUCKET, keys[1], b"leak", version_id="leak_v")
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")

        assert routing["attachment_version_ids"] == {"good.md": "good_v"}, (
            "wrong-prefix attachment key must be skipped, not exposed under a bare basename"
        )


# ---------------------------------------------------------------------------
# Scope validation (defense-in-depth on top of publish-time meta-schema)
# ---------------------------------------------------------------------------


class TestScopeValidation:
    def _index(self):
        return _index_payload(
            [
                {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
            ]
        )

    def test_valid_scope_passes_through(self):
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload(
            "smoke_test",
            scope={
                "allowed_tables": ["goodparty_data_catalog.dbt.synthetic_table"],
                "max_rows": 1000,
            },
        )
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        routing = loader.routing_for("smoke_test")
        assert routing["scope"]["max_rows"] == 1000

    def test_rejects_non_list_allowed_tables(self):
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload("smoke_test", scope={"allowed_tables": "not_a_list"})
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        with pytest.raises(ManifestLoaderMalformedError, match="allowed_tables"):
            loader.routing_for("smoke_test")

    def test_rejects_invalid_table_name_pattern(self):
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload(
            "smoke_test",
            scope={
                "allowed_tables": ["bad table name; DROP"],
            },
        )
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        with pytest.raises(ManifestLoaderMalformedError, match="allowed_tables"):
            loader.routing_for("smoke_test")

    def test_rejects_max_rows_out_of_range(self):
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload(
            "smoke_test",
            scope={
                "allowed_tables": ["goodparty_data_catalog.dbt.t"],
                "max_rows": 10_000_000,
            },
        )
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        with pytest.raises(ManifestLoaderMalformedError, match="max_rows"):
            loader.routing_for("smoke_test")

    def test_rejects_max_rows_zero(self):
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload(
            "smoke_test",
            scope={
                "allowed_tables": ["goodparty_data_catalog.dbt.t"],
                "max_rows": 0,
            },
        )
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        with pytest.raises(ManifestLoaderMalformedError, match="max_rows"):
            loader.routing_for("smoke_test")

    def test_empty_scope_skips_validation(self):
        """Manifests without a scope block (web-only experiments) are valid."""
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload("smoke_test")
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)
        routing = loader.routing_for("smoke_test")
        assert routing["scope"] == {}


# ---------------------------------------------------------------------------
# Write-action experiment fields (compliance_setup-shaped — ENG-10128).
#
# Optional top-level manifest fields that signal a write-action experiment:
#   - system_prompt: str                                  (dispatch-time
#                                                          discriminator)
#   - permission_mode: "default" | "bypassPermissions"    (Claude SDK)
#   - allowed_external_tools: list[str]                   (non-gp-api tools
#                                                          the runner calls
#                                                          directly)
#
# Note: there is no per-experiment gp-api endpoint allowlist. Every
# @McpTool-decorated endpoint on gp-api is exposed to every agent run;
# the broker mints per-call Clerk JWTs from clerk_session_id on the
# ScopeTicket and forwards MCP traffic at /agent/mcp.
#
# Legacy Databricks/web-only manifests do not carry these fields and must
# project unchanged through _project_routing.
# ---------------------------------------------------------------------------


def _write_action_overrides(**extras) -> dict:
    base = {
        "system_prompt": "You are a compliance setup agent.",
        "permission_mode": "default",
        "allowed_external_tools": ["WebFetch"],
    }
    base.update(extras)
    return base


class TestWriteActionFieldProjection:
    def _index(self):
        return _index_payload(
            [
                {
                    "id": "compliance_smoke_test",
                    "version": 1,
                    "mode": "serve",
                    "manifest_key": "compliance_smoke_test/manifest.json",
                },
            ]
        )

    def test_projects_all_write_action_fields_when_present(self):
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload("compliance_smoke_test", **_write_action_overrides())
        s3.set_json(BUCKET, "compliance_smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("compliance_smoke_test")

        assert routing["system_prompt"] == "You are a compliance setup agent."
        assert routing["permission_mode"] == "default"
        assert routing["allowed_external_tools"] == ["WebFetch"]

    def test_projects_only_present_fields(self):
        """A manifest with system_prompt but no permission_mode/external_tools
        projects only the present field — no synthetic defaults."""
        s3 = _fake_s3_with_index(self._index())
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="You are a compliance setup agent.",
        )
        s3.set_json(BUCKET, "compliance_smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("compliance_smoke_test")
        assert routing["system_prompt"] == "You are a compliance setup agent."
        assert "permission_mode" not in routing
        assert "allowed_external_tools" not in routing

    def test_omits_write_action_fields_when_absent(self):
        """Legacy Databricks/web-only manifests must not gain new keys."""
        s3 = _fake_s3_with_index(
            _index_payload(
                [
                    {"id": "smoke_test", "version": 1, "mode": "win", "manifest_key": "smoke_test/manifest.json"},
                ]
            )
        )
        manifest = _manifest_payload("smoke_test")
        s3.set_json(BUCKET, "smoke_test/manifest.json", manifest)
        loader = ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

        routing = loader.routing_for("smoke_test")
        for field in ("system_prompt", "permission_mode", "allowed_external_tools"):
            assert field not in routing, f"unexpected {field} in legacy routing"


class TestWriteActionFieldValidation:
    def _index(self):
        return _index_payload(
            [
                {
                    "id": "compliance_smoke_test",
                    "version": 1,
                    "mode": "serve",
                    "manifest_key": "compliance_smoke_test/manifest.json",
                },
            ]
        )

    def _publish(self, manifest):
        s3 = _fake_s3_with_index(self._index())
        s3.set_json(BUCKET, "compliance_smoke_test/manifest.json", manifest)
        return ManifestRoutingLoader(bucket=BUCKET, s3_client=s3)

    def test_rejects_unknown_permission_mode(self):
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="You are a compliance setup agent.",
            permission_mode="hax",
        )
        with pytest.raises(ManifestLoaderMalformedError, match="permission_mode"):
            self._publish(manifest).routing_for("compliance_smoke_test")

    def test_accepts_bypass_permissions_mode(self):
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="You are a compliance setup agent.",
            permission_mode="bypassPermissions",
        )
        routing = self._publish(manifest).routing_for("compliance_smoke_test")
        assert routing["permission_mode"] == "bypassPermissions"

    def test_rejects_non_string_system_prompt(self):
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt=42,
        )
        with pytest.raises(ManifestLoaderMalformedError, match="system_prompt"):
            self._publish(manifest).routing_for("compliance_smoke_test")

    def test_rejects_empty_system_prompt(self):
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="   ",
        )
        with pytest.raises(ManifestLoaderMalformedError, match="system_prompt"):
            self._publish(manifest).routing_for("compliance_smoke_test")

    def test_rejects_non_list_allowed_external_tools(self):
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="You are a compliance setup agent.",
            allowed_external_tools="WebFetch",
        )
        with pytest.raises(ManifestLoaderMalformedError, match="allowed_external_tools"):
            self._publish(manifest).routing_for("compliance_smoke_test")

    def test_rejects_non_string_external_tool_entry(self):
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="You are a compliance setup agent.",
            allowed_external_tools=["WebFetch", None],
        )
        with pytest.raises(ManifestLoaderMalformedError, match="allowed_external_tools"):
            self._publish(manifest).routing_for("compliance_smoke_test")

    def test_empty_external_tools_list_is_valid(self):
        """An empty list explicitly denies external tools — different from
        the field being absent (which means 'unspecified, runner default')."""
        manifest = _manifest_payload(
            "compliance_smoke_test",
            system_prompt="You are a compliance setup agent.",
            allowed_external_tools=[],
        )
        routing = self._publish(manifest).routing_for("compliance_smoke_test")
        assert routing["allowed_external_tools"] == []

    def test_permission_mode_validated_in_isolation(self):
        """Defense in depth: a malformed permission_mode is rejected even
        when it's the only write-action field on the manifest."""
        manifest = _manifest_payload(
            "compliance_smoke_test",
            permission_mode="hax",
        )
        with pytest.raises(ManifestLoaderMalformedError, match="permission_mode"):
            self._publish(manifest).routing_for("compliance_smoke_test")
