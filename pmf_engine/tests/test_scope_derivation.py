import pytest

from pmf_engine.control_plane.scope_derivation import derive_gp_api_scope, derive_scope


SYNTHETIC_MANIFEST_SCOPE = {
    "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
    "max_rows": 50000,
}


class TestManifestScopePassthrough:
    def test_full_params_produces_correct_scope(self):
        """The scope's allowed_tables/max_rows come from the manifest (passed
        in via `manifest_scope`). The bundled EXPERIMENT_SCOPE_CONFIG fallback
        is gone — callers must supply the manifest's scope block."""
        params = {
            "state": "NC",
            "city": "Hendersonville",
            "district": "NC-11",
        }
        scope = derive_scope("smoke_test", params, manifest_scope=SYNTHETIC_MANIFEST_SCOPE)

        assert scope["state"] == "NC"
        assert scope["cities"] == ["Hendersonville"]
        assert scope["districts"] == ["NC-11"]
        assert scope["allowed_tables"] == SYNTHETIC_MANIFEST_SCOPE["allowed_tables"]
        assert scope["max_rows"] == 50000
        assert "allowed_columns" not in scope
        assert "dynamic_column_prefixes" not in scope

    def test_state_only_params_with_manifest_scope(self):
        params = {"state": "NC", "city": "Hendersonville"}
        scope = derive_scope("smoke_test", params, manifest_scope=SYNTHETIC_MANIFEST_SCOPE)

        assert scope["state"] == "NC"
        assert scope["cities"] == ["Hendersonville"]
        assert scope["allowed_tables"] == SYNTHETIC_MANIFEST_SCOPE["allowed_tables"]
        assert scope["max_rows"] == 50000
        assert "allowed_columns" not in scope


class TestCityIsPassedThroughVerbatim:
    """The dispatcher is responsible for giving us a clean city value.
    PMF engine does NOT normalize or parse — it trusts the input."""

    def test_city_used_as_is(self):
        params = {"state": "NC", "city": "Asheville"}
        scope = derive_scope("smoke_test", params)
        assert scope["cities"] == ["Asheville"]

    def test_city_case_preserved(self):
        params = {"state": "WI", "city": "STURGEON BAY"}
        scope = derive_scope("smoke_test", params)
        assert scope["cities"] == ["STURGEON BAY"]

    def test_election_location_is_ignored(self):
        params = {"state": "NC", "electionLocation": "Hendersonville"}
        scope = derive_scope("smoke_test", params)
        assert scope["cities"] == []


class TestMissingParams:
    def test_missing_state_defaults_to_empty_string(self):
        scope = derive_scope("smoke_test", {})
        assert scope["state"] == ""

    def test_missing_city_defaults_to_empty_list(self):
        scope = derive_scope("smoke_test", {})
        assert scope["cities"] == []

    def test_missing_district_defaults_to_empty_list(self):
        scope = derive_scope("smoke_test", {"state": "NC"})
        assert scope["districts"] == []

    def test_unknown_experiment_returns_defaults(self):
        scope = derive_scope("nonexistent_experiment", {"state": "CA"})
        assert scope["state"] == "CA"
        assert scope["allowed_tables"] == []
        assert scope["max_rows"] == 50000
        assert "allowed_columns" not in scope


class TestInputValidation:
    """Defense-in-depth on scope-derivation inputs. Control chars corrupt logs,
    unreasonable lengths are DoS vectors, state must be a 2-letter code. Broker's
    sqlglot parametrization handles SQL-injection-shaped strings downstream."""

    @pytest.mark.parametrize("bad_state", ["NCC", "N1", "N C", "N\x00C", "n\n"])
    def test_rejects_malformed_state(self, bad_state):
        with pytest.raises(ValueError, match="state"):
            derive_scope("smoke_test", {"state": bad_state, "city": "Durham"})

    @pytest.mark.parametrize("good_state", ["NC", "CA", "WI", "DC", ""])
    def test_accepts_valid_or_empty_state(self, good_state):
        scope = derive_scope("smoke_test", {"state": good_state})
        assert scope["state"] == good_state

    @pytest.mark.parametrize(
        "bad_city",
        [
            "Durham\x00",
            "Durham\nInjection",
            "A" * 201,
        ],
    )
    def test_rejects_control_chars_or_excessive_length_city(self, bad_city):
        with pytest.raises(ValueError, match="city"):
            derive_scope("smoke_test", {"state": "NC", "city": bad_city})

    @pytest.mark.parametrize(
        "good_city",
        [
            "Durham",
            "New York",
            "Fayetteville",
            "St. Paul",
            "O'Brien",
            "San Jose-Campbell",
            "Washington, D.C.",
            "STURGEON BAY",
            "A" * 200,
        ],
    )
    def test_accepts_reasonable_city_names(self, good_city):
        scope = derive_scope("smoke_test", {"state": "NC", "city": good_city})
        assert scope["cities"] == [good_city]

    @pytest.mark.parametrize(
        "bad_district",
        [
            "District\x00",
            "D\n",
            "D" * 201,
        ],
    )
    def test_rejects_control_chars_or_excessive_length_district(self, bad_district):
        with pytest.raises(ValueError, match="district"):
            derive_scope("smoke_test", {"state": "NC", "district": bad_district})


# ---------------------------------------------------------------------------
# gp-api scope derivation (write-action experiments — ENG-10128).
#
# Sibling to derive_scope; produces a Clerk-actor-JWT-shaped broker scope
# instead of a Databricks-table-shaped one. dispatch_handler chooses which
# to call based on whether the routing dict carries `allowed_gp_api_endpoints`.
# ---------------------------------------------------------------------------


class TestDeriveGpApiScopeHappyPath:
    def test_returns_gp_api_shaped_scope(self):
        scope = derive_gp_api_scope(
            "compliance_smoke_test",
            params={
                "campaign_id": "0a4c1b2e-1111-4222-8333-444444444444",
                "clerk_user_id": "user_2pZxQy8AbCdEfGhIjKlMn",
            },
            allowed_endpoints=[
                "GET /v1/campaigns/:id/compliance-state",
                "POST /v1/websites/domains/search",
            ],
        )
        assert scope["gp_api_allowed_endpoints"] == [
            "GET /v1/campaigns/:id/compliance-state",
            "POST /v1/websites/domains/search",
        ]
        assert scope["gp_api_allowed_campaign_id"] == "0a4c1b2e-1111-4222-8333-444444444444"
        assert scope["gp_api_acting_clerk_user_id"] == "user_2pZxQy8AbCdEfGhIjKlMn"

    def test_does_not_include_databricks_fields(self):
        """gp-api scope is disjoint from the Databricks scope shape so the broker
        can route off field presence without ambiguity."""
        scope = derive_gp_api_scope(
            "compliance_smoke_test",
            params={
                "campaign_id": "0a4c1b2e-1111-4222-8333-444444444444",
                "clerk_user_id": "user_abc123",
            },
            allowed_endpoints=["GET /v1/foo"],
        )
        for forbidden in ("state", "cities", "districts", "allowed_tables", "max_rows"):
            assert forbidden not in scope, f"unexpected {forbidden} in gp-api scope"

    def test_does_not_include_token_ttl(self):
        """Token TTL is a separate mint argument (BrokerClient.mint_run_token's
        exp_ttl_seconds) — matches the Databricks flow. Including it in the
        scope dict would create two sources of truth."""
        scope = derive_gp_api_scope(
            "compliance_smoke_test",
            params={
                "campaign_id": "0a4c1b2e-1111-4222-8333-444444444444",
                "clerk_user_id": "user_abc123",
            },
            allowed_endpoints=["GET /v1/foo"],
        )
        assert "exp_ttl_seconds" not in scope

    def test_endpoints_pass_through_verbatim(self):
        endpoints = ["GET /v1/x", "POST /v1/y", "PUT /v1/z"]
        scope = derive_gp_api_scope(
            "compliance_smoke_test",
            params={
                "campaign_id": "0a4c1b2e-1111-4222-8333-444444444444",
                "clerk_user_id": "user_abc123",
            },
            allowed_endpoints=endpoints,
        )
        assert scope["gp_api_allowed_endpoints"] == endpoints
        # Defensive copy expected — mutating the input shouldn't affect the scope.
        endpoints.append("DELETE /v1/everything")
        assert "DELETE /v1/everything" not in scope["gp_api_allowed_endpoints"]


class TestDeriveGpApiScopeValidation:
    _VALID_ENDPOINTS = ["GET /v1/foo"]

    def test_rejects_missing_campaign_id(self):
        params = {"clerk_user_id": "user_abc123"}
        with pytest.raises(ValueError, match="campaign_id"):
            derive_gp_api_scope("compliance_smoke_test", params, self._VALID_ENDPOINTS)

    def test_rejects_empty_campaign_id(self):
        params = {"campaign_id": "", "clerk_user_id": "user_abc123"}
        with pytest.raises(ValueError, match="campaign_id"):
            derive_gp_api_scope("compliance_smoke_test", params, self._VALID_ENDPOINTS)

    def test_rejects_non_string_campaign_id(self):
        params = {"campaign_id": 12345, "clerk_user_id": "user_abc123"}
        with pytest.raises(ValueError, match="campaign_id"):
            derive_gp_api_scope("compliance_smoke_test", params, self._VALID_ENDPOINTS)

    def test_rejects_missing_clerk_user_id(self):
        params = {"campaign_id": "0a4c1b2e-1111-4222-8333-444444444444"}
        with pytest.raises(ValueError, match="clerk_user_id"):
            derive_gp_api_scope("compliance_smoke_test", params, self._VALID_ENDPOINTS)

    def test_rejects_empty_clerk_user_id(self):
        params = {"campaign_id": "0a4c1b2e-1111-4222-8333-444444444444", "clerk_user_id": ""}
        with pytest.raises(ValueError, match="clerk_user_id"):
            derive_gp_api_scope("compliance_smoke_test", params, self._VALID_ENDPOINTS)

    def test_rejects_clerk_user_id_without_user_prefix(self):
        """`act.sub` audit subjects in Clerk are namespaced as `user_*`. Reject
        otherwise — a typo here would mint a token impersonating the wrong subject."""
        params = {"campaign_id": "0a4c1b2e-1111-4222-8333-444444444444", "clerk_user_id": "abc123"}
        with pytest.raises(ValueError, match="clerk_user_id"):
            derive_gp_api_scope("compliance_smoke_test", params, self._VALID_ENDPOINTS)

    def test_rejects_non_list_allowed_endpoints(self):
        """A bare string would otherwise silently produce a per-character
        endpoint list (`list("GET /v1/foo")` → ["G", "E", "T", ...]). Defense
        in depth for test callers that bypass the manifest loader."""
        params = {
            "campaign_id": "0a4c1b2e-1111-4222-8333-444444444444",
            "clerk_user_id": "user_abc123",
        }
        with pytest.raises(ValueError, match="allowed_endpoints"):
            derive_gp_api_scope("compliance_smoke_test", params, "GET /v1/foo")  # type: ignore[arg-type]

    def test_rejects_empty_allowed_endpoints(self):
        """Symmetric with the manifest_loader's rejection of empty
        allowed_gp_api_endpoints — an empty allowlist would mint a token with
        no permitted gp-api routes, which is never the intended dispatch
        outcome."""
        params = {
            "campaign_id": "0a4c1b2e-1111-4222-8333-444444444444",
            "clerk_user_id": "user_abc123",
        }
        with pytest.raises(ValueError, match="allowed_endpoints"):
            derive_gp_api_scope("compliance_smoke_test", params, [])
