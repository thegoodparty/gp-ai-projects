"""Parity tests: ensure runbook validate_contract.py matches contract.py.

The runbook script at ~/work/runbooks/scripts/python/validate_contract.py is
the third copy of the validator, used for local CLI runs. This test ensures it
stays in sync with the canonical contract.py.

Skipped if the runbook file doesn't exist (CI environments).
"""
import importlib.util
import json
import os
import sys

import pytest

from pmf_engine.runner.contract import validate_artifact_contract, ContractViolation

_RUNBOOK_PATH = os.path.expanduser("~/work/runbooks/scripts/python/validate_contract.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_RUNBOOK_PATH),
    reason="Runbook validator not found (CI or different machine)",
)


def _load_runbook_module():
    spec = importlib.util.spec_from_file_location("validate_contract_runbook", _RUNBOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Prevent the module's __main__ block from executing
    saved_argv = sys.argv
    sys.argv = [_RUNBOOK_PATH, "--help"]
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    finally:
        sys.argv = saved_argv
    return mod


@pytest.fixture(scope="module")
def runbook():
    return _load_runbook_module()


def _run_contract_py(data, schema, constraints=None):
    try:
        validate_artifact_contract(json.dumps(data).encode(), schema, constraints)
        return True, []
    except ContractViolation as e:
        return False, [str(e)]


def _run_runbook(mod, data, schema, constraints=None):
    errors = mod.validate(data, schema)
    if constraints:
        errors.extend(mod.validate_constraints(data, constraints))
    return len(errors) == 0, errors


_CASES = [
    (
        "basic_pass",
        {"name": "Alice", "age": 30},
        {"name": "string", "age": "number"},
        None,
        True,
    ),
    (
        "missing_field",
        {"name": "Alice"},
        {"name": "string", "age": "number"},
        None,
        False,
    ),
    (
        "wrong_type",
        {"name": 42, "age": 30},
        {"name": "string", "age": "number"},
        None,
        False,
    ),
    (
        "nested_pass",
        {"user": {"name": "Bob", "active": True}},
        {"user": {"name": "string", "active": "boolean"}},
        None,
        True,
    ),
    (
        "array_pass",
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": "number"}]},
        None,
        True,
    ),
    (
        "empty_array",
        {"items": []},
        {"items": [{"id": "number"}]},
        None,
        False,
    ),
    (
        "enum_pass",
        {"status": "active"},
        {"status": "string"},
        {"enums": [{"path": "status", "values": ["active", "inactive"]}]},
        True,
    ),
    (
        "enum_fail",
        {"status": "deleted"},
        {"status": "string"},
        {"enums": [{"path": "status", "values": ["active", "inactive"]}]},
        False,
    ),
    (
        "range_pass",
        {"score": 50},
        {"score": "number"},
        {"ranges": [{"path": "score", "min": 0, "max": 100}]},
        True,
    ),
    (
        "range_fail",
        {"score": 200},
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
        "array_length_bracket_path_fail",
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
        {"total": 2, "items": [{"id": 1}, {"id": 2}]},
        {"total": "number", "items": [{"id": "number"}]},
        {"equals": [{"left": "total", "right": {"count": "items"}}]},
        True,
    ),
    (
        "exact_ids_pass",
        {"items": [{"id": "x"}, {"id": "y"}]},
        {"items": [{"id": "string"}]},
        {"exact_ids": [{"path": "items[].id", "values": ["x", "y"]}]},
        True,
    ),
    (
        "exact_ids_fail",
        {"items": [{"id": "x"}, {"id": "z"}]},
        {"items": [{"id": "string"}]},
        {"exact_ids": [{"path": "items[].id", "values": ["x", "y"]}]},
        False,
    ),
]


class TestRunbookParity:
    @pytest.mark.parametrize(
        "name,data,schema,constraints,should_pass",
        _CASES,
        ids=[c[0] for c in _CASES],
    )
    def test_runbook_matches_contract_py(self, runbook, name, data, schema, constraints, should_pass):
        contract_passed, _ = _run_contract_py(data, schema, constraints)
        runbook_passed, runbook_errors = _run_runbook(runbook, data, schema, constraints)

        assert contract_passed == should_pass, f"contract.py disagreed on '{name}'"
        assert runbook_passed == should_pass, (
            f"runbook disagreed on '{name}': expected {'pass' if should_pass else 'fail'}, "
            f"errors={runbook_errors}"
        )
        assert contract_passed == runbook_passed, (
            f"PARITY VIOLATION on '{name}': contract.py={'pass' if contract_passed else 'fail'}, "
            f"runbook={'pass' if runbook_passed else 'fail'}"
        )
