import pytest

from pmf_engine.control_plane.scope_derivation import derive_scope


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


class TestDataRequiredUnlessPassthrough:
    """Regression: the broker's NoDataQueriesSucceeded publish guard reads
    `data_required_unless` from the ticket scope. derive_scope must carry it
    through from the manifest, or legitimate no-data placeholder runs (e.g.
    meeting_briefing's awaiting_agenda / no_meeting_found) get rejected at
    publish even though no Databricks query is appropriate for that branch."""

    def test_data_required_unless_is_carried_through(self):
        manifest_scope = {
            "allowed_tables": ["goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"],
            "max_rows": 50000,
            "data_required_unless": {
                "field": "briefing_status",
                "values": ["awaiting_agenda", "no_meeting_found", "error"],
            },
        }
        scope = derive_scope("meeting_briefing", {"state": "NC"}, manifest_scope=manifest_scope)

        assert scope["data_required_unless"] == {
            "field": "briefing_status",
            "values": ["awaiting_agenda", "no_meeting_found", "error"],
        }

    def test_data_required_unless_absent_when_manifest_omits_it(self):
        scope = derive_scope("web_only_experiment", {"state": "NC"}, manifest_scope={"max_rows": 50000})
        assert scope.get("data_required_unless") is None
