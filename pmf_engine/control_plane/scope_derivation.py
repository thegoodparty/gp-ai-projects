from __future__ import annotations

import re

_STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
_MAX_CITY_DISTRICT_LEN = 200


def _validate_scope_string(field: str, value: str) -> None:
    if len(value) > _MAX_CITY_DISTRICT_LEN:
        raise ValueError(
            f"{field!r} exceeds max length {_MAX_CITY_DISTRICT_LEN}: got {len(value)} chars"
        )
    for ch in value:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValueError(
                f"{field!r} contains disallowed control character: {ch!r}"
            )


EXPERIMENT_SCOPE_CONFIG = {
    "voter_targeting": {
        "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
        "max_rows": 50000,
    },
    "walking_plan": {
        "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
        "max_rows": 50000,
    },
    "district_intel": {
        "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
        "max_rows": 50000,
    },
    "peer_city_benchmarking": {
        "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
        "max_rows": 50000,
    },
    "meeting_briefing": {
        "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
        "max_rows": 50000,
    },
}


def derive_scope(experiment_id: str, params: dict) -> dict:
    config = EXPERIMENT_SCOPE_CONFIG.get(experiment_id, {})

    state = params.get("state", "")
    if state and not _STATE_PATTERN.match(state):
        raise ValueError(
            f"state must be a 2-letter uppercase code or empty; got {state!r}"
        )

    city = params.get("city", "")
    if city:
        _validate_scope_string("city", city)

    district = params.get("district") or ""
    if district:
        _validate_scope_string("district", district)

    return {
        "state": state,
        "cities": [city] if city else [],
        "districts": [district] if district else [],
        "allowed_tables": config.get("allowed_tables", []),
        "max_rows": config.get("max_rows", 50000),
    }
