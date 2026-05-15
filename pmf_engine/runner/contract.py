from __future__ import annotations

import json

from jsonschema import Draft7Validator

from pmf_engine.control_plane.jsonschema_errors import format_validation_errors


class ContractViolation(Exception):
    pass


def validate_artifact_contract(artifact_bytes: bytes, schema: dict | None) -> None:
    errors = collect_contract_errors(artifact_bytes, schema)
    if errors:
        raise ContractViolation("; ".join(errors))


def collect_contract_errors(artifact_bytes: bytes, schema: dict | None) -> list[str]:
    """Validate artifact JSON against a JSON Schema Draft-07 schema.

    Returns every error so callers (e.g. the in-container validator script)
    can present all violations to the agent in one pass instead of one at a
    time. Empty list = valid artifact. Empty/None schema = no validation.
    """
    if not schema:
        return []

    raw = artifact_bytes or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")

    try:
        data = json.loads(raw) if raw else None
    except (json.JSONDecodeError, ValueError) as e:
        return [f"Invalid JSON ({len(raw)} bytes): {e}"]
    if data is None:
        return ["Artifact is empty"]

    if not isinstance(data, dict):
        return ["Artifact must be a JSON object"]

    return _collect_jsonschema_errors(data, schema)


def _collect_jsonschema_errors(data: dict, schema: dict) -> list[str]:
    return format_validation_errors(Draft7Validator(schema), data)


def _jsonschema_to_example(schema) -> dict | list | str:
    """Render a JSON Schema fragment as a friendly `{field: type}` example for
    the agent's system prompt. Validation itself goes through jsonschema."""
    if not isinstance(schema, dict):
        return schema
    if "enum" in schema:
        # Render the allowed values literally so the agent sees the constraint.
        # Without this, `{"enum": ["bronze", "gold"]}` would fall through to
        # "string" and hide the allowlist from the prompt.
        values = schema["enum"]
        return "enum: " + " | ".join(repr(v) for v in values)
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties", {})
        if not properties:
            return {}
        return {k: _jsonschema_to_example(v) for k, v in properties.items()}
    if schema_type == "array":
        items = schema.get("items", {})
        return [_jsonschema_to_example(items)]
    if schema_type in ("string", "number", "boolean"):
        return schema_type
    return "string"


def format_contract_for_prompt(schema: dict | None) -> str:
    if not schema:
        return ""

    lines = [
        "## OUTPUT CONTRACT",
        "",
        "Your output JSON MUST match this exact shape. Missing or wrong-typed fields = FAILURE.",
        "",
        "```json",
        _schema_to_example(_jsonschema_to_example(schema), indent=0),
        "```",
        "",
        "Field types: `string`, `number`, `boolean`. Arrays marked with `[...]` must have at least one item.",
    ]
    return "\n".join(lines)


def _schema_to_example(schema, indent: int) -> str:
    pad = "  " * indent
    if isinstance(schema, dict):
        if not schema:
            return "{}"
        inner = ",\n".join(
            f'{pad}  "{k}": {_schema_to_example(v, indent + 1)}'
            for k, v in schema.items()
        )
        return "{\n" + inner + "\n" + pad + "}"
    if isinstance(schema, list):
        item = _schema_to_example(schema[0], indent + 1) if schema else "..."
        return "[\n" + pad + "  " + item + "\n" + pad + "]"
    if isinstance(schema, str):
        return f'"{schema}"'
    return json.dumps(schema)
