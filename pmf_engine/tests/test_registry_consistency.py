"""DISPATCH_REGISTRY is now derived from EXPERIMENT_REGISTRY at import time
(see pmf_engine/control_plane/dispatch_registry.py::_derive), so the historical
"do these two hand-maintained dicts agree?" checks are tautological.

The tests below used to guard against drift. After the derivation refactor
they exist as a smoke test: derivation produces the expected five-experiment
projection and the four required routing fields are all present.
"""

from pmf_engine.control_plane.dispatch_registry import DISPATCH_REGISTRY
from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY


class TestDispatchRegistryProjection:
    def test_every_experiment_is_routable(self):
        assert set(DISPATCH_REGISTRY.keys()) == set(EXPERIMENT_REGISTRY.keys())

    def test_every_entry_has_required_routing_fields(self):
        required = {"harness", "model", "timeout_seconds", "contract"}
        for exp_id, entry in DISPATCH_REGISTRY.items():
            missing = required - set(entry.keys())
            assert not missing, f"{exp_id} missing routing fields: {missing}"
            assert "s3_key_template" in entry["contract"], (
                f"{exp_id} contract missing s3_key_template"
            )

    def test_projection_matches_source_of_truth(self):
        for exp_id, full in EXPERIMENT_REGISTRY.items():
            d = DISPATCH_REGISTRY[exp_id]
            assert d["harness"] == full["harness"]
            assert d["model"] == full["model"]
            assert d["timeout_seconds"] == full.get("timeout_seconds", 600)
            assert d["contract"]["s3_key_template"] == full["contract"]["s3_key_template"]
