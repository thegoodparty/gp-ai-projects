from __future__ import annotations

from pmf_engine.runner.experiments.voter_targeting import EXPERIMENT as VOTER_TARGETING
from pmf_engine.runner.experiments.walking_plan import EXPERIMENT as WALKING_PLAN
from pmf_engine.runner.experiments.district_intel import EXPERIMENT as DISTRICT_INTEL
from pmf_engine.runner.experiments.peer_city_benchmarking import EXPERIMENT as PEER_CITY_BENCHMARKING
from pmf_engine.runner.experiments.meeting_briefing import EXPERIMENT as MEETING_BRIEFING

EXPERIMENT_REGISTRY: dict[str, dict] = {
    "voter_targeting": VOTER_TARGETING,
    "walking_plan": WALKING_PLAN,
    "district_intel": DISTRICT_INTEL,
    "peer_city_benchmarking": PEER_CITY_BENCHMARKING,
    "meeting_briefing": MEETING_BRIEFING,
}

REQUIRED_FIELDS = {"instruction", "contract", "harness", "model", "mode", "max_turns", "cpu", "memory"}
VALID_MODES = {"win", "serve"}
CONTRACT_REQUIRED_FIELDS = {"type", "s3_key_template"}


def validate_registry(registry: dict[str, dict] | None = None) -> list[str]:
    if registry is None:
        registry = EXPERIMENT_REGISTRY

    errors = []
    for name, experiment in registry.items():
        missing = REQUIRED_FIELDS - set(experiment.keys())
        if missing:
            errors.append(f"Experiment '{name}' missing fields: {missing}")
            continue

        mode = experiment.get("mode")
        if mode not in VALID_MODES:
            errors.append(f"Experiment '{name}' has invalid mode: {mode!r} (must be one of {VALID_MODES})")

        contract = experiment.get("contract", {})
        contract_missing = CONTRACT_REQUIRED_FIELDS - set(contract.keys())
        if contract_missing:
            errors.append(f"Experiment '{name}' contract missing fields: {contract_missing}")

    return errors
