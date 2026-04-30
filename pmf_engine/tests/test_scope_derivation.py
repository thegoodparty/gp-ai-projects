import pytest

from pmf_engine.control_plane.scope_derivation import derive_scope


class TestVoterTargetingScope:
    def test_full_params_produces_correct_scope(self):
        params = {
            "state": "NC",
            "city": "Hendersonville",
            "district": "NC-11",
        }
        scope = derive_scope("voter_targeting", params)

        assert scope["state"] == "NC"
        assert scope["cities"] == ["Hendersonville"]
        assert scope["districts"] == ["NC-11"]
        assert "goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq" in scope["allowed_tables"]
        assert scope["max_rows"] == 50000
        assert "allowed_columns" not in scope
        assert "dynamic_column_prefixes" not in scope


class TestDistrictIntelScope:
    def test_serve_experiment_has_l2_access_for_demographics(self):
        params = {"state": "NC", "city": "Hendersonville"}
        scope = derive_scope("district_intel", params)

        assert scope["state"] == "NC"
        assert scope["cities"] == ["Hendersonville"]
        assert "goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq" in scope["allowed_tables"]
        assert scope["max_rows"] == 50000
        assert "allowed_columns" not in scope


class TestCityIsPassedThroughVerbatim:
    """The dispatcher is responsible for giving us a clean city value.
    PMF engine does NOT normalize or parse — it trusts the input."""

    def test_city_used_as_is(self):
        params = {"state": "NC", "city": "Asheville"}
        scope = derive_scope("voter_targeting", params)
        assert scope["cities"] == ["Asheville"]

    def test_city_case_preserved(self):
        params = {"state": "WI", "city": "STURGEON BAY"}
        scope = derive_scope("voter_targeting", params)
        assert scope["cities"] == ["STURGEON BAY"]

    def test_election_location_is_ignored(self):
        params = {"state": "NC", "electionLocation": "Hendersonville"}
        scope = derive_scope("voter_targeting", params)
        assert scope["cities"] == []


class TestMissingParams:
    def test_missing_state_defaults_to_empty_string(self):
        scope = derive_scope("voter_targeting", {})
        assert scope["state"] == ""

    def test_missing_city_defaults_to_empty_list(self):
        scope = derive_scope("voter_targeting", {})
        assert scope["cities"] == []

    def test_missing_district_defaults_to_empty_list(self):
        scope = derive_scope("voter_targeting", {"state": "NC"})
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
            derive_scope("voter_targeting", {"state": bad_state, "city": "Durham"})

    @pytest.mark.parametrize("good_state", ["NC", "CA", "WI", "DC", ""])
    def test_accepts_valid_or_empty_state(self, good_state):
        scope = derive_scope("voter_targeting", {"state": good_state})
        assert scope["state"] == good_state

    @pytest.mark.parametrize("bad_city", [
        "Durham\x00",
        "Durham\nInjection",
        "A" * 201,
    ])
    def test_rejects_control_chars_or_excessive_length_city(self, bad_city):
        with pytest.raises(ValueError, match="city"):
            derive_scope("voter_targeting", {"state": "NC", "city": bad_city})

    @pytest.mark.parametrize("good_city", [
        "Durham",
        "New York",
        "Fayetteville",
        "St. Paul",
        "O'Brien",
        "San Jose-Campbell",
        "Washington, D.C.",
        "STURGEON BAY",
        "A" * 200,
    ])
    def test_accepts_reasonable_city_names(self, good_city):
        scope = derive_scope("voter_targeting", {"state": "NC", "city": good_city})
        assert scope["cities"] == [good_city]

    @pytest.mark.parametrize("bad_district", [
        "District\x00",
        "D\n",
        "D" * 201,
    ])
    def test_rejects_control_chars_or_excessive_length_district(self, bad_district):
        with pytest.raises(ValueError, match="district"):
            derive_scope("voter_targeting", {"state": "NC", "district": bad_district})
