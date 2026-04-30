"""Parity tests: ensure _VALIDATOR_SCRIPT heredoc and contract.py produce identical results.

The PMF engine has three copies of the contract validator:
1. contract.py (canonical, used by runner post-hoc)
2. _VALIDATOR_SCRIPT heredoc in main.py (written to workspace for agent self-validation)
3. runbooks/scripts/python/validate_contract.py (CLI tool for local runs)

These tests catch drift between copy #1 and #2 by running both against identical inputs.
Copy #3 is tested in test_validator_parity_runbook.py.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from pmf_engine.runner.contract import validate_artifact_contract, ContractViolation
from pmf_engine.runner.main import _VALIDATOR_SCRIPT


def _run_heredoc_validator(
    data: dict,
    schema: dict,
    constraints: dict | None = None,
) -> tuple[bool, str]:
    """Write the heredoc script + data to a temp dir and run it as a subprocess.

    Returns (passed: bool, output: str).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Mimic the workspace layout the heredoc expects
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir)

        with open(os.path.join(output_dir, "artifact.json"), "w") as f:
            json.dump(data, f)

        with open(os.path.join(tmpdir, "contract_schema.json"), "w") as f:
            json.dump(schema, f)

        if constraints:
            with open(os.path.join(tmpdir, "contract_constraints.json"), "w") as f:
                json.dump(constraints, f)

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


def _run_contract_py(
    data: dict,
    schema: dict,
    constraints: dict | None = None,
) -> tuple[bool, str]:
    """Run contract.py's validate_artifact_contract and return (passed, error_msg)."""
    try:
        validate_artifact_contract(json.dumps(data).encode(), schema, constraints)
        return True, ""
    except ContractViolation as e:
        return False, str(e)


# --- Test cases: each is (description, data, schema, constraints, should_pass) ---

_CASES = [
    (
        "basic_schema_pass",
        {"name": "Alice", "age": 30},
        {"name": "string", "age": "number"},
        None,
        True,
    ),
    (
        "missing_field_fails",
        {"name": "Alice"},
        {"name": "string", "age": "number"},
        None,
        False,
    ),
    (
        "wrong_type_fails",
        {"name": "Alice", "age": "thirty"},
        {"name": "string", "age": "number"},
        None,
        False,
    ),
    (
        "nested_object_pass",
        {"user": {"name": "Bob", "active": True}},
        {"user": {"name": "string", "active": "boolean"}},
        None,
        True,
    ),
    (
        "nested_missing_field_fails",
        {"user": {"name": "Bob"}},
        {"user": {"name": "string", "active": "boolean"}},
        None,
        False,
    ),
    (
        "array_of_objects_pass",
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": "number"}]},
        None,
        True,
    ),
    (
        "empty_array_fails",
        {"items": []},
        {"items": [{"id": "number"}]},
        None,
        False,
    ),
    (
        "enum_constraint_pass",
        {"status": "active"},
        {"status": "string"},
        {"enums": [{"path": "status", "values": ["active", "inactive"]}]},
        True,
    ),
    (
        "enum_constraint_fails",
        {"status": "deleted"},
        {"status": "string"},
        {"enums": [{"path": "status", "values": ["active", "inactive"]}]},
        False,
    ),
    (
        "range_constraint_pass",
        {"score": 85},
        {"score": "number"},
        {"ranges": [{"path": "score", "min": 0, "max": 100}]},
        True,
    ),
    (
        "range_constraint_fails",
        {"score": 150},
        {"score": "number"},
        {"ranges": [{"path": "score", "min": 0, "max": 100}]},
        False,
    ),
    (
        "array_length_bracket_path_pass",
        {
            "issues": [
                {"sources": [{"id": 1}, {"id": 2}]},
                {"sources": [{"id": 3}, {"id": 4}]},
            ]
        },
        {"issues": [{"sources": [{"id": "number"}]}]},
        {"array_length": [{"path": "issues[].sources", "min": 2}]},
        True,
    ),
    (
        "array_length_bracket_path_fails_second_item",
        {
            "issues": [
                {"sources": [{"id": 1}, {"id": 2}]},
                {"sources": [{"id": 3}]},
            ]
        },
        {"issues": [{"sources": [{"id": "number"}]}]},
        {"array_length": [{"path": "issues[].sources", "min": 2}]},
        False,
    ),
    (
        "equals_count_pass",
        {"total": 3, "items": [{"id": 1}, {"id": 2}, {"id": 3}]},
        {"total": "number", "items": [{"id": "number"}]},
        {"equals": [{"left": "total", "right": {"count": "items"}}]},
        True,
    ),
    (
        "equals_count_fails",
        {"total": 5, "items": [{"id": 1}, {"id": 2}]},
        {"total": "number", "items": [{"id": "number"}]},
        {"equals": [{"left": "total", "right": {"count": "items"}}]},
        False,
    ),
    (
        "equals_sum_pass",
        {"total": 6, "vals": [{"n": 1}, {"n": 2}, {"n": 3}]},
        {"total": "number", "vals": [{"n": "number"}]},
        {"equals": [{"left": "total", "right": {"sum": "vals[].n"}}]},
        True,
    ),
    (
        "exact_ids_pass",
        {"items": [{"id": "a"}, {"id": "b"}]},
        {"items": [{"id": "string"}]},
        {"exact_ids": [{"path": "items[].id", "values": ["a", "b"]}]},
        True,
    ),
    (
        "exact_ids_fails",
        {"items": [{"id": "a"}, {"id": "c"}]},
        {"items": [{"id": "string"}]},
        {"exact_ids": [{"path": "items[].id", "values": ["a", "b"]}]},
        False,
    ),
]


class TestValidatorParity:
    @pytest.mark.parametrize(
        "name,data,schema,constraints,should_pass",
        _CASES,
        ids=[c[0] for c in _CASES],
    )
    def test_heredoc_matches_contract_py(self, name, data, schema, constraints, should_pass):
        """Both validators must agree on pass/fail for every test case."""
        contract_passed, contract_err = _run_contract_py(data, schema, constraints)
        heredoc_passed, heredoc_output = _run_heredoc_validator(data, schema, constraints)

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
