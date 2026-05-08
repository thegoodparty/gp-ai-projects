"""Parity tests for the in-container validator script.

`_VALIDATOR_SCRIPT` (in `runner/main.py`) is a thin shim that imports
`collect_contract_errors` from `runner.contract` and runs it via subprocess
against `/workspace/output/*.json`. There is one validation implementation —
this suite exercises the shim's subprocess plumbing (file discovery, exit
codes, stdout format) by running the heredoc against the same cases the
canonical `contract.py` is run against, and asserting the pass/fail verdict
matches.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from pmf_engine.runner.contract import validate_artifact_contract, ContractViolation
from pmf_engine.runner.main import _VALIDATOR_SCRIPT


def _run_heredoc_validator(data: dict, schema: dict) -> tuple[bool, str]:
    """Write the heredoc script + data to a temp dir and run it as a subprocess."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir)

        with open(os.path.join(output_dir, "artifact.json"), "w") as f:
            json.dump(data, f)

        with open(os.path.join(tmpdir, "contract_schema.json"), "w") as f:
            json.dump(schema, f)

        with open(os.path.join(tmpdir, "validate_output.py"), "w") as f:
            f.write(_VALIDATOR_SCRIPT)

        result = subprocess.run(
            [sys.executable, os.path.join(tmpdir, "validate_output.py")],
            capture_output=True,
            text=True,
            cwd=tmpdir,
        )
        passed = result.returncode == 0
        output = result.stdout + result.stderr
        return passed, output


def _run_contract_py(data: dict, schema: dict) -> tuple[bool, str]:
    """Run contract.py's validate_artifact_contract and return (passed, error_msg)."""
    try:
        validate_artifact_contract(json.dumps(data).encode(), schema)
        return True, ""
    except ContractViolation as e:
        return False, str(e)


def _OBJ(required, **props):
    return {"type": "object", "required": required, "properties": props}

_CASES = [
    (
        "basic_schema_pass",
        {"name": "Alice", "age": 30},
        _OBJ(["name", "age"], name={"type": "string"}, age={"type": "number"}),
        True,
    ),
    (
        "missing_field_fails",
        {"name": "Alice"},
        _OBJ(["name", "age"], name={"type": "string"}, age={"type": "number"}),
        False,
    ),
    (
        "wrong_type_fails",
        {"name": "Alice", "age": "thirty"},
        _OBJ(["name", "age"], name={"type": "string"}, age={"type": "number"}),
        False,
    ),
    (
        "nested_object_pass",
        {"user": {"name": "Bob", "active": True}},
        _OBJ(
            ["user"],
            user=_OBJ(["name", "active"], name={"type": "string"}, active={"type": "boolean"}),
        ),
        True,
    ),
    (
        "nested_missing_field_fails",
        {"user": {"name": "Bob"}},
        _OBJ(
            ["user"],
            user=_OBJ(["name", "active"], name={"type": "string"}, active={"type": "boolean"}),
        ),
        False,
    ),
    (
        "array_of_objects_pass",
        {"items": [{"id": 1}, {"id": 2}]},
        _OBJ(
            ["items"],
            items={"type": "array", "items": _OBJ(["id"], id={"type": "number"})},
        ),
        True,
    ),
    (
        "empty_array_fails",
        {"items": []},
        _OBJ(
            ["items"],
            items={"type": "array", "minItems": 1, "items": _OBJ(["id"], id={"type": "number"})},
        ),
        False,
    ),
    (
        "enum_pass",
        {"status": "active"},
        _OBJ(["status"], status={"type": "string", "enum": ["active", "inactive"]}),
        True,
    ),
    (
        "enum_fails",
        {"status": "deleted"},
        _OBJ(["status"], status={"type": "string", "enum": ["active", "inactive"]}),
        False,
    ),
    (
        "range_pass",
        {"score": 85},
        _OBJ(["score"], score={"type": "number", "minimum": 0, "maximum": 100}),
        True,
    ),
    (
        "range_fails",
        {"score": 150},
        _OBJ(["score"], score={"type": "number", "minimum": 0, "maximum": 100}),
        False,
    ),
    (
        "array_min_items_pass",
        {"items": [{"id": 1}, {"id": 2}]},
        _OBJ(
            ["items"],
            items={
                "type": "array",
                "minItems": 2,
                "items": _OBJ(["id"], id={"type": "number"}),
            },
        ),
        True,
    ),
    (
        "array_min_items_fails",
        {"items": [{"id": 1}]},
        _OBJ(
            ["items"],
            items={
                "type": "array",
                "minItems": 2,
                "items": _OBJ(["id"], id={"type": "number"}),
            },
        ),
        False,
    ),
    (
        "nested_array_min_items_fails_second_item",
        # Restoring the bracket-path coverage from the deleted constraints
        # engine: validation must check minItems on EVERY nested sources[]
        # array, not just the first. Old constraints API used path
        # "issues[].sources" + min:2; Draft-07 expresses the same as
        # nested items.properties.sources.minItems=2.
        {
            "issues": [
                {"sources": [{"id": 1}, {"id": 2}]},
                {"sources": [{"id": 3}]},
            ],
        },
        _OBJ(
            ["issues"],
            issues={
                "type": "array",
                "items": _OBJ(
                    ["sources"],
                    sources={
                        "type": "array",
                        "minItems": 2,
                        "items": _OBJ(["id"], id={"type": "number"}),
                    },
                ),
            },
        ),
        False,
    ),
    (
        "nested_array_min_items_pass",
        {
            "issues": [
                {"sources": [{"id": 1}, {"id": 2}]},
                {"sources": [{"id": 3}, {"id": 4}]},
            ],
        },
        _OBJ(
            ["issues"],
            issues={
                "type": "array",
                "items": _OBJ(
                    ["sources"],
                    sources={
                        "type": "array",
                        "minItems": 2,
                        "items": _OBJ(["id"], id={"type": "number"}),
                    },
                ),
            },
        ),
        True,
    ),
]


class TestValidatorParity:
    @pytest.mark.parametrize(
        "name,data,schema,should_pass",
        _CASES,
        ids=[c[0] for c in _CASES],
    )
    def test_heredoc_matches_contract_py(self, name, data, schema, should_pass):
        contract_passed, contract_err = _run_contract_py(data, schema)
        heredoc_passed, heredoc_output = _run_heredoc_validator(data, schema)

        assert contract_passed == should_pass, (
            f"contract.py expected {'pass' if should_pass else 'fail'} "
            f"but got {'pass' if contract_passed else 'fail'}: {contract_err}"
        )
        assert heredoc_passed == should_pass, (
            f"heredoc expected {'pass' if should_pass else 'fail'} "
            f"but got {'pass' if heredoc_passed else 'fail'}: {heredoc_output}"
        )
        assert contract_passed == heredoc_passed, (
            f"PARITY VIOLATION on '{name}': contract.py={'pass' if contract_passed else 'fail'}, "
            f"heredoc={'pass' if heredoc_passed else 'fail'}"
        )
