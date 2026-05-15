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
