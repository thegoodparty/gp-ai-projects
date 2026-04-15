from pmf_engine.control_plane.callback_handler import format_gp_api_result_message


class TestGodQueueEnvelopeContract:
    def _base_message(self, **overrides):
        defaults = {
            "experiment_id": "voter_targeting",
            "run_id": "run-001",
            "candidate_id": "cand-123",
            "status": "success",
        }
        defaults.update(overrides)
        return defaults

    def test_envelope_type_is_agent_experiment_result(self):
        envelope = format_gp_api_result_message(self._base_message())
        assert envelope["type"] == "agentExperimentResult"

    def test_data_field_names_are_camel_case(self):
        message = self._base_message(
            artifact_key="voter_targeting/run-001/result.json",
            artifact_bucket="gp-agent-artifacts-dev",
            duration_seconds=45.2,
            error="something broke",
        )
        envelope = format_gp_api_result_message(message)
        data = envelope["data"]

        assert "experimentId" in data
        assert "runId" in data
        assert "candidateId" in data
        assert "artifactKey" in data
        assert "artifactBucket" in data
        assert "durationSeconds" in data

        assert "experiment_id" not in data
        assert "run_id" not in data
        assert "candidate_id" not in data
        assert "artifact_key" not in data
        assert "artifact_bucket" not in data
        assert "duration_seconds" not in data

    def test_status_values_match_gp_api_expectations(self):
        for status in ("success", "failed", "contract_violation"):
            envelope = format_gp_api_result_message(self._base_message(status=status))
            assert envelope["data"]["status"] == status

    def test_optional_fields_present_when_provided(self):
        message = self._base_message(
            artifact_key="voter_targeting/run-001/result.json",
            artifact_bucket="gp-agent-artifacts-dev",
            duration_seconds=120.5,
            error="contract mismatch",
        )
        envelope = format_gp_api_result_message(message)
        data = envelope["data"]

        assert data["artifactKey"] == "voter_targeting/run-001/result.json"
        assert data["artifactBucket"] == "gp-agent-artifacts-dev"
        assert data["durationSeconds"] == 120.5
        assert data["error"] == "contract mismatch"

    def test_optional_fields_absent_when_not_provided(self):
        envelope = format_gp_api_result_message(self._base_message())
        data = envelope["data"]

        assert "artifactKey" not in data
        assert "artifactBucket" not in data
        assert "durationSeconds" not in data
        assert "error" not in data
