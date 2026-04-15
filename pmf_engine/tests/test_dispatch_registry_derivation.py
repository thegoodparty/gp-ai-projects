from __future__ import annotations

import importlib.util


def test_derive_projects_required_fields_only():
    from pmf_engine.control_plane.dispatch_registry import _derive
    mock_full = {
        "test_exp": {
            "harness": "claude_sdk",
            "model": "sonnet",
            "timeout_seconds": 1200,
            "contract": {
                "type": "json",
                "s3_key_template": "test_exp/{run_id}/test_exp.json",
                "schema": {"a": "string"},
            },
            "instruction": "a very long markdown blob",
            "mode": "win",
            "max_turns": 50,
        }
    }
    derived = _derive(mock_full)
    assert derived == {
        "test_exp": {
            "harness": "claude_sdk",
            "model": "sonnet",
            "timeout_seconds": 1200,
            "contract": {"s3_key_template": "test_exp/{run_id}/test_exp.json"},
        },
    }


def test_derive_applies_default_timeout_when_missing():
    from pmf_engine.control_plane.dispatch_registry import _derive
    mock_full = {
        "test_exp": {
            "harness": "claude_sdk",
            "model": "sonnet",
            "contract": {"s3_key_template": "x"},
        },
    }
    derived = _derive(mock_full)
    assert derived["test_exp"]["timeout_seconds"] == 600


def test_dispatch_registry_derives_every_experiment_from_full_registry():
    from pmf_engine.control_plane.dispatch_registry import DISPATCH_REGISTRY
    from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY

    assert set(DISPATCH_REGISTRY.keys()) == set(EXPERIMENT_REGISTRY.keys())
    for name, full in EXPERIMENT_REGISTRY.items():
        d = DISPATCH_REGISTRY[name]
        assert d["harness"] == full["harness"]
        assert d["model"] == full["model"]
        assert d["timeout_seconds"] == full.get("timeout_seconds", 600)
        assert d["contract"]["s3_key_template"] == full["contract"]["s3_key_template"]


def test_generated_flat_registry_is_self_contained(tmp_path):
    from pmf_engine.scripts.generate_flat_dispatch_registry import render_flat_registry
    from pmf_engine.control_plane.dispatch_registry import DISPATCH_REGISTRY

    flat_code = render_flat_registry(DISPATCH_REGISTRY)
    flat_file = tmp_path / "dispatch_registry.py"
    flat_file.write_text(flat_code)

    spec = importlib.util.spec_from_file_location("_isolated_flat_dispatch_registry", flat_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.DISPATCH_REGISTRY == DISPATCH_REGISTRY


def test_generated_flat_registry_has_no_runtime_imports():
    from pmf_engine.scripts.generate_flat_dispatch_registry import render_flat_registry
    from pmf_engine.control_plane.dispatch_registry import DISPATCH_REGISTRY

    flat_code = render_flat_registry(DISPATCH_REGISTRY)
    code_lines = [
        ln for ln in flat_code.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    for line in code_lines:
        if line.strip().startswith(("import ", "from ")):
            assert "pmf_engine" not in line, f"flat registry must not import pmf_engine: {line}"
            assert "registry" not in line or "__future__" in line, (
                f"flat registry must not import from registry: {line}"
            )
