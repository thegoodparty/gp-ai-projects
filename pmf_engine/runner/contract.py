from __future__ import annotations

import json


class ContractViolation(Exception):
    pass


TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def validate_artifact_contract(
    artifact_bytes: bytes,
    schema: dict | None,
    constraints: dict | None = None,
) -> None:
    errors = collect_contract_errors(artifact_bytes, schema, constraints)
    if errors:
        raise ContractViolation("; ".join(errors))


def collect_contract_errors(
    artifact_bytes: bytes,
    schema: dict | None,
    constraints: dict | None = None,
) -> list[str]:
    """Validate artifact JSON against schema + constraints, returning every error.

    Unlike validate_artifact_contract, this does not raise — it returns a list
    so callers (e.g., the in-container validator script) can present every
    violation to the agent in one pass instead of one at a time.
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

    errors: list[str] = []
    _collect_object_errors(data, schema, "", errors)

    if constraints:
        errors.extend(_check_constraints(data, constraints))

    return errors


def _collect_object_errors(data: dict, schema: dict, path: str, errors: list[str]) -> None:
    for key, expected in schema.items():
        full_path = f"{path}.{key}" if path else key

        if key not in data:
            errors.append(f"Missing required field: {full_path}")
            continue

        value = data[key]

        if isinstance(expected, str):
            checker = TYPE_CHECKS.get(expected)
            if checker and not checker(value):
                errors.append(
                    f"Wrong type for {full_path}: expected {expected}, "
                    f"got {type(value).__name__} (value: {repr(value)[:80]})"
                )

        elif isinstance(expected, dict):
            if not isinstance(value, dict):
                errors.append(
                    f"Wrong type for {full_path}: expected object, "
                    f"got {type(value).__name__}"
                )
            else:
                _collect_object_errors(value, expected, full_path, errors)

        elif isinstance(expected, list):
            if len(expected) != 1:
                errors.append(
                    f"Schema error at {full_path}: array schemas must contain exactly "
                    f"one item template, got {len(expected)}"
                )
                continue
            if not isinstance(value, list):
                errors.append(
                    f"Wrong type for {full_path}: expected array, "
                    f"got {type(value).__name__}"
                )
                continue
            if len(value) == 0:
                errors.append(f"Empty array: {full_path}")
                continue

            item_schema = expected[0]
            if isinstance(item_schema, dict):
                for i, item in enumerate(value):
                    if not isinstance(item, dict):
                        errors.append(
                            f"Wrong type for {full_path}[{i}]: expected object, "
                            f"got {type(item).__name__}"
                        )
                    else:
                        _collect_object_errors(item, item_schema, f"{full_path}[{i}]", errors)
            elif isinstance(item_schema, str):
                checker = TYPE_CHECKS.get(item_schema)
                if checker:
                    for i, item in enumerate(value):
                        if not checker(item):
                            errors.append(
                                f"Wrong type for {full_path}[{i}]: expected {item_schema}, "
                                f"got {type(item).__name__}"
                            )


def _resolve_path(data, path: str):
    """Walk a dotted path with optional `[]` array iteration.

    Returns a list of (concrete_path, value) tuples.
    """
    results: list[tuple[str, object]] = []
    _walk(data, path.split("."), 0, "", results)
    return results


def _walk(current, segments, idx, concrete, out):
    if idx == len(segments):
        out.append((concrete, current))
        return
    segment = segments[idx]
    if segment.endswith("[]"):
        key = segment[:-2]
        if key:
            if not isinstance(current, dict) or key not in current:
                return
            current = current[key]
        if not isinstance(current, list):
            return
        base = f"{concrete}.{key}" if concrete and key else (key if not concrete else concrete)
        for i, item in enumerate(current):
            item_concrete = f"{base}[{i}]" if key or concrete else f"[{i}]"
            _walk(item, segments, idx + 1, item_concrete, out)
    else:
        if not isinstance(current, dict) or segment not in current:
            return
        next_concrete = f"{concrete}.{segment}" if concrete else segment
        _walk(current[segment], segments, idx + 1, next_concrete, out)


def _resolve_single(data, path):
    matches = _resolve_path(data, path)
    if not matches:
        return None, False
    return matches[0][1], True


def _check_constraints(data, constraints):
    errors: list[str] = []

    for rule in constraints.get("enums", []):
        path = rule["path"]
        allowed = set(rule["values"])
        matches = _resolve_path(data, path)
        if not matches:
            errors.append(f"Enum path not found: {path}")
            continue
        for concrete, value in matches:
            if value not in allowed:
                errors.append(
                    f"Enum violation at {concrete}: got {value!r}, expected one of {sorted(allowed)}"
                )

    for rule in constraints.get("ranges", []):
        path = rule["path"]
        lo = rule.get("min")
        hi = rule.get("max")
        matches = _resolve_path(data, path)
        if not matches:
            errors.append(f"Range path not found: {path}")
            continue
        for concrete, value in matches:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"Range target at {concrete} is not numeric: {value!r}")
                continue
            if lo is not None and value < lo:
                errors.append(f"Range violation at {concrete}: {value} < min {lo}")
            if hi is not None and value > hi:
                errors.append(f"Range violation at {concrete}: {value} > max {hi}")

    for rule in constraints.get("array_length", []):
        path = rule["path"]
        matches = _resolve_path(data, path)
        if not matches:
            errors.append(f"Array length path not found: {path}")
            continue
        for concrete, value in matches:
            if not isinstance(value, list):
                errors.append(f"Array length target at {concrete} is not a list: {type(value).__name__}")
                continue
            length = len(value)
            if "exact" in rule and length != rule["exact"]:
                errors.append(
                    f"Array length violation at {concrete}: got {length}, expected exactly {rule['exact']}"
                )
            if "min" in rule and length < rule["min"]:
                errors.append(
                    f"Array length violation at {concrete}: got {length}, expected min {rule['min']}"
                )
            if "max" in rule and length > rule["max"]:
                errors.append(
                    f"Array length violation at {concrete}: got {length}, expected max {rule['max']}"
                )

    for rule in constraints.get("exact_ids", []):
        path = rule["path"]
        expected = list(rule["values"])
        matches = _resolve_path(data, path)
        if not matches:
            errors.append(f"Exact-ids path not found: {path}")
            continue
        actual = [v for _, v in matches]
        if sorted(actual) != sorted(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            parts = []
            if missing:
                parts.append(f"missing {missing}")
            if extra:
                parts.append(f"unexpected {extra}")
            if len(actual) != len(expected):
                parts.append(f"got {len(actual)}, expected {len(expected)}")
            errors.append(f"Exact-ids violation at {path}: " + "; ".join(parts))

    for rule in constraints.get("equals", []):
        left_path = rule["left"]
        right = rule["right"]
        left_value, found = _resolve_single(data, left_path)
        if not found:
            errors.append(f"Equals left path not found: {left_path}")
            continue
        right_value = _evaluate_right(data, right, errors, left_path)
        if right_value is None:
            continue
        if left_value != right_value:
            errors.append(
                f"Equals violation at {left_path}: left={left_value}, right={right_value} "
                f"(right expression: {right})"
            )

    return errors


def _evaluate_right(data, right, errors, left_path):
    if isinstance(right, (int, float, str, bool)):
        return right
    if isinstance(right, dict):
        if "count" in right:
            path = right["count"]
            value, found = _resolve_single(data, path)
            if not found or not isinstance(value, list):
                errors.append(f"Equals right count path not a list: {path}")
                return None
            return len(value)
        if "sum" in right:
            path = right["sum"]
            matches = _resolve_path(data, path)
            if not matches:
                errors.append(f"Equals right sum path not found: {path}")
                return None
            total = 0
            for concrete, value in matches:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    errors.append(f"Equals right sum non-numeric at {concrete}: {value!r}")
                    return None
                total += value
            return total
    errors.append(f"Equals right expression not understood for {left_path}: {right!r}")
    return None


def format_contract_for_prompt(schema: dict | None, constraints: dict | None = None) -> str:
    if not schema:
        return ""

    lines = ["## OUTPUT CONTRACT", "", "Your output JSON MUST match this exact shape. Missing or wrong-typed fields = FAILURE.", ""]
    lines.append("```json")
    lines.append(_schema_to_example(schema, indent=0))
    lines.append("```")
    lines.append("")
    lines.append("Field types: `string`, `number`, `boolean`. Arrays marked with `[...]` must have at least one item.")

    if constraints:
        lines.append("")
        lines.append("## FIELD CONSTRAINTS")
        lines.append("")
        lines.append("In addition to the shape above, these rules MUST hold:")
        lines.append("")
        for rule in constraints.get("enums", []):
            lines.append(f"- `{rule['path']}` must be one of: {', '.join(repr(v) for v in rule['values'])}")
        for rule in constraints.get("ranges", []):
            parts = []
            if "min" in rule:
                parts.append(f">= {rule['min']}")
            if "max" in rule:
                parts.append(f"<= {rule['max']}")
            lines.append(f"- `{rule['path']}` must be {' and '.join(parts)}")
        for rule in constraints.get("array_length", []):
            if "exact" in rule:
                lines.append(f"- `{rule['path']}` must have exactly {rule['exact']} items")
            else:
                parts = []
                if "min" in rule:
                    parts.append(f">= {rule['min']}")
                if "max" in rule:
                    parts.append(f"<= {rule['max']}")
                lines.append(f"- `{rule['path']}` length must be {' and '.join(parts)}")
        for rule in constraints.get("exact_ids", []):
            lines.append(f"- `{rule['path']}` must contain exactly these values (no more, no less): {rule['values']}")
        for rule in constraints.get("equals", []):
            right = rule["right"]
            if isinstance(right, dict) and "count" in right:
                right_desc = f"len({right['count']})"
            elif isinstance(right, dict) and "sum" in right:
                right_desc = f"sum of {right['sum']}"
            else:
                right_desc = repr(right)
            lines.append(f"- `{rule['left']}` must equal {right_desc}")

    return "\n".join(lines)


def _schema_to_example(schema: dict | list | str, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(schema, str):
        return f'"{schema}"'
    if isinstance(schema, list) and len(schema) == 1:
        inner = _schema_to_example(schema[0], indent + 1)
        if isinstance(schema[0], dict):
            return f"[\n{inner}\n{pad}]"
        return f"[{inner}]"
    if isinstance(schema, dict):
        if not schema:
            return "{}"
        lines = [f"{pad}{{"]
        items = list(schema.items())
        for i, (key, val) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            val_str = _schema_to_example(val, indent + 1)
            lines.append(f"{pad}  \"{key}\": {val_str}{comma}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    return str(schema)
