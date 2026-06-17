"""Behavioral tests for runner/manifest_loader.py.

Uses a fake httpx transport (httpx.MockTransport) so we exercise the real
client / response parsing without hitting any network or boto3.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pmf_engine.runner.manifest_loader import (
    ManifestLoadError,
    load_from_broker,
)

BROKER_URL = "https://broker-dev.test"
BROKER_TOKEN = "broker-token-test-123"


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(
        base_url=BROKER_URL,
        headers={"X-Broker-Token": BROKER_TOKEN},
        transport=httpx.MockTransport(handler),
    )


def _good_envelope(experiment_id: str = "smoke_test") -> dict:
    return {
        "manifest": {
            "id": experiment_id,
            "version": 1,
            "mode": "win",
            "model": "sonnet",
            "max_turns": 50,
            "input_schema": {
                "type": "object",
                "required": ["state"],
                "properties": {"state": {"type": "string"}},
            },
            "output_schema": {"type": "object"},
        },
        "instruction": "# Smoke Test\n\nstep 1: query Databricks",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestManifestLoaderSuccess:
    def test_returns_envelope_for_valid_response(self):
        envelope = _good_envelope()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/experiment/manifest"
            assert request.headers["x-broker-token"] == BROKER_TOKEN
            payload = json.loads(request.content)
            assert payload == {"experiment_id": "smoke_test"}
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )

        assert result["manifest"] == envelope["manifest"]
        assert result["instruction"] == envelope["instruction"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestManifestLoaderInputValidation:
    @pytest.mark.parametrize(
        "args,expected_msg_fragment",
        [
            (("", BROKER_URL, BROKER_TOKEN), "experiment_id required"),
            (("smoke_test", "", BROKER_TOKEN), "broker_url required"),
            (("smoke_test", BROKER_URL, ""), "broker_token required"),
        ],
    )
    def test_rejects_empty_required_args(self, args, expected_msg_fragment):
        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker(*args)
        assert expected_msg_fragment in str(exc.value)


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


class TestManifestLoaderHttpErrors:
    def test_raises_on_403(self):
        def handler(request):
            return httpx.Response(403, json={"detail": "manifest access denied for this run's scope"})

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "403" in str(exc.value)
        assert "denied" in str(exc.value).lower()

    def test_raises_on_404(self):
        def handler(request):
            return httpx.Response(404, json={"detail": "manifest object not found: smoke_test/manifest.json"})

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "404" in str(exc.value)

    def test_raises_on_500(self):
        def handler(request):
            return httpx.Response(500, json={"detail": "manifest store unavailable"})

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "500" in str(exc.value)

    def test_raises_on_transport_error(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "transport error" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Envelope shape validation
# ---------------------------------------------------------------------------


class TestManifestLoaderEnvelopeValidation:
    def test_raises_when_manifest_missing(self):
        def handler(request):
            return httpx.Response(200, json={"instruction": "x"})

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "manifest" in str(exc.value).lower()

    def test_raises_when_instruction_missing(self):
        envelope = _good_envelope()
        envelope.pop("instruction")

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "instruction" in str(exc.value).lower()

    def test_raises_when_instruction_is_blank(self):
        envelope = _good_envelope()
        envelope["instruction"] = "   \n  "

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError):
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))

    @pytest.mark.parametrize("missing", ["output_schema", "model", "max_turns"])
    def test_raises_when_runner_critical_field_missing(self, missing):
        envelope = _good_envelope()
        envelope["manifest"].pop(missing)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert missing in str(exc.value)

    def test_raises_when_output_schema_missing(self):
        envelope = _good_envelope()
        envelope["manifest"].pop("output_schema")

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "output_schema" in str(exc.value).lower()

    def test_raises_on_non_json_body(self):
        def handler(request):
            return httpx.Response(200, content=b"not json")

        with pytest.raises(ManifestLoadError) as exc:
            load_from_broker("smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler))
        assert "non-json" in str(exc.value).lower() or "json" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# attachment_version_ids forwarding into POST body + envelope acceptance
#
# The whole point of attachment VersionId pinning is end-to-end: dispatch
# captures, runner forwards into POST /experiment/manifest, broker resolves
# bytes. If load_from_broker drops the dict, the broker silently falls
# through to "latest" and the publish-during-run race re-opens.
# ---------------------------------------------------------------------------


class TestManifestLoaderAttachmentVersionIds:
    def test_attachment_version_ids_included_in_broker_request_body(self):
        """When the caller passes attachment_version_ids, it MUST appear in the
        POST body so the broker can pin its S3 GetObject calls."""
        envelope = _good_envelope()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=envelope)

        pin = {"lookup.csv": "Vlk1", "notes.md": "Vnt2"}
        load_from_broker(
            "smoke_test",
            BROKER_URL,
            BROKER_TOKEN,
            attachment_version_ids=pin,
            client=_client_returning(handler),
        )

        assert captured["body"]["attachment_version_ids"] == pin

    def test_attachment_version_ids_omitted_when_none(self):
        """Calling with attachment_version_ids=None (default) must NOT add
        the key to the request body — that would force older brokers to
        reject as 'unexpected field'."""
        envelope = _good_envelope()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=envelope)

        load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )

        assert "attachment_version_ids" not in captured["body"]

    def test_envelope_parses_resolved_attachment_version_ids(self):
        """Brokers that pinned attachments echo the resolved VersionIds back
        in the envelope so the runner can log them as an audit trail.
        load_from_broker must surface this dict on the returned envelope."""
        envelope = _good_envelope()
        envelope["attachments"] = {"lookup.csv": "k,v\n"}
        envelope["resolved_attachment_version_ids"] = {"lookup.csv": "Vlk_resolved"}

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert result["resolved_attachment_version_ids"] == {"lookup.csv": "Vlk_resolved"}

    def test_envelope_absent_attachments_key_logs_info(self, caplog):
        """If the broker response omits the `attachments` key entirely (an
        older broker not yet redeployed) the runner logs INFO so operators
        can distinguish 'old broker' from 'no attachments published'. Empty
        attachments dict (key present, value {}) must NOT trigger the log —
        that's the 'no attachments' case."""
        envelope = _good_envelope()
        assert "attachments" not in envelope

        def handler(request):
            return httpx.Response(200, json=envelope)

        import logging
        with caplog.at_level(logging.INFO, logger="pmf_engine.runner.manifest_loader"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

        messages = [r.message for r in caplog.records if r.name == "pmf_engine.runner.manifest_loader"]
        assert any("attachments" in m.lower() and "older broker" in m.lower() for m in messages), (
            f"expected an INFO log mentioning attachments + older broker; got {messages!r}"
        )

    def test_envelope_empty_attachments_dict_does_not_log_older_broker(self, caplog):
        envelope = _good_envelope()
        envelope["attachments"] = {}

        def handler(request):
            return httpx.Response(200, json=envelope)

        import logging
        with caplog.at_level(logging.INFO, logger="pmf_engine.runner.manifest_loader"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

        messages = [r.message for r in caplog.records if r.name == "pmf_engine.runner.manifest_loader"]
        assert not any("older broker" in m.lower() for m in messages), (
            "empty attachments dict must NOT log 'older broker' — that's a "
            "different operator signal (no attachments published) vs (broker too old to ship them)"
        )

    def test_envelope_rejects_non_dict_resolved_attachment_version_ids(self):
        """Defense-in-depth: if the broker returns a malformed value for
        resolved_attachment_version_ids (e.g. a list), reject loudly rather
        than carrying garbage into the runner's audit trail."""
        envelope = _good_envelope()
        envelope["resolved_attachment_version_ids"] = ["not", "a", "dict"]

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )


# ---------------------------------------------------------------------------
# Write-action field validation (ENG-10234, runner-side mirror of
# pmf_engine.control_plane.manifest_loader._validate_write_action_fields).
#
# Each test sends a single malformed write-action field through the real
# load_from_broker + httpx.MockTransport path so the validator runs against
# the actual envelope-parse code, not in isolation. If a future refactor
# drops any of these branches, one of these tests must fail.
# ---------------------------------------------------------------------------


def _envelope_with_write_action(**overrides) -> dict:
    """Build a baseline-valid envelope with the three write-action fields,
    then apply overrides. Use `_MISSING` sentinel to delete a key entirely
    (vs setting it to None / empty)."""
    envelope = _good_envelope()
    envelope["manifest"]["system_prompt"] = "You are a compliance setup agent."
    envelope["manifest"]["permission_mode"] = "default"
    envelope["manifest"]["allowed_external_tools"] = ["Read"]
    for key, value in overrides.items():
        if value is _MISSING:
            envelope["manifest"].pop(key, None)
        else:
            envelope["manifest"][key] = value
    return envelope


_MISSING = object()


class TestRunnerWriteActionValidation:
    """Mirrors test_lambda_manifest_loader.py's parametrized coverage of
    `_validate_write_action_fields`. The runner-side validator is the last
    line of defense for hand-edited manifests in local-dev runs that bypass
    dispatch; without these tests, a refactor that drops a branch would land
    silently."""

    def test_accepts_all_three_fields_well_formed(self):
        envelope = _envelope_with_write_action()

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert result["manifest"]["system_prompt"] == "You are a compliance setup agent."
        assert result["manifest"]["permission_mode"] == "default"
        assert result["manifest"]["allowed_external_tools"] == ["Read"]

    def test_accepts_when_all_write_action_fields_absent(self):
        """Legacy read-action manifest (no write-action fields) must still
        load — the runner's existing 1.6k tests rely on this."""
        envelope = _envelope_with_write_action(
            system_prompt=_MISSING,
            permission_mode=_MISSING,
            allowed_external_tools=_MISSING,
        )

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert "system_prompt" not in result["manifest"]
        assert "permission_mode" not in result["manifest"]
        assert "allowed_external_tools" not in result["manifest"]

    @pytest.mark.parametrize(
        "permission_mode",
        ["acceptEdits", "plan", "hax", "", "BypassPermissions"],
    )
    def test_rejects_unknown_permission_mode(self, permission_mode):
        """Only `default` and `bypassPermissions` are allowlisted on both
        sides. `acceptEdits` and `plan` are intentionally excluded until the
        harness is audited for them; case-sensitive."""
        envelope = _envelope_with_write_action(permission_mode=permission_mode)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="permission_mode"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_rejects_non_string_permission_mode(self):
        envelope = _envelope_with_write_action(permission_mode=42)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="permission_mode"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_accepts_bypass_permissions_mode(self):
        envelope = _envelope_with_write_action(permission_mode="bypassPermissions")

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert result["manifest"]["permission_mode"] == "bypassPermissions"

    def test_accepts_valid_runtime_max_parallel_subagents(self):
        """runtime.max_parallel_subagents is the fan-out opt-in. A well-formed
        non-negative int loads."""
        envelope = _envelope_with_write_action(runtime={"max_parallel_subagents": 4})

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert result["manifest"]["runtime"]["max_parallel_subagents"] == 4

    def test_accepts_when_runtime_absent(self):
        """No runtime block ⇒ fan-out off; legacy manifests load unchanged."""
        envelope = _envelope_with_write_action(runtime=_MISSING)

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert "runtime" not in result["manifest"]

    @pytest.mark.parametrize("bad_value", ["4", 3.14, True, -1, [4], {"n": 4}])
    def test_rejects_bad_max_parallel_subagents(self, bad_value):
        """Must be a non-negative int (bool excluded). Anything else is a
        manifest authoring error and must fail loud at the runner boundary."""
        envelope = _envelope_with_write_action(runtime={"max_parallel_subagents": bad_value})

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="max_parallel_subagents"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_rejects_non_dict_runtime(self):
        envelope = _envelope_with_write_action(runtime=42)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="runtime"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    @pytest.mark.parametrize("bad_value", [42, 3.14, ["a", "list"], {"a": "dict"}, True])
    def test_rejects_non_string_system_prompt(self, bad_value):
        envelope = _envelope_with_write_action(system_prompt=bad_value)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="system_prompt"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    @pytest.mark.parametrize("empty_value", ["", "   ", "\n\t"])
    def test_rejects_empty_system_prompt(self, empty_value):
        """Whitespace-only is treated as empty — matches dispatch-side rule."""
        envelope = _envelope_with_write_action(system_prompt=empty_value)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="system_prompt"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    @pytest.mark.parametrize("bad_value", ["WebFetch", 42, {"tool": "WebFetch"}])
    def test_rejects_non_list_allowed_external_tools(self, bad_value):
        envelope = _envelope_with_write_action(allowed_external_tools=bad_value)

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="allowed_external_tools"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    @pytest.mark.parametrize(
        "bad_entry",
        [None, 42, ["nested"], "", "   "],
    )
    def test_rejects_invalid_external_tool_entry(self, bad_entry):
        envelope = _envelope_with_write_action(
            allowed_external_tools=["Read", bad_entry],
        )

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="allowed_external_tools"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_empty_external_tools_list_is_valid(self):
        """An empty list explicitly denies external tools — distinct from
        the field being absent (runner default ALLOWED_TOOLS applies)."""
        envelope = _envelope_with_write_action(allowed_external_tools=[])

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )
        assert result["manifest"]["allowed_external_tools"] == []

    def test_permission_mode_validated_in_isolation(self):
        """A malformed permission_mode is rejected even when it's the only
        write-action field on the manifest (no system_prompt /
        allowed_external_tools present)."""
        envelope = _envelope_with_write_action(
            system_prompt=_MISSING,
            allowed_external_tools=_MISSING,
            permission_mode="hax",
        )

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="permission_mode"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )


# ---------------------------------------------------------------------------
# qa_version_ids forwarding + qa envelope parsing (PMF QA gate, contracts G/H).
#
# The qa folder is served under a SEPARATE broker envelope key from attachments
# and never written to /workspace — the runner holds it in memory and hands it
# to the gate engine. The runner forwards QA_VERSION_IDS into the POST body
# (mirroring attachment_version_ids) so the broker pins the qa S3 fetch.
# ---------------------------------------------------------------------------


class TestManifestLoaderQaVersionIds:
    def test_qa_version_ids_included_in_broker_request_body(self):
        """When the caller passes qa_version_ids, it MUST appear in the POST
        body so the broker pins its qa S3 GetObject calls (contract H)."""
        envelope = _good_envelope()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=envelope)

        pin = {"manifest.json": "Vqa1", "eval.md": "Vqa2"}
        load_from_broker(
            "smoke_test",
            BROKER_URL,
            BROKER_TOKEN,
            qa_version_ids=pin,
            client=_client_returning(handler),
        )

        assert captured["body"]["qa_version_ids"] == pin

    def test_qa_version_ids_omitted_when_none(self):
        """qa_version_ids=None (default, unversioned dev/local bucket) must NOT
        add the key to the request body — mirrors attachment_version_ids."""
        envelope = _good_envelope()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=envelope)

        load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )

        assert "qa_version_ids" not in captured["body"]


class TestManifestLoaderQaEnvelope:
    def test_envelope_parses_qa_block(self):
        """The optional `qa` envelope key (contract H) carries the qa manifest,
        the qa entrypoint file bodies, and the resolved VersionIds. The loader
        surfaces it verbatim on the returned envelope; it is NEVER written to
        disk here (the gate's private dir is the only place these bytes land)."""
        envelope = _good_envelope()
        envelope["qa"] = {
            "manifest": {"blocking": False},
            "files": {
                "main.py": "import sys\nprint('[]')\n",
                "eval.md": "# Evaluate faithfulness\n",
            },
            "resolved_qa_version_ids": {
                "manifest.json": "Vm1",
                "main.py": "Vp1",
                "eval.md": "Ve1",
            },
        }

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )

        assert result["qa"] == {
            "manifest": {"blocking": False},
            "files": {
                "main.py": "import sys\nprint('[]')\n",
                "eval.md": "# Evaluate faithfulness\n",
            },
            "resolved_qa_version_ids": {
                "manifest.json": "Vm1",
                "main.py": "Vp1",
                "eval.md": "Ve1",
            },
        }

    def test_envelope_omits_qa_key_when_absent(self):
        """No qa folder published (or older broker): the `qa` key is absent
        from the result so the runner takes the byte-identical no-qa path."""
        envelope = _good_envelope()
        assert "qa" not in envelope

        def handler(request):
            return httpx.Response(200, json=envelope)

        result = load_from_broker(
            "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
        )

        assert "qa" not in result

    def test_envelope_rejects_non_dict_qa(self):
        """A malformed `qa` value (e.g. a list) must reject loudly rather than
        carrying garbage into the gate engine."""
        envelope = _good_envelope()
        envelope["qa"] = ["not", "a", "dict"]

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="qa"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_envelope_rejects_non_dict_qa_manifest(self):
        envelope = _good_envelope()
        envelope["qa"] = {"manifest": "not-a-dict", "files": {}, "resolved_qa_version_ids": {}}

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="qa.manifest"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_envelope_rejects_non_string_qa_file_body(self):
        envelope = _good_envelope()
        envelope["qa"] = {
            "manifest": {"blocking": False},
            "files": {"main.py": 123},
            "resolved_qa_version_ids": {},
        }

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="qa.files"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )

    def test_envelope_rejects_non_string_qa_version_id(self):
        envelope = _good_envelope()
        envelope["qa"] = {
            "manifest": {"blocking": False},
            "files": {"main.py": "print('[]')\n"},
            "resolved_qa_version_ids": {"main.py": 5},
        }

        def handler(request):
            return httpx.Response(200, json=envelope)

        with pytest.raises(ManifestLoadError, match="resolved_qa_version_ids"):
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, client=_client_returning(handler)
            )


def test_permission_mode_values_parity_with_control_plane():
    """The runner-side `_PERMISSION_MODE_VALUES` is a deliberate copy of the
    control-plane set (kept inline to avoid a control_plane → runner import
    in the runner Docker image). This parity test catches drift the moment
    a new mode is added on one side but not the other: without it, a future
    addition like `acceptEdits` to control-plane would dispatch correctly
    but every experiment using it would hard-fail in the runner with
    `ManifestLoadError`.
    """
    from pmf_engine.control_plane.manifest_loader import (
        _PERMISSION_MODE_VALUES as cp_vals,
    )
    from pmf_engine.runner.manifest_loader import (
        _PERMISSION_MODE_VALUES as runner_vals,
    )

    assert runner_vals == cp_vals, (
        f"runner and control_plane _PERMISSION_MODE_VALUES diverged: "
        f"runner={sorted(runner_vals)!r} cp={sorted(cp_vals)!r}. "
        f"Update whichever side is missing the value; both must allowlist "
        f"the same set (the harness allowlists are reviewed alongside the "
        f"validator rules)."
    )


# ---------------------------------------------------------------------------
# B6 (MEDIUM dup + LOW position):
#   - _require_str_str_map(raw, label): one validator for both
#     resolved_attachment_version_ids and qa.resolved_qa_version_ids.
#   - client / timeout_seconds are keyword-only so adding qa_version_ids (or
#     any future param) can never shift an existing positional arg.
# ---------------------------------------------------------------------------


class TestRequireStrStrMap:
    def _validate(self, raw, label):
        from pmf_engine.runner.manifest_loader import _require_str_str_map

        return _require_str_str_map(raw, label)

    def test_accepts_str_str_map_and_returns_it(self):
        out = self._validate({"a.md": "V1", "b.csv": "V2"}, "envelope.x")
        assert out == {"a.md": "V1", "b.csv": "V2"}

    def test_rejects_non_dict_with_label_in_message(self):
        with pytest.raises(ManifestLoadError) as exc:
            self._validate(["not", "a", "dict"], "envelope.resolved_attachment_version_ids")
        msg = str(exc.value)
        assert "envelope.resolved_attachment_version_ids" in msg
        assert "must be an object" in msg

    def test_rejects_non_string_value_with_label_and_key(self):
        with pytest.raises(ManifestLoadError) as exc:
            self._validate({"main.py": 5}, "envelope.qa.resolved_qa_version_ids")
        msg = str(exc.value)
        assert "envelope.qa.resolved_qa_version_ids" in msg
        assert "main.py" in msg
        assert "string" in msg

    def test_rejects_non_string_key(self):
        with pytest.raises(ManifestLoadError):
            self._validate({5: "V1"}, "envelope.x")


class TestLoadFromBrokerKeywordOnlyTail:
    """client / timeout_seconds must be keyword-only — passing them positionally
    raises TypeError, which is the mechanical guard that a future param insert
    (e.g. qa_version_ids) can never silently shift them."""

    def test_client_is_keyword_only(self):
        import inspect

        sig = inspect.signature(load_from_broker)
        client_param = sig.parameters["client"]
        timeout_param = sig.parameters["timeout_seconds"]
        assert client_param.kind is inspect.Parameter.KEYWORD_ONLY, (
            "client must be keyword-only so positional args can't shift onto it"
        )
        assert timeout_param.kind is inspect.Parameter.KEYWORD_ONLY

    def test_passing_client_positionally_raises_type_error(self):
        # 4 leading positionals are fine (experiment_id, broker_url,
        # broker_token, manifest_version_id), but the client/timeout tail can't
        # be reached positionally.
        def handler(request):
            return httpx.Response(200, json=_good_envelope())

        client = _client_returning(handler)
        with pytest.raises(TypeError):
            # Attempt to pass too many positionals — would land on a
            # keyword-only slot.
            load_from_broker(
                "smoke_test", BROKER_URL, BROKER_TOKEN, None, None, None, None, 30.0, client
            )
