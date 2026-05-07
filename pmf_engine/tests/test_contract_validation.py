"""Generic contract-validation tests.

Exercise the engine's contract module against tiny, hand-written synthetic
schemas. Per-experiment contract validation belongs in the runbooks repo.
"""
from __future__ import annotations

import json

import pytest

from pmf_engine.runner.contract import (
    ContractViolation,
    collect_contract_errors,
    format_contract_for_prompt,
    validate_artifact_contract,
)


SYNTHETIC_GP_SCHEMA = {
    "name": "string",
    "summary": {"total": "number"},
    "items": [{"id": "string", "count": "number"}],
}


SYNTHETIC_JSONSCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "summary"],
    "properties": {
        "name": {"type": "string"},
        "summary": {
            "type": "object",
            "required": ["total"],
            "properties": {"total": {"type": "number"}},
        },
    },
}


def _valid_artifact() -> dict:
    return {
        "name": "alpha",
        "summary": {"total": 42},
        "items": [{"id": "i1", "count": 1}],
    }


class TestValidateArtifactContract:
    def test_valid_artifact_passes(self):
        validate_artifact_contract(json.dumps(_valid_artifact()).encode(), SYNTHETIC_GP_SCHEMA)

    def test_missing_required_field_raises(self):
        artifact = {"summary": {"total": 1}, "items": [{"id": "i", "count": 1}]}
        with pytest.raises(ContractViolation, match="name"):
            validate_artifact_contract(json.dumps(artifact).encode(), SYNTHETIC_GP_SCHEMA)

    def test_wrong_type_raises(self):
        artifact = {"name": "ok", "summary": {"total": "not a number"}, "items": []}
        with pytest.raises(ContractViolation, match="summary.total"):
            validate_artifact_contract(json.dumps(artifact).encode(), SYNTHETIC_GP_SCHEMA)

    def test_array_item_missing_field_raises(self):
        artifact = {"name": "ok", "summary": {"total": 1}, "items": [{"id": "i1"}]}
        with pytest.raises(ContractViolation, match=r"items\[0\]"):
            validate_artifact_contract(json.dumps(artifact).encode(), SYNTHETIC_GP_SCHEMA)

    def test_invalid_json_raises(self):
        with pytest.raises(ContractViolation, match="Invalid JSON"):
            validate_artifact_contract(b"not json", SYNTHETIC_GP_SCHEMA)

    def test_non_object_json_raises(self):
        with pytest.raises(ContractViolation, match="must be a JSON object"):
            validate_artifact_contract(b'"just a string"', SYNTHETIC_GP_SCHEMA)

    def test_none_schema_skips_validation(self):
        validate_artifact_contract(b"anything", None)

    def test_empty_schema_skips_validation(self):
        validate_artifact_contract(b"anything", {})

    def test_extra_fields_are_allowed(self):
        schema = {"name": "string"}
        artifact = json.dumps({"name": "Alice", "extra": {"x": 1}}).encode()
        validate_artifact_contract(artifact, schema)

    def test_string_where_object_expected_raises(self):
        schema = {"district": {"state": "string"}}
        artifact = json.dumps({"district": "MI"}).encode()
        with pytest.raises(ContractViolation, match="district.*expected object"):
            validate_artifact_contract(artifact, schema)

    def test_string_where_array_expected_raises(self):
        schema = {"items": [{"name": "string"}]}
        artifact = json.dumps({"items": "not an array"}).encode()
        with pytest.raises(ContractViolation, match="items.*expected array"):
            validate_artifact_contract(artifact, schema)

    def test_multi_item_list_schema_is_author_error(self):
        """A schema list of length != 1 is an author bug. The validator must
        surface it instead of vacuously passing every artifact."""
        schema = {"items": [{"a": "string"}, {"b": "string"}]}
        errors = collect_contract_errors(b'{"items": [{"a": "x"}]}', schema)
        assert errors, "Expected schema-author error for multi-item list schema"
        assert any("items" in e for e in errors)


class TestCollectContractErrors:
    """collect_contract_errors returns every validation error; used by the
    in-container validator script so the agent can fix multiple errors per
    run instead of one at a time."""

    def test_valid_artifact_returns_empty_list(self):
        artifact = json.dumps({"name": "ok", "count": 5}).encode()
        assert collect_contract_errors(artifact, {"name": "string", "count": "number"}) == []

    def test_none_schema_returns_empty_list(self):
        assert collect_contract_errors(b"anything", None) == []

    def test_invalid_json_returns_single_error(self):
        errors = collect_contract_errors(b"not json", {"x": "string"})
        assert len(errors) == 1
        assert "Invalid JSON" in errors[0]

    def test_collects_multiple_missing_fields(self):
        schema = {"a": "string", "b": "number", "c": "boolean"}
        errors = collect_contract_errors(b"{}", schema)
        assert len(errors) == 3
        joined = " | ".join(errors)
        assert "a" in joined and "b" in joined and "c" in joined

    def test_collects_errors_from_nested_objects(self):
        schema = {"district": {"state": "string", "name": "string"}}
        errors = collect_contract_errors(b'{"district": {}}', schema)
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


class TestConstraintValidation:
    """Cross-field constraints (enums, ranges, array_length, equals) on top
    of the structural schema."""

    def test_enum_violation_raises(self):
        schema = {"tier": "string"}
        constraints = {"enums": [{"path": "tier", "values": ["bronze", "gold"]}]}
        artifact = json.dumps({"tier": "platinum"}).encode()
        with pytest.raises(ContractViolation, match="Enum violation at tier"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_range_violation_raises(self):
        schema = {"score": "number"}
        constraints = {"ranges": [{"path": "score", "min": 0, "max": 10}]}
        artifact = json.dumps({"score": 99}).encode()
        with pytest.raises(ContractViolation, match="Range violation at score"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_array_length_exact_violation_raises(self):
        schema = {"items": [{"name": "string"}]}
        constraints = {"array_length": [{"path": "items", "exact": 2}]}
        artifact = json.dumps({"items": [{"name": "a"}]}).encode()
        with pytest.raises(ContractViolation, match="Array length violation at items"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_array_length_min_violation_raises(self):
        schema = {"items": [{"name": "string"}]}
        constraints = {"array_length": [{"path": "items", "min": 3}]}
        artifact = json.dumps({"items": [{"name": "a"}]}).encode()
        with pytest.raises(ContractViolation, match="expected min 3"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_array_length_max_violation_raises(self):
        schema = {"items": [{"name": "string"}]}
        constraints = {"array_length": [{"path": "items", "max": 1}]}
        artifact = json.dumps({"items": [{"name": "a"}, {"name": "b"}]}).encode()
        with pytest.raises(ContractViolation, match="expected max 1"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_exact_ids_violation_raises(self):
        schema = {"items": [{"id": "string"}]}
        constraints = {"exact_ids": [{"path": "items[].id", "values": ["A", "B"]}]}
        artifact = json.dumps({"items": [{"id": "A"}, {"id": "wrong"}]}).encode()
        with pytest.raises(ContractViolation, match="Exact-ids violation"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_equals_sum_violation_raises(self):
        schema = {"total": "number", "items": [{"score": "number"}]}
        constraints = {"equals": [{"left": "total", "right": {"sum": "items[].score"}}]}
        artifact = json.dumps({"total": 99, "items": [{"score": 1}, {"score": 2}]}).encode()
        with pytest.raises(ContractViolation, match="Equals violation at total"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_equals_count_violation_raises(self):
        schema = {"summary": {"count": "number"}, "items": [{"name": "string"}]}
        constraints = {"equals": [{"left": "summary.count", "right": {"count": "items"}}]}
        artifact = json.dumps({"summary": {"count": 5}, "items": [{"name": "a"}, {"name": "b"}]}).encode()
        with pytest.raises(ContractViolation, match="Equals violation at summary.count"):
            validate_artifact_contract(artifact, schema, constraints)

    def test_equals_count_passes_when_equal(self):
        schema = {"summary": {"count": "number"}, "items": [{"name": "string"}]}
        constraints = {"equals": [{"left": "summary.count", "right": {"count": "items"}}]}
        artifact = json.dumps({"summary": {"count": 2}, "items": [{"name": "a"}, {"name": "b"}]}).encode()
        validate_artifact_contract(artifact, schema, constraints)

    def test_constraints_none_skipped(self):
        validate_artifact_contract(json.dumps(_valid_artifact()).encode(), SYNTHETIC_GP_SCHEMA, None)

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
        with pytest.raises(ContractViolation, match=r"issues\[1\].sources"):
            validate_artifact_contract(json.dumps(artifact).encode(), schema, constraints)


class TestFormatContractForPrompt:
    def test_formats_schema_as_readable_spec(self):
        schema = {"name": "string", "count": "number", "items": [{"id": "string"}]}
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

    def test_renders_jsonschema_as_simple_example(self):
        """Agent prompt should still see the friendly {field: type} shape, not raw JSON Schema."""
        rendered = format_contract_for_prompt(SYNTHETIC_JSONSCHEMA)
        assert "name" in rendered
        assert "string" in rendered
        assert '"properties"' not in rendered
        assert '"required"' not in rendered


class TestJsonSchemaDraft7:
    """The new manifest format uses JSON Schema Draft-07 directly."""

    def test_valid_artifact_returns_no_errors(self):
        artifact = {"name": "ok", "summary": {"total": 1}}
        assert collect_contract_errors(json.dumps(artifact).encode(), SYNTHETIC_JSONSCHEMA) == []

    def test_missing_required_root_field_reports_error(self):
        artifact = {"summary": {"total": 1}}
        errors = collect_contract_errors(json.dumps(artifact).encode(), SYNTHETIC_JSONSCHEMA)
        assert any("name" in e for e in errors), errors

    def test_wrong_type_reports_error_with_path(self):
        artifact = {"name": "ok", "summary": {"total": "not-a-number"}}
        errors = collect_contract_errors(json.dumps(artifact).encode(), SYNTHETIC_JSONSCHEMA)
        assert any("summary.total" in e for e in errors), errors

    def test_constraints_run_alongside_jsonschema(self):
        schema = {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                },
            },
        }
        constraints = {"array_length": [{"path": "items", "min": 2}]}
        artifact = {"items": [{"name": "only-one"}]}
        errors = collect_contract_errors(json.dumps(artifact).encode(), schema, constraints)
        assert any("items" in e and ("min" in e or "length" in e) for e in errors), errors

    def test_legacy_gpshape_still_works(self):
        """Backward-compat: existing GP-shape manifests must still validate."""
        gp_shape = {"name": "string", "summary": {"total": "number"}}
        artifact = {"name": "ok", "summary": {"total": 1}}
        assert collect_contract_errors(json.dumps(artifact).encode(), gp_shape) == []

        bad = {"name": 99, "summary": {"total": "x"}}
        errors = collect_contract_errors(json.dumps(bad).encode(), gp_shape)
        assert errors


class TestPrimitiveTypeChecks:
    def test_float_accepted_for_number_type(self):
        validate_artifact_contract(b'{"score": 99.7}', {"score": "number"})

    def test_boolean_rejected_for_number_type(self):
        with pytest.raises(ContractViolation, match="count"):
            validate_artifact_contract(b'{"count": true}', {"count": "number"})

    def test_null_value_rejected_for_string_field(self):
        with pytest.raises(ContractViolation, match="name"):
            validate_artifact_contract(b'{"name": null}', {"name": "string"})

    def test_null_value_rejected_for_nested_object(self):
        with pytest.raises(ContractViolation, match="district"):
            validate_artifact_contract(b'{"district": null}', {"district": {"state": "string"}})

    def test_array_of_primitives_valid(self):
        validate_artifact_contract(b'{"tags": ["a", "b"]}', {"tags": ["string"]})

    def test_array_of_primitives_wrong_item_type(self):
        with pytest.raises(ContractViolation, match=r"tags\[1\]"):
            validate_artifact_contract(b'{"tags": ["a", 42]}', {"tags": ["string"]})
