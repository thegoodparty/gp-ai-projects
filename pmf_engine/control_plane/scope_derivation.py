from __future__ import annotations

import re

_STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
_MAX_CITY_DISTRICT_LEN = 200


def _validate_scope_string(field: str, value: str) -> None:
    if len(value) > _MAX_CITY_DISTRICT_LEN:
        raise ValueError(f"{field!r} exceeds max length {_MAX_CITY_DISTRICT_LEN}: got {len(value)} chars")
    for ch in value:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValueError(f"{field!r} contains disallowed control character: {ch!r}")


def derive_scope(experiment_id: str, params: dict, manifest_scope: dict | None = None) -> dict:
    """Build the broker scope ticket fields.

    `manifest_scope` is the `scope` block from the experiment manifest in S3.
    When present, its `allowed_tables`, `max_rows`, and `data_required_unless`
    are passed through verbatim. `data_required_unless` MUST be carried through:
    the broker's NoDataQueriesSucceeded publish guard reads it from the ticket
    scope to exempt legitimate no-data placeholder runs (e.g. meeting_briefing's
    `briefing_status=awaiting_agenda`). Dropping it here causes those runs to be
    rejected at publish even though no Databricks query is appropriate.

    Permissive defaults are intentional: when `manifest_scope` is None or empty
    (e.g. web-research-only experiments with no Databricks access), we default
    to `allowed_tables=[]` (no table access) and `max_rows=50000`. The empty
    `allowed_tables` is a hard deny at the broker layer — there is no implicit
    table grant. Callers needing Databricks data MUST supply a manifest scope
    block; absence here is treated as "no Databricks data needed" rather than
    a publish-pipeline bug.
    """
    config = manifest_scope or {}

    state = params.get("state", "")
    if state and not _STATE_PATTERN.match(state):
        raise ValueError(f"state must be a 2-letter uppercase code or empty; got {state!r}")

    city = params.get("city", "")
    if city:
        _validate_scope_string("city", city)

    district = params.get("district") or ""
    if district:
        _validate_scope_string("district", district)

    scope = {
        "state": state,
        "cities": [city] if city else [],
        "districts": [district] if district else [],
        "allowed_tables": config.get("allowed_tables", []),
        "max_rows": config.get("max_rows", 50000),
    }
    if "data_required_unless" in config:
        scope["data_required_unless"] = config["data_required_unless"]
    return scope
