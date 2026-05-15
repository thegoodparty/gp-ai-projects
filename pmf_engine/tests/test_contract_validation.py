"""Generic contract-validation tests.

Exercise the engine's contract module against tiny, hand-written synthetic
JSON Schema Draft-07 schemas. Per-experiment contract validation belongs in
the runbooks repo.
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


SYNTHETIC_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "required": ["name", "summary", "items"],
    "properties": {
        "name": {"type": "string"},
        "summary": {
            "type": "object",
            "required": ["total"],
            "properties": {"total": {"type": "number"}},
        },
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "count"],
                "properties": {
                    "id": {"type": "string"},
                    "count": {"type": "number"},
                },
            },
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
        validate_artifact_contract(json.dumps(_valid_artifact()).encode(), SYNTHETIC_SCHEMA)

    def test_missing_required_field_raises(self):
        artifact = {"summary": {"total": 1}, "items": [{"id": "i", "count": 1}]}
        with pytest.raises(ContractViolation, match="name"):
            validate_artifact_contract(json.dumps(artifact).encode(), SYNTHETIC_SCHEMA)

    def test_wrong_type_raises(self):
        artifact = {"name": "ok", "summary": {"total": "not a number"}, "items": [{"id": "i", "count": 1}]}
        with pytest.raises(ContractViolation, match="summary.total"):
            validate_artifact_contract(json.dumps(artifact).encode(), SYNTHETIC_SCHEMA)

    def test_array_item_missing_field_raises(self):
        artifact = {"name": "ok", "summary": {"total": 1}, "items": [{"id": "i1"}]}
        with pytest.raises(ContractViolation, match=r"items\[0\]"):
            validate_artifact_contract(json.dumps(artifact).encode(), SYNTHETIC_SCHEMA)

    def test_invalid_json_raises(self):
        with pytest.raises(ContractViolation, match="Invalid JSON"):
            validate_artifact_contract(b"not json", SYNTHETIC_SCHEMA)

    def test_non_object_json_raises(self):
        with pytest.raises(ContractViolation, match="must be a JSON object"):
            validate_artifact_contract(b'"just a string"', SYNTHETIC_SCHEMA)

    def test_none_schema_skips_validation(self):
        validate_artifact_contract(b"anything", None)

    def test_empty_schema_skips_validation(self):
        validate_artifact_contract(b"anything", {})


class TestCollectContractErrors:
    """collect_contract_errors returns every validation error; used by the
    in-container validator script so the agent can fix multiple errors per
    run instead of one at a time."""

    def test_valid_artifact_returns_empty_list(self):
        schema = {
            "type": "object",
            "required": ["name", "count"],
            "properties": {"name": {"type": "string"}, "count": {"type": "number"}},
        }
        artifact = json.dumps({"name": "ok", "count": 5}).encode()
        assert collect_contract_errors(artifact, schema) == []

    def test_none_schema_returns_empty_list(self):
        assert collect_contract_errors(b"anything", None) == []

    def test_invalid_json_returns_single_error(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        errors = collect_contract_errors(b"not json", schema)
        assert len(errors) == 1
        assert "Invalid JSON" in errors[0]

    def test_collects_multiple_missing_fields(self):
        schema = {
            "type": "object",
            "required": ["a", "b", "c"],
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "number"},
                "c": {"type": "boolean"},
            },
        }
        errors = collect_contract_errors(b"{}", schema)
        assert len(errors) == 3
        joined = " | ".join(errors)
        assert "a" in joined and "b" in joined and "c" in joined

    def test_collects_errors_from_nested_objects(self):
        schema = {
            "type": "object",
            "required": ["district"],
            "properties": {
                "district": {
                    "type": "object",
                    "required": ["state", "name"],
                    "properties": {
                        "state": {"type": "string"},
                        "name": {"type": "string"},
                    },
                },
            },
        }
        errors = collect_contract_errors(b'{"district": {}}', schema)
        assert len(errors) == 2
        # Loose on punctuation (jsonschema versions vary on quote style for
        # required-property errors), tight on path + field name presence.
        assert any("district" in e and "state" in e for e in errors)
        assert any("district" in e and "name" in e for e in errors)

    def test_collects_errors_from_array_items(self):
        schema = {
            "type": "object",
            "required": ["segments"],
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "count"],
                        "properties": {
                            "name": {"type": "string"},
                            "count": {"type": "number"},
                        },
                    },
                },
            },
        }
        artifact = json.dumps({
            "segments": [
                {"name": "ok", "count": 1},
                {"name": 99},
            ]
        }).encode()
        errors = collect_contract_errors(artifact, schema)
        assert len(errors) == 2
        assert any("segments[1].name" in e for e in errors)
        assert any("segments[1]" in e and "count" in e for e in errors)

    def test_does_not_mutate_schema(self):
        schema = {
            "type": "object",
            "required": ["greeting"],
            "properties": {"greeting": {"type": "string"}},
        }
        schema_before = json.dumps(schema)
        collect_contract_errors(b'{"greeting": 5}', schema)
        assert json.dumps(schema) == schema_before


class TestFormatContractForPrompt:
    def test_formats_schema_as_readable_spec(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "number"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                },
            },
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

    def test_renders_jsonschema_as_simple_example(self):
        """Agent prompt should see the friendly {field: type} shape, not raw JSON Schema."""
        rendered = format_contract_for_prompt(SYNTHETIC_SCHEMA)
        assert "name" in rendered
        assert "string" in rendered
        assert '"properties"' not in rendered
        assert '"required"' not in rendered

    def test_renders_enum_values_for_enum_only_field(self):
        """When a field has `enum` without explicit `type`, render the allowed
        values so the agent learns what to produce. Falling through to literal
        "string" hides the constraint and burns turns."""
        schema = {
            "type": "object",
            "properties": {
                "tier": {"enum": ["bronze", "silver", "gold"]},
            },
        }
        rendered = format_contract_for_prompt(schema)
        assert "bronze" in rendered
        assert "silver" in rendered
        assert "gold" in rendered

    def test_renders_enum_values_for_typed_enum_field(self):
        """Same when `enum` is specified alongside `type: string`."""
        schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "done"]},
            },
        }
        rendered = format_contract_for_prompt(schema)
        assert "active" in rendered
        assert "done" in rendered


class TestPrimitiveTypeChecks:
    def _wrap(self, field_schema: dict, required: bool = True) -> dict:
        s = {"type": "object", "properties": {"score": field_schema}}
        if required:
            s["required"] = ["score"]
        return s

    def test_float_accepted_for_number_type(self):
        validate_artifact_contract(b'{"score": 99.7}', self._wrap({"type": "number"}))

    def test_boolean_rejected_for_number_type(self):
        # Word-boundary match on the field name — `match="x"` would
        # match "extra", "context", etc. and pass on any error string.
        with pytest.raises(ContractViolation, match=r"^score:"):
            validate_artifact_contract(b'{"score": true}', self._wrap({"type": "number"}))

    def test_null_value_rejected_for_string_field(self):
        with pytest.raises(ContractViolation, match=r"^score:"):
            validate_artifact_contract(b'{"score": null}', self._wrap({"type": "string"}))

    def test_array_of_primitives_valid(self):
        schema = self._wrap({"type": "array", "items": {"type": "string"}})
        validate_artifact_contract(b'{"score": ["a", "b"]}', schema)

    def test_array_of_primitives_wrong_item_type(self):
        schema = self._wrap({"type": "array", "items": {"type": "string"}})
        with pytest.raises(ContractViolation, match=r"score\[1\]"):
            validate_artifact_contract(b'{"score": ["a", 42]}', schema)
