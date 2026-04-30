from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY, VALID_MODES, validate_registry

REQUIRED_FIELDS = {"instruction", "contract", "harness", "model", "mode", "max_turns", "cpu", "memory"}
CONTRACT_REQUIRED_FIELDS = {"type", "s3_key_template"}


def test_registry_is_not_empty():
    assert len(EXPERIMENT_REGISTRY) > 0


def test_all_experiments_have_required_fields():
    for name, experiment in EXPERIMENT_REGISTRY.items():
        missing = REQUIRED_FIELDS - set(experiment.keys())
        assert not missing, f"Experiment '{name}' missing fields: {missing}"


def test_all_experiments_have_valid_contracts():
    for name, experiment in EXPERIMENT_REGISTRY.items():
        contract = experiment["contract"]
        missing = CONTRACT_REQUIRED_FIELDS - set(contract.keys())
        assert not missing, f"Experiment '{name}' contract missing fields: {missing}"


def test_all_experiments_have_non_empty_instruction():
    for name, experiment in EXPERIMENT_REGISTRY.items():
        assert experiment["instruction"].strip(), f"Experiment '{name}' has empty instruction"


def test_all_experiments_have_valid_harness():
    valid_harnesses = {"claude_sdk"}
    for name, experiment in EXPERIMENT_REGISTRY.items():
        assert experiment["harness"] in valid_harnesses, (
            f"Experiment '{name}' has unknown harness: {experiment['harness']}"
        )


def test_all_experiments_have_valid_s3_key_template():
    for name, experiment in EXPERIMENT_REGISTRY.items():
        template = experiment["contract"]["s3_key_template"]
        assert "{experiment_id}" in template, f"Experiment '{name}' template missing {{experiment_id}}"
        assert "{run_id}" in template, f"Experiment '{name}' template missing {{run_id}}"


def test_validate_registry_passes_with_no_errors():
    errors = validate_registry()
    assert errors == []


def test_validate_registry_catches_missing_field():
    bad_registry = {
        "broken": {
            "instruction": "do stuff",
        }
    }
    errors = validate_registry(bad_registry)
    assert len(errors) > 0
    assert "broken" in errors[0]


def test_voter_targeting_is_registered():
    assert "voter_targeting" in EXPERIMENT_REGISTRY


def test_walking_plan_is_registered():
    assert "walking_plan" in EXPERIMENT_REGISTRY


def test_district_intel_is_registered():
    assert "district_intel" in EXPERIMENT_REGISTRY


def test_all_experiments_have_valid_mode():
    for name, experiment in EXPERIMENT_REGISTRY.items():
        assert experiment["mode"] in VALID_MODES, (
            f"Experiment '{name}' has invalid mode: {experiment['mode']!r}"
        )


def test_win_experiments():
    win = [n for n, e in EXPERIMENT_REGISTRY.items() if e["mode"] == "win"]
    assert "voter_targeting" in win
    assert "walking_plan" in win


def test_peer_city_benchmarking_is_registered():
    assert "peer_city_benchmarking" in EXPERIMENT_REGISTRY


def test_serve_experiments():
    serve = [n for n, e in EXPERIMENT_REGISTRY.items() if e["mode"] == "serve"]
    assert "district_intel" in serve
    assert "peer_city_benchmarking" in serve
    assert "meeting_briefing" in serve


def test_meeting_briefing_is_registered():
    assert "meeting_briefing" in EXPERIMENT_REGISTRY


class TestDispatchRegistry:
    def setup_method(self):
        from pmf_engine.control_plane.dispatch_registry import DISPATCH_REGISTRY
        self.registry = DISPATCH_REGISTRY

    def test_has_all_experiments(self):
        for name in EXPERIMENT_REGISTRY:
            assert name in self.registry, f"DISPATCH_REGISTRY missing '{name}'"

    def test_has_required_fields(self):
        for name, entry in self.registry.items():
            assert "harness" in entry, f"'{name}' missing harness"
            assert "model" in entry, f"'{name}' missing model"
            assert "contract" in entry, f"'{name}' missing contract"
            assert "s3_key_template" in entry["contract"], f"'{name}' missing s3_key_template"
            assert "timeout_seconds" in entry, f"'{name}' missing timeout_seconds"
            assert entry["timeout_seconds"] > 0, f"'{name}' timeout_seconds must be positive"

    def test_s3_key_templates_match_full_registry(self):
        for name, entry in self.registry.items():
            full = EXPERIMENT_REGISTRY[name]
            assert entry["contract"]["s3_key_template"] == full["contract"]["s3_key_template"], (
                f"'{name}' s3_key_template mismatch between dispatch and full registry"
            )

    def test_timeout_seconds_match_full_registry(self):
        for name, entry in self.registry.items():
            full = EXPERIMENT_REGISTRY[name]
            assert entry["timeout_seconds"] == full["timeout_seconds"], (
                f"'{name}' timeout_seconds mismatch: dispatch={entry['timeout_seconds']} vs full={full['timeout_seconds']}"
            )
