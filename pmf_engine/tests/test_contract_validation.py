import json

import pytest

from pmf_engine.runner.contract import (
    validate_artifact_contract,
    ContractViolation,
    format_contract_for_prompt,
    collect_contract_errors,
)
from pmf_engine.runner.experiments.voter_targeting import EXPERIMENT as _VOTER_TARGETING_EXPERIMENT
from pmf_engine.runner.experiments.walking_plan import EXPERIMENT as _WALKING_PLAN_EXPERIMENT
from pmf_engine.runner.experiments.district_intel import EXPERIMENT as _DISTRICT_INTEL_EXPERIMENT
from pmf_engine.runner.experiments.peer_city_benchmarking import EXPERIMENT as _PEER_CITY_EXPERIMENT
from pmf_engine.runner.experiments.meeting_briefing import EXPERIMENT as _MEETING_BRIEFING_EXPERIMENT


VOTER_TARGETING_SCHEMA = _VOTER_TARGETING_EXPERIMENT["contract"]["schema"]
WALKING_PLAN_SCHEMA = _WALKING_PLAN_EXPERIMENT["contract"]["schema"]


def _valid_voter_targeting_artifact() -> dict:
    return {
        "organization_slug": "1",
        "district": {"state": "MI", "type": "CITY", "name": "Mayor"},
        "generated_at": "2026-03-25T00:00:00Z",
        "summary": {
            "total_voters_in_district": 5000,
            "win_number": 1500,
            "projected_turnout": 3000,
        },
        "segments": [{
            "tier": 1,
            "name": "Strong Prospects",
            "description": "Independent-leaning voters high on appeal score.",
            "count": 500,
            "demographics": {
                "party_breakdown": {"I": 300, "D": 100, "R": 100},
                "age_distribution": {"18-34": 200, "35-54": 200, "55+": 100},
                "gender_split": {"F": 260, "M": 240},
            },
            "outreach_priority": "high",
            "recommended_channels": ["door", "text"],
            "voters": [{
                "voter_id": "V001",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "address": "100 Oak",
                "city": "Detroit",
                "zip": "48201",
                "age": 42,
                "gender": "F",
                "party": "I",
                "voter_status": "active",
            }],
        }],
        "geographic_clusters": [{
            "area": "Ward 3",
            "voter_count": 1200,
            "density_rank": 1,
        }],
        "methodology": "Scored by appeal.",
    }


def _valid_walking_plan_artifact() -> dict:
    return {
        "organization_slug": "1",
        "district": {"state": "MI"},
        "generated_at": "2026-03-25T00:00:00Z",
        "summary": {
            "total_areas": 5,
            "total_doors": 200,
            "estimated_total_hours": 12.5,
            "top_issues": ["taxes", "schools"],
        },
        "areas": [{
            "name": "Evans St",
            "zip": "48201",
            "city": "Detroit",
            "priority_rank": 1,
            "door_count": 40,
            "estimated_minutes": 90,
            "maps_url": "https://google.com/maps/dir/...",
            "voters": [{
                "order": 1,
                "address": "123 Evans St",
                "voter_name": "Jane Doe",
                "party": "I",
                "voter_status": "active",
                "age": 35,
                "talking_points": ["Property taxes"],
            }],
        }],
        "methodology": "Clustered by lat/lon.",
    }


class TestValidateArtifactContract:
    def test_valid_artifact_passes(self):
        artifact = _valid_voter_targeting_artifact()
        validate_artifact_contract(json.dumps(artifact).encode(), VOTER_TARGETING_SCHEMA)

    def test_missing_top_level_field_raises(self):
        artifact = {"organization_slug": "1"}
        with pytest.raises(ContractViolation, match="district"):
            validate_artifact_contract(json.dumps(artifact).encode(), VOTER_TARGETING_SCHEMA)

    def test_missing_nested_field_raises(self):
        artifact = {
            "organization_slug": "1",
            "district": {"state": "MI"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {
                "total_voters_in_district": 5000,
                "win_number": 1500,
                "projected_turnout": 3000,
            },
            "segments": [{"tier": 1, "name": "T1", "count": 1, "voters": [{"voter_id": "V1"}]}],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="district.type"):
            validate_artifact_contract(json.dumps(artifact).encode(), VOTER_TARGETING_SCHEMA)

    def test_wrong_type_raises(self):
        artifact = {
            "organization_slug": "1",
            "district": {"state": "MI", "type": "CITY", "name": "Mayor"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {
                "total_voters_in_district": "not a number",
                "win_number": 1500,
                "projected_turnout": 3000,
            },
            "segments": [{"tier": 1, "name": "T1", "count": 1, "voters": [{"voter_id": "V1"}]}],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="summary.total_voters_in_district"):
            validate_artifact_contract(json.dumps(artifact).encode(), VOTER_TARGETING_SCHEMA)

    def test_empty_array_raises(self):
        artifact = {
            "organization_slug": "1",
            "district": {"state": "MI", "type": "CITY", "name": "Mayor"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {
                "total_voters_in_district": 5000,
                "win_number": 1500,
                "projected_turnout": 3000,
            },
            "segments": [],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="segments"):
            validate_artifact_contract(json.dumps(artifact).encode(), VOTER_TARGETING_SCHEMA)

    def test_array_item_missing_field_raises(self):
        artifact = {
            "organization_slug": "1",
            "district": {"state": "MI", "type": "CITY", "name": "Mayor"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {
                "total_voters_in_district": 5000,
                "win_number": 1500,
                "projected_turnout": 3000,
            },
            "segments": [{"tier": 1, "name": "T1", "count": 1, "voters": [{"voter_id": "V1"}]}, {"tier": 2}],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="segments\\[1\\].name"):
            validate_artifact_contract(json.dumps(artifact).encode(), VOTER_TARGETING_SCHEMA)

    def test_invalid_json_raises(self):
        with pytest.raises(ContractViolation, match="Invalid JSON"):
            validate_artifact_contract(b"not json", VOTER_TARGETING_SCHEMA)

    def test_non_object_json_raises(self):
        with pytest.raises(ContractViolation, match="must be a JSON object"):
            validate_artifact_contract(b'"just a string"', VOTER_TARGETING_SCHEMA)

    def test_walking_plan_valid(self):
        artifact = _valid_walking_plan_artifact()
        validate_artifact_contract(json.dumps(artifact).encode(), WALKING_PLAN_SCHEMA)

    def test_multi_item_list_schema_is_author_error(self):
        """A schema list of length != 1 is an author bug. Currently the
        validator silently skips validation for such entries, letting a
        copy-paste mistake like `[{"a": "string"}, {"b": "string"}]` vacuously
        pass every artifact. It must surface as an error so schema authoring
        mistakes are caught by tests."""
        from pmf_engine.runner.contract import collect_contract_errors
        schema = {"items": [{"a": "string"}, {"b": "string"}]}
        errors = collect_contract_errors(b'{"items": [{"a": "x"}]}', schema)
        assert errors, "Expected schema-author error for multi-item list schema"
        assert any("items" in e for e in errors)

    def test_none_schema_skips_validation(self):
        validate_artifact_contract(b"anything", None)

    def test_empty_schema_skips_validation(self):
        validate_artifact_contract(b"anything", {})

    def test_float_accepted_for_number_type(self):
        schema = {"score": "number"}
        artifact = json.dumps({"score": 99.7}).encode()
        validate_artifact_contract(artifact, schema)

    def test_boolean_rejected_for_number_type(self):
        schema = {"count": "number"}
        artifact = json.dumps({"count": True}).encode()
        with pytest.raises(ContractViolation, match="count"):
            validate_artifact_contract(artifact, schema)

    def test_null_value_rejected_for_string_field(self):
        schema = {"name": "string"}
        artifact = json.dumps({"name": None}).encode()
        with pytest.raises(ContractViolation, match="name"):
            validate_artifact_contract(artifact, schema)

    def test_null_value_rejected_for_nested_object(self):
        schema = {"district": {"state": "string"}}
        artifact = json.dumps({"district": None}).encode()
        with pytest.raises(ContractViolation, match="district"):
            validate_artifact_contract(artifact, schema)

    def test_extra_fields_are_allowed(self):
        schema = {"name": "string"}
        artifact = json.dumps({"name": "Alice", "age": 30, "extra": {"x": 1}}).encode()
        validate_artifact_contract(artifact, schema)

    def test_string_where_object_expected_raises(self):
        schema = {"district": {"state": "string"}}
        artifact = json.dumps({"district": "MI"}).encode()
        with pytest.raises(ContractViolation, match="district.*expected object"):
            validate_artifact_contract(artifact, schema)

    def test_string_where_array_expected_raises(self):
        schema = {"segments": [{"name": "string"}]}
        artifact = json.dumps({"segments": "not an array"}).encode()
        with pytest.raises(ContractViolation, match="segments.*expected array"):
            validate_artifact_contract(artifact, schema)

    def test_array_of_primitives_valid(self):
        schema = {"tags": ["string"]}
        artifact = json.dumps({"tags": ["a", "b"]}).encode()
        validate_artifact_contract(artifact, schema)

    def test_array_of_primitives_wrong_item_type(self):
        schema = {"tags": ["string"]}
        artifact = json.dumps({"tags": ["a", 42]}).encode()
        with pytest.raises(ContractViolation, match="tags\\[1\\]"):
            validate_artifact_contract(artifact, schema)


DISTRICT_INTEL_SCHEMA = _DISTRICT_INTEL_EXPERIMENT["contract"]["schema"]


class TestDistrictIntelContract:
    def test_valid_district_intel_passes(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "City Council Member",
            "district": {"state": "MI", "type": "City_Ward", "name": "Ward 3"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {
                "total_constituents": 8500,
                "issues_identified": 3,
                "meetings_analyzed": 4,
                "sources_consulted": 7,
            },
            "issues": [{
                "title": "Senior Center Funding",
                "summary": "Proposed 15% budget cut to senior services[1] affecting the Main St facility[2].",
                "status": "active",
                "affected_constituents": 2100,
                "affected_segments": [
                    {"name": "Seniors (65+)", "count": 1800, "description": "Directly impacted by service cuts"},
                    {"name": "Caregivers (35-54)", "count": 300, "description": "Family members of affected seniors"},
                ],
                "sources": [
                    {"id": 1, "name": "City Council Minutes", "url": "https://example.com/minutes/2026-03", "date": "2026-03-10"},
                    {"id": 2, "name": "Tecumseh Herald", "url": "https://example.com/herald/senior-center", "date": "2026-03-12"},
                ],
            }],
            "demographic_snapshot": {
                "total_voters": 8500,
                "party_breakdown": [
                    {"party": "Non-Partisan", "count": 3200},
                    {"party": "Democratic", "count": 2800},
                ],
                "age_distribution": [
                    {"range": "18-34", "count": 1500},
                    {"range": "35-54", "count": 3000},
                    {"range": "55+", "count": 4000},
                ],
            },
            "methodology": "Web research of city council minutes and local news.",
        }
        validate_artifact_contract(json.dumps(artifact).encode(), DISTRICT_INTEL_SCHEMA)

    def test_missing_issues_array_raises(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "Council Member",
            "district": {"state": "MI", "type": "City_Ward", "name": "Ward 3"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {"total_constituents": 100, "issues_identified": 0, "meetings_analyzed": 0, "sources_consulted": 0},
            "issues": [],

            "demographic_snapshot": {
                "total_voters": 100,
                "party_breakdown": [{"party": "NP", "count": 100}],
                "age_distribution": [{"range": "18-34", "count": 100}],
            },
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="issues"):
            validate_artifact_contract(json.dumps(artifact).encode(), DISTRICT_INTEL_SCHEMA)

    def test_issue_missing_affected_segments_raises(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "Council Member",
            "district": {"state": "MI", "type": "City_Ward", "name": "Ward 3"},
            "generated_at": "2026-03-25T00:00:00Z",
            "summary": {"total_constituents": 100, "issues_identified": 1, "meetings_analyzed": 1, "sources_consulted": 1},
            "issues": [{
                "title": "Test Issue",
                "summary": "Test[1]",
                "status": "active",
                "affected_constituents": 50,
                "affected_segments": [],
                "sources": [{"id": 1, "name": "Test", "url": "https://example.com", "date": "2026-03-10"}],
            }],
            "demographic_snapshot": {
                "total_voters": 100,
                "party_breakdown": [{"party": "NP", "count": 100}],
                "age_distribution": [{"range": "18-34", "count": 100}],
            },
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="affected_segments"):
            validate_artifact_contract(json.dumps(artifact).encode(), DISTRICT_INTEL_SCHEMA)


PEER_CITY_BENCHMARKING_SCHEMA = _PEER_CITY_EXPERIMENT["contract"]["schema"]


class TestPeerCityBenchmarkingContract:
    def test_valid_peer_city_benchmarking_passes(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "City Council Member",
            "district": {"state": "MI", "name": "Ward 3"},
            "generated_at": "2026-03-26T00:00:00Z",
            "based_on_district_intel_run": "run-abc-123",
            "summary": {
                "home_city_population": 8500,
                "peer_cities_analyzed": 3,
                "issues_compared": 2,
                "sources_consulted": 12,
            },
            "home_city": {"name": "Tecumseh", "state": "MI", "population": 8500},
            "peer_cities": [{
                "name": "Saline",
                "state": "MI",
                "population": 9400,
                "similarity_reason": "Similar population, same state, small-city government structure",
            }],
            "comparisons": [{
                "issue": "Senior Center Funding",
                "home_city_approach": "Proposed 15% budget cut to senior services.",
                "peer_approaches": [{
                    "city": "Saline",
                    "approach": "Partnered with county for shared senior services facility",
                    "outcome": "Reduced city costs by 20% while expanding services",
                    "budget": "$450,000 annually (shared with county)",
                    "timeline": "18-month rollout (2024-2025)",
                    "sources": [
                        {"id": 1, "name": "Saline City Council Minutes", "url": "https://example.com/saline/minutes", "date": "2024-06-15"},
                    ],
                }],
                "takeaways": "County partnerships can reduce costs while expanding service reach.",
            }],
            "methodology": "Identified peer cities by population similarity and government structure.",
        }
        validate_artifact_contract(json.dumps(artifact).encode(), PEER_CITY_BENCHMARKING_SCHEMA)

    def test_empty_comparisons_array_raises(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "Council Member",
            "district": {"state": "MI", "name": "Ward 3"},
            "generated_at": "2026-03-26T00:00:00Z",
            "based_on_district_intel_run": "run-abc-123",
            "summary": {"home_city_population": 100, "peer_cities_analyzed": 0, "issues_compared": 0, "sources_consulted": 0},
            "home_city": {"name": "Tecumseh", "state": "MI", "population": 100},
            "peer_cities": [{"name": "Saline", "state": "MI", "population": 200, "similarity_reason": "nearby"}],
            "comparisons": [],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="comparisons"):
            validate_artifact_contract(json.dumps(artifact).encode(), PEER_CITY_BENCHMARKING_SCHEMA)

    def test_empty_peer_cities_raises(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "Council Member",
            "district": {"state": "MI", "name": "Ward 3"},
            "generated_at": "2026-03-26T00:00:00Z",
            "based_on_district_intel_run": "run-abc-123",
            "summary": {"home_city_population": 100, "peer_cities_analyzed": 0, "issues_compared": 0, "sources_consulted": 0},
            "home_city": {"name": "Tecumseh", "state": "MI", "population": 100},
            "peer_cities": [],
            "comparisons": [{
                "issue": "Test",
                "home_city_approach": "Nothing",
                "peer_approaches": [{"city": "X", "approach": "Y", "outcome": "Z", "budget": "$0", "timeline": "N/A", "sources": [{"id": 1, "name": "T", "url": "https://x.com", "date": "2026-01-01"}]}],
                "takeaways": "None",
            }],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="peer_cities"):
            validate_artifact_contract(json.dumps(artifact).encode(), PEER_CITY_BENCHMARKING_SCHEMA)

    def test_comparison_missing_peer_approaches_raises(self):
        artifact = {
            "official_name": "Jane Smith",
            "office": "Council Member",
            "district": {"state": "MI", "name": "Ward 3"},
            "generated_at": "2026-03-26T00:00:00Z",
            "based_on_district_intel_run": "run-abc-123",
            "summary": {"home_city_population": 100, "peer_cities_analyzed": 1, "issues_compared": 1, "sources_consulted": 1},
            "home_city": {"name": "Tecumseh", "state": "MI", "population": 100},
            "peer_cities": [{"name": "Saline", "state": "MI", "population": 200, "similarity_reason": "nearby"}],
            "comparisons": [{
                "issue": "Test",
                "home_city_approach": "Nothing",
                "peer_approaches": [],
                "takeaways": "None",
            }],
            "methodology": "test",
        }
        with pytest.raises(ContractViolation, match="peer_approaches"):
            validate_artifact_contract(json.dumps(artifact).encode(), PEER_CITY_BENCHMARKING_SCHEMA)


MEETING_BRIEFING_SCHEMA = _MEETING_BRIEFING_EXPERIMENT["contract"]["schema"]

VALID_MEETING_BRIEFING = {
    "eo": {"name": "Jane Smith", "city": "Fayetteville", "state": "NC", "office": "City Council District 1"},
    "meeting": {"body": "City Council Regular Meeting", "date": "2026-03-23", "time": "6:30 PM", "agenda_source": "legistar"},
    "agenda_items": [{
        "item_number": "8.01",
        "title": "P26-05 Rezoning at 5210 Arbor Rd",
        "type": "public_hearing",
        "requires_vote": True,
    }],
    "fiscal": {"tax_rate": "$0.5795 per $100", "budget_total": "$274.3M", "source": "NC LINC"},
    "data_quality": {"agenda": "high", "fiscal": "high", "platform": "high", "overall": "high"},
    "teaser_email": "**Subject: Your prep for Monday's meeting**\n\nJane, two things...",
    "briefing_content": "# Governance Briefing\n\n## Your priorities...",
    "score": {
        "total": 79,
        "max": 120,
        "recommendation": "send",
        "dimensions": [{
            "id": "D1",
            "name": "Legislative Record",
            "score": 8,
            "justification": "All items have formal identifiers.",
        }],
    },
    "sources": [{
        "id": "S1",
        "type": "government_record",
        "title": "Fayetteville Legistar 2026-03-23",
        "url": "https://example.com/legistar",
        "accessed_at": "2026-03-27T11:00:00Z",
    }],
    "generated_at": "2026-03-27T12:00:00Z",
    "based_on_district_intel_run": "none",
}


class TestMeetingBriefingContract:
    def test_valid_meeting_briefing_passes(self):
        validate_artifact_contract(json.dumps(VALID_MEETING_BRIEFING).encode(), MEETING_BRIEFING_SCHEMA)

    def test_missing_eo_raises(self):
        artifact = {k: v for k, v in VALID_MEETING_BRIEFING.items() if k != "eo"}
        with pytest.raises(ContractViolation, match="eo"):
            validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA)

    def test_missing_score_dimension_field_raises(self):
        artifact = {**VALID_MEETING_BRIEFING, "score": {
            "total": 79, "max": 120, "recommendation": "send",
            "dimensions": [{"id": "D1", "name": "Legislative Record"}],
        }}
        with pytest.raises(ContractViolation, match="score"):
            validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA)

    def test_empty_agenda_items_raises(self):
        artifact = {**VALID_MEETING_BRIEFING, "agenda_items": []}
        with pytest.raises(ContractViolation, match="agenda_items"):
            validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA)

    def test_empty_dimensions_raises(self):
        artifact = {**VALID_MEETING_BRIEFING, "score": {
            "total": 0, "max": 120, "recommendation": "hold", "dimensions": [],
        }}
        with pytest.raises(ContractViolation, match="dimensions"):
            validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA)

    def test_wrong_type_requires_vote_raises(self):
        artifact = {**VALID_MEETING_BRIEFING, "agenda_items": [{
            "item_number": "1", "title": "Test", "type": "consent", "requires_vote": "yes",
        }]}
        with pytest.raises(ContractViolation, match="requires_vote"):
            validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA)

    def test_extra_fields_allowed(self):
        artifact = {**VALID_MEETING_BRIEFING, "campaign_platform": {"priorities": ["safety"]}, "committees": []}
        validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA)


class TestFormatContractForPrompt:
    def test_formats_schema_as_readable_spec(self):
        schema = {
            "name": "string",
            "count": "number",
            "items": [{"id": "string"}],
        }
        result = format_contract_for_prompt(schema)
        assert result.startswith("## OUTPUT CONTRACT")
        assert '"name": "string"' in result
        assert '"count": "number"' in result
        assert "```json" in result
        assert "at least one item" in result

    def test_returns_empty_for_none(self):
        assert format_contract_for_prompt(None) == ""

    def test_returns_empty_for_empty(self):
        assert format_contract_for_prompt({}) == ""

    def test_constraints_appended_when_present(self):
        schema = {"score": {"total": "number"}}
        constraints = {
            "enums": [{"path": "status", "values": ["active", "done"]}],
            "ranges": [{"path": "score.total", "min": 0, "max": 120}],
            "array_length": [{"path": "items", "exact": 12}],
            "equals": [{"left": "score.total", "right": {"sum": "items[].score"}}],
        }
        result = format_contract_for_prompt(schema, constraints)
        assert "FIELD CONSTRAINTS" in result
        assert "status" in result
        assert "active" in result
        assert "score.total" in result
        assert "120" in result
        assert "exactly 12 items" in result
        assert "sum of items[].score" in result


_BASE_MB = VALID_MEETING_BRIEFING

MEETING_BRIEFING_CONSTRAINTS = {
    "enums": [
        {"path": "score.recommendation", "values": ["send", "review", "hold"]},
        {"path": "data_quality.agenda", "values": ["high", "medium", "low", "not_applicable"]},
    ],
    "ranges": [
        {"path": "score.dimensions[].score", "min": 0, "max": 10},
        {"path": "score.total", "min": 0, "max": 120},
    ],
    "array_length": [
        {"path": "score.dimensions", "exact": 1},
    ],
    "exact_ids": [
        {"path": "score.dimensions[].id", "values": ["D1"]},
    ],
    "equals": [
        {"left": "score.total", "right": {"sum": "score.dimensions[].score"}},
    ],
}


class TestConstraintValidation:
    def test_valid_artifact_with_constraints_passes(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 8,
                "max": 120,
                "recommendation": "send",
                "dimensions": [{"id": "D1", "name": "L", "score": 8, "justification": "ok"}],
            },
        }
        validate_artifact_contract(
            json.dumps(artifact).encode(),
            MEETING_BRIEFING_SCHEMA,
            MEETING_BRIEFING_CONSTRAINTS,
        )

    def test_enum_violation_in_recommendation_raises(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 8,
                "max": 120,
                "recommendation": "maybe",
                "dimensions": [{"id": "D1", "name": "L", "score": 8, "justification": "ok"}],
            },
        }
        with pytest.raises(ContractViolation, match="Enum violation at score.recommendation"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_enum_violation_in_data_quality_raises(self):
        artifact = {
            **_BASE_MB,
            "data_quality": {"agenda": "excellent", "fiscal": "high", "platform": "high", "overall": "high"},
            "score": {
                "total": 8,
                "max": 120,
                "recommendation": "send",
                "dimensions": [{"id": "D1", "name": "L", "score": 8, "justification": "ok"}],
            },
        }
        with pytest.raises(ContractViolation, match="Enum violation at data_quality.agenda"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_range_violation_dimension_score_above_max_raises(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 11,
                "max": 120,
                "recommendation": "send",
                "dimensions": [{"id": "D1", "name": "L", "score": 11, "justification": "ok"}],
            },
        }
        with pytest.raises(ContractViolation, match="Range violation at score.dimensions\\[0\\].score"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_range_violation_score_total_above_max_raises(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 200,
                "max": 120,
                "recommendation": "send",
                "dimensions": [{"id": "D1", "name": "L", "score": 8, "justification": "ok"}],
            },
        }
        with pytest.raises(ContractViolation, match="Range violation at score.total"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_array_length_exact_violation_raises(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 16,
                "max": 120,
                "recommendation": "send",
                "dimensions": [
                    {"id": "D1", "name": "L", "score": 8, "justification": "ok"},
                    {"id": "D2", "name": "F", "score": 8, "justification": "ok"},
                ],
            },
        }
        with pytest.raises(ContractViolation, match="Array length violation at score.dimensions"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_exact_ids_violation_raises(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 8,
                "max": 120,
                "recommendation": "send",
                "dimensions": [{"id": "wrong_id", "name": "L", "score": 8, "justification": "ok"}],
            },
        }
        with pytest.raises(ContractViolation, match="Exact-ids violation"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_equals_sum_violation_raises(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 99,
                "max": 120,
                "recommendation": "send",
                "dimensions": [{"id": "D1", "name": "L", "score": 8, "justification": "ok"}],
            },
        }
        with pytest.raises(ContractViolation, match="Equals violation at score.total"):
            validate_artifact_contract(
                json.dumps(artifact).encode(),
                MEETING_BRIEFING_SCHEMA,
                MEETING_BRIEFING_CONSTRAINTS,
            )

    def test_equals_count_violation_raises(self):
        schema = {
            "summary": {"count": "number"},
            "items": [{"name": "string"}],
        }
        constraints = {
            "equals": [{"left": "summary.count", "right": {"count": "items"}}],
        }
        artifact = {"summary": {"count": 5}, "items": [{"name": "a"}, {"name": "b"}]}
        with pytest.raises(ContractViolation, match="Equals violation at summary.count"):
            validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)

    def test_equals_count_passes_when_equal(self):
        schema = {
            "summary": {"count": "number"},
            "items": [{"name": "string"}],
        }
        constraints = {
            "equals": [{"left": "summary.count", "right": {"count": "items"}}],
        }
        artifact = {"summary": {"count": 2}, "items": [{"name": "a"}, {"name": "b"}]}
        validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)

    def test_constraints_none_skipped(self):
        artifact = {
            **_BASE_MB,
            "score": {
                "total": 200,
                "max": 9999,
                "recommendation": "garbage",
                "dimensions": [{"id": "x", "name": "L", "score": 99, "justification": "ok"}],
            },
        }
        validate_artifact_contract(json.dumps(artifact).encode(), MEETING_BRIEFING_SCHEMA, None)

    def test_enum_violation_with_array_path_includes_index(self):
        schema = {"items": [{"status": "string"}]}
        constraints = {"enums": [{"path": "items[].status", "values": ["ok", "bad"]}]}
        artifact = {"items": [{"status": "ok"}, {"status": "weird"}]}
        with pytest.raises(ContractViolation, match="items\\[1\\].status"):
            validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)

    def test_array_length_min_violation_raises(self):
        schema = {"peer_cities": [{"name": "string"}]}
        constraints = {"array_length": [{"path": "peer_cities", "min": 3}]}
        artifact = {"peer_cities": [{"name": "A"}, {"name": "B"}]}
        with pytest.raises(ContractViolation, match="expected min 3"):
            validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)

    def test_array_length_max_violation_raises(self):
        schema = {"peer_cities": [{"name": "string"}]}
        constraints = {"array_length": [{"path": "peer_cities", "max": 2}]}
        artifact = {"peer_cities": [{"name": "A"}, {"name": "B"}, {"name": "C"}]}
        with pytest.raises(ContractViolation, match="expected max 2"):
            validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)

    def test_array_length_with_bracket_path_validates_all_matches(self):
        """array_length through [] must check every resolved array, not just the first."""
        schema = {"issues": [{"sources": [{"id": "number"}]}]}
        constraints = {"array_length": [{"path": "issues[].sources", "min": 2}]}
        artifact = {
            "issues": [
                {"sources": [{"id": 1}, {"id": 2}]},
                {"sources": [{"id": 3}]},
            ],
        }
        with pytest.raises(ContractViolation, match="issues\\[1\\].sources"):
            validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)


class TestCollectContractErrors:
    """collect_contract_errors returns every validation error instead of raising on the first.

    Used by the in-container validator script so the agent can fix multiple
    errors per run instead of one at a time.
    """

    def test_valid_artifact_returns_empty_list(self):
        schema = {"greeting": "string", "count": "number"}
        artifact = json.dumps({"greeting": "hello", "count": 5}).encode()
        assert collect_contract_errors(artifact, schema) == []

    def test_none_schema_returns_empty_list(self):
        assert collect_contract_errors(b"anything at all", None) == []

    def test_invalid_json_returns_single_json_error(self):
        errors = collect_contract_errors(b"not json", {"x": "string"})
        assert len(errors) == 1
        assert "Invalid JSON" in errors[0]

    def test_non_object_root_returns_single_error(self):
        errors = collect_contract_errors(b"[1, 2, 3]", {"x": "string"})
        assert len(errors) == 1
        assert "JSON object" in errors[0]

    def test_collects_multiple_missing_fields(self):
        schema = {"a": "string", "b": "number", "c": "boolean"}
        errors = collect_contract_errors(b"{}", schema)
        assert len(errors) == 3
        joined = " | ".join(errors)
        assert "a" in joined and "b" in joined and "c" in joined

    def test_collects_missing_and_wrong_type_together(self):
        schema = {"name": "string", "count": "number"}
        artifact = json.dumps({"name": 42}).encode()
        errors = collect_contract_errors(artifact, schema)
        assert len(errors) == 2
        assert any("name" in e and "string" in e for e in errors)
        assert any("count" in e and "Missing" in e for e in errors)

    def test_collects_errors_from_nested_objects(self):
        schema = {"district": {"state": "string", "name": "string"}}
        artifact = json.dumps({"district": {}}).encode()
        errors = collect_contract_errors(artifact, schema)
        assert len(errors) == 2
        assert any("district.state" in e for e in errors)
        assert any("district.name" in e for e in errors)

    def test_collects_errors_from_array_items(self):
        schema = {"segments": [{"name": "string", "count": "number"}]}
        artifact = json.dumps({
            "segments": [
                {"name": "ok", "count": 1},
                {"name": 99},
            ]
        }).encode()
        errors = collect_contract_errors(artifact, schema)
        assert len(errors) == 2
        assert any("segments[1].name" in e for e in errors)
        assert any("segments[1].count" in e for e in errors)

    def test_collects_constraint_errors_alongside_schema_errors(self):
        schema = {"score": "number", "tier": "string"}
        constraints = {"enums": [{"path": "tier", "values": ["bronze", "silver", "gold"]}]}
        artifact = json.dumps({"score": "bad", "tier": "platinum"}).encode()
        errors = collect_contract_errors(artifact, schema, constraints)
        assert len(errors) >= 2
        assert any("score" in e for e in errors)
        assert any("tier" in e and "platinum" in e for e in errors)

    def test_does_not_mutate_schema(self):
        schema = {"greeting": "string"}
        schema_before = json.dumps(schema)
        collect_contract_errors(b'{"greeting": 5}', schema)
        assert json.dumps(schema) == schema_before
