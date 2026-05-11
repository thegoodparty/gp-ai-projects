from __future__ import annotations

import re

_STATE_PATTERN = re.compile(r"^[A-Z]{2}$")
_MAX_CITY_DISTRICT_LEN = 200
_CLERK_USER_ID_PATTERN = re.compile(r"^user_[A-Za-z0-9]+$")


def _validate_scope_string(field: str, value: str) -> None:
    if len(value) > _MAX_CITY_DISTRICT_LEN:
        raise ValueError(f"{field!r} exceeds max length {_MAX_CITY_DISTRICT_LEN}: got {len(value)} chars")
    for ch in value:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValueError(f"{field!r} contains disallowed control character: {ch!r}")


def derive_scope(experiment_id: str, params: dict, manifest_scope: dict | None = None) -> dict:
    """Build the broker scope ticket fields.

    `manifest_scope` is the `scope` block from the experiment manifest in S3.
    When present, its `allowed_tables` and `max_rows` are passed through verbatim.

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

    return {
        "state": state,
        "cities": [city] if city else [],
        "districts": [district] if district else [],
        "allowed_tables": config.get("allowed_tables", []),
        "max_rows": config.get("max_rows", 50000),
    }


def derive_gp_api_scope(
    experiment_id: str,
    params: dict,
    allowed_endpoints: list[str],
) -> dict:
    """Build the broker scope for a write-action experiment (ENG-10128).

    Sibling to `derive_scope`. Where `derive_scope` produces a Databricks-shaped
    scope (table allowlist + row cap) for read-only experiments, this produces
    a Clerk-actor-JWT-shaped scope so the broker can mint an actor token that
    impersonates the candidate against gp-api. Disjoint field set so the broker
    can dispatch on field presence without a discriminator.

    Token TTL is supplied separately to `BrokerClient.mint_run_token` —
    matching the existing Databricks-scope flow, where TTL is a mint argument
    rather than a scope field.

    Platform contract — every write-action experiment dispatches with:
      - `params.campaign_id`: the resource the agent is allowed to act on.
      - `params.clerk_user_id`: the human candidate the actor token impersonates
        (becomes the JWT's `act.sub`).
    Per-experiment manifests' `input_schema` enforces both as required fields;
    the regex / non-empty checks here are defense in depth against stale
    routing dicts or test callers that bypass the upstream validators. If a
    future write-action experiment needs a different scoping shape (e.g.
    multi-campaign, no candidate impersonation), add a sibling
    `derive_gp_api_v2_scope` rather than overloading this function — the broker
    dispatches on scope-field presence, so a new shape can land cleanly.

    `allowed_endpoints` comes from the manifest (validated in
    `manifest_loader._validate_write_action_fields` and projected via
    `_project_routing`); the type / non-empty guard here exists so test callers
    that bypass the loader still get a clear error instead of silently producing
    a per-character endpoint list when a bare string is passed.
    """
    if not isinstance(allowed_endpoints, list) or not allowed_endpoints:
        raise ValueError(
            f"{experiment_id}: allowed_endpoints must be a non-empty list of strings; got {allowed_endpoints!r}"
        )

    campaign_id = params.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError(f"{experiment_id}: campaign_id must be a non-empty string; got {campaign_id!r}")

    clerk_user_id = params.get("clerk_user_id")
    if not isinstance(clerk_user_id, str) or not _CLERK_USER_ID_PATTERN.match(clerk_user_id):
        raise ValueError(
            f"{experiment_id}: clerk_user_id must match {_CLERK_USER_ID_PATTERN.pattern}; got {clerk_user_id!r}"
        )

    return {
        "gp_api_allowed_endpoints": list(allowed_endpoints),
        "gp_api_allowed_campaign_id": campaign_id,
        "gp_api_acting_clerk_user_id": clerk_user_id,
    }
