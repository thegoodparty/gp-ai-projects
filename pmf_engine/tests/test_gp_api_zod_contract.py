"""Snapshot contract test: callback_handler output matches gp-api's Zod schema.

`callback_handler.format_gp_api_result_message` produces the SQS envelope forwarded
to gp-api's results queue. gp-api's queue consumer validates it with
`AgentExperimentResultSchema` (Zod). A mismatch between the two silently breaks
the whole experiment pipeline — runs complete in pmf_engine but never update
the DB in gp-api.

This test pins the contract via a checked-in JSON Schema snapshot and verifies:
1. format_gp_api_result_message output validates against the snapshot for every
   status (success, failed, contract_violation).
2. The snapshot's status enum matches the literal `z.enum([...])` in the
   gp-api source file (skipped if gp-api is not checked out).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

os.environ.setdefault("ARTIFACT_BUCKET", "gp-agent-artifacts-dev")

from pmf_engine.control_plane.callback_handler import format_gp_api_result_message  # noqa: E402


_FIXTURE = Path(__file__).parent / "fixtures" / "gp_api_result_schema.json"
_GP_API_SOURCE = Path("/Users/collinpark/work/gp-api/src/queue/queue.types.ts")


def _load_schema() -> dict:
    return json.loads(_FIXTURE.read_text())


def _success_message() -> dict:
    return {
        "experiment_id": "voter_targeting",
        "run_id": "run-123",
        "candidate_id": "cand-456",
        "status": "success",
        "artifact_key": "voter_targeting/run-123/voter_targeting.json",
        "artifact_bucket": "gp-agent-artifacts-dev",
        "duration_seconds": 123.4,
    }


def _failed_message() -> dict:
    return {
        "experiment_id": "district_intel",
        "run_id": "run-777",
        "candidate_id": "cand-2",
        "status": "failed",
        "error": "Harness timed out after 600s",
        "duration_seconds": 600,
    }


def _contract_violation_message() -> dict:
    return {
        "experiment_id": "meeting_briefing",
        "run_id": "run-cv-9",
        "candidate_id": "cand-3",
        "status": "contract_violation",
        "artifact_key": "meeting_briefing/run-cv-9/rejected.json",
        "artifact_bucket": "gp-agent-artifacts-dev",
        "duration_seconds": 42.0,
        "error": "Missing field score.dimensions",
    }


class TestCallbackEnvelopeMatchesSnapshot:
    def test_success_envelope_validates(self):
        schema = _load_schema()
        envelope = format_gp_api_result_message(_success_message())
        Draft202012Validator(schema).validate(envelope)

    def test_failed_envelope_validates(self):
        schema = _load_schema()
        envelope = format_gp_api_result_message(_failed_message())
        Draft202012Validator(schema).validate(envelope)

    def test_contract_violation_envelope_validates(self):
        schema = _load_schema()
        envelope = format_gp_api_result_message(_contract_violation_message())
        Draft202012Validator(schema).validate(envelope)

    def test_envelope_has_required_camelcase_fields(self):
        envelope = format_gp_api_result_message(_success_message())
        assert envelope["type"] == "agentExperimentResult"
        assert envelope["data"]["experimentId"] == "voter_targeting"
        assert envelope["data"]["runId"] == "run-123"
        assert envelope["data"]["candidateId"] == "cand-456"
        assert envelope["data"]["status"] == "success"
        assert envelope["data"]["artifactKey"].endswith("voter_targeting.json")

    def test_schema_rejects_invalid_status(self):
        """If pmf_engine starts emitting a status gp-api doesn't know, the
        JSON Schema validator must reject it — confirming the enum is enforced."""
        schema = _load_schema()
        bad_envelope = {
            "type": "agentExperimentResult",
            "data": {
                "experimentId": "voter_targeting",
                "runId": "r",
                "candidateId": "c",
                "status": "stale",
            },
        }
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad_envelope)

    def test_schema_rejects_wrong_envelope_type(self):
        schema = _load_schema()
        bad_envelope = {
            "type": "generateAiContent",
            "data": {
                "experimentId": "voter_targeting",
                "runId": "r",
                "candidateId": "c",
                "status": "success",
            },
        }
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad_envelope)


class TestSnapshotMatchesGpApiSource:
    """Cross-check the snapshot's status enum against the literal enum in
    gp-api/src/queue/queue.types.ts::AgentExperimentResultSchema. If gp-api
    adds a new allowed status, this test fails so we remember to update both
    the snapshot and pmf_engine's callback_handler."""

    @pytest.mark.skipif(
        not _GP_API_SOURCE.exists(),
        reason=f"gp-api source not checked out at {_GP_API_SOURCE}",
    )
    def test_status_enum_matches_zod_source(self):
        src = _GP_API_SOURCE.read_text()
        match = re.search(
            r"AgentExperimentResultSchema\s*=\s*z\.object\(\{.*?\}\)",
            src,
            re.DOTALL,
        )
        assert match, (
            f"Could not locate AgentExperimentResultSchema in {_GP_API_SOURCE}. "
            "If the schema moved, update this test's regex."
        )
        body = match.group(0)

        enum_match = re.search(r"status:\s*z\.enum\(\[(.*?)\]\)", body, re.DOTALL)
        assert enum_match, (
            "Could not locate `status: z.enum([...])` in "
            f"{_GP_API_SOURCE}::AgentExperimentResultSchema. "
            "If the status field stopped being a Zod enum, update this test."
        )
        zod_values = {
            v.strip().strip("'\"")
            for v in enum_match.group(1).split(",")
            if v.strip()
        }

        schema = _load_schema()
        snapshot_values = set(schema["properties"]["data"]["properties"]["status"]["enum"])

        assert zod_values == snapshot_values, (
            f"Snapshot enum drift!\n"
            f"  gp-api z.enum: {sorted(zod_values)}\n"
            f"  snapshot enum: {sorted(snapshot_values)}\n"
            f"Update {_FIXTURE} and pmf_engine.control_plane.callback_handler "
            "to match gp-api."
        )

    @pytest.mark.skipif(
        not _GP_API_SOURCE.exists(),
        reason=f"gp-api source not checked out at {_GP_API_SOURCE}",
    )
    def test_required_data_fields_match_zod_source(self):
        """All non-optional fields in Zod must be in `required` in the snapshot."""
        src = _GP_API_SOURCE.read_text()
        match = re.search(
            r"AgentExperimentResultSchema\s*=\s*z\.object\(\{(.*?)\}\)",
            src,
            re.DOTALL,
        )
        assert match
        body = match.group(1)
        field_lines = [line.strip() for line in body.split(",") if ":" in line]

        non_optional = set()
        for line in field_lines:
            name_match = re.match(r"(\w+)\s*:\s*(.*)", line)
            if not name_match:
                continue
            name = name_match.group(1)
            rhs = name_match.group(2)
            if ".optional()" in rhs:
                continue
            non_optional.add(name)

        schema = _load_schema()
        required = set(schema["properties"]["data"]["required"])
        assert non_optional == required, (
            f"Required fields drift:\n"
            f"  gp-api non-optional: {sorted(non_optional)}\n"
            f"  snapshot required:   {sorted(required)}"
        )
