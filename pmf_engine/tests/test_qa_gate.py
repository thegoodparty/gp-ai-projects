"""Tests for the PMF QA gate engine (LANE A, v1 observe-only).

The engine grades the exact artifact bytes a run produced against an
experiment's qa/ folder (a deterministic `main.py` subprocess and/or an
`eval.md` evaluator agent), and emits a Verdict that ALWAYS rides the
success/publish path. v1 is OBSERVE-ONLY: there is no blocking-fail branch,
no quarantine, no fail-closed. A gate error becomes a Verdict with
status 'error' and the run still publishes (fail-open).

Tests inject a fake `evaluator_runner` and materialize tiny `main.py`
fixtures through the qa_envelope, so the engine is exercised end-to-end with
no real agent and no real subprocess crash beyond what a fixture script does.

The materialization base is injected (`gate_base_dir`) so the private qa dir
lands outside both `workspace_dir` and `/tmp` (the runner's log sweep collects
/tmp .md/.json — see runner/main.py `_collect_log_files` + `_SAFE_TMP_EXTENSIONS`).
"""

from __future__ import annotations

import json
import logging
import os

import pytest

import pmf_engine.runner.qa_gate as qa_gate_mod
from pmf_engine.runner.harness.base import EvaluatorHarnessParams, EvaluatorResult
from pmf_engine.runner.qa_gate import Verdict, run_qa_gate


@pytest.fixture
def gate_logs():
    """Capture WARNING+ records emitted by the qa_gate module logger.

    ``shared.logger.get_logger`` sets ``propagate = False`` on its loggers, so
    pytest's ``caplog`` (which attaches to the root logger) never sees them.
    Attaching a list-capturing handler directly to the module logger asserts on
    the actual emitted records."""

    class _ListHandler(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    handler = _ListHandler()
    qa_gate_mod.logger.addHandler(handler)
    prev_level = qa_gate_mod.logger.level
    qa_gate_mod.logger.setLevel(logging.WARNING)
    try:
        yield handler
    finally:
        qa_gate_mod.logger.removeHandler(handler)
        qa_gate_mod.logger.setLevel(prev_level)


def _messages(handler) -> list[str]:
    return [r.getMessage() for r in handler.records]


# --------------------------------------------------------------------------
# Fakes / helpers
# --------------------------------------------------------------------------

ARTIFACT = json.dumps({"summary": {"total": 3}}).encode("utf-8")


class FakeEvaluator:
    """Records the params it was called with and writes a configurable
    fragment array to the injected result_file_path, returning a configurable
    EvaluatorResult. Mirrors the real run_evaluator contract: the engine reads
    the canonical fragments from the file, not from the returned object."""

    def __init__(
        self,
        *,
        fragments: list[dict] | None = None,
        write_file: bool = True,
        file_contents: str | None = None,
        result: EvaluatorResult | None = None,
        raise_exc: BaseException | None = None,
    ):
        self.fragments = fragments if fragments is not None else [{"name": "faithfulness", "passed": True}]
        self.write_file = write_file
        self.file_contents = file_contents
        self.result = result
        self.raise_exc = raise_exc
        self.calls: list[EvaluatorHarnessParams] = []

    def __call__(self, params: EvaluatorHarnessParams) -> EvaluatorResult:
        self.calls.append(params)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.write_file:
            contents = self.file_contents if self.file_contents is not None else json.dumps(self.fragments)
            with open(params.result_file_path, "w", encoding="utf-8") as fh:
                fh.write(contents)
        if self.result is not None:
            return self.result
        return EvaluatorResult(
            fragments=self.fragments,
            cost_usd=0.04,
            duration_ms=1200,
            num_turns=3,
            session_id="eval-sess",
            status="ok",
        )


def _never_called_evaluator(_params: EvaluatorHarnessParams) -> EvaluatorResult:
    raise AssertionError("evaluator_runner should not have been invoked")


def _envelope(
    *,
    files: dict[str, str] | None = None,
    manifest: dict | None = None,
    resolved_qa_version_ids: dict[str, str] | None = None,
) -> dict:
    return {
        "manifest": manifest if manifest is not None else {"blocking": False},
        "files": files if files is not None else {},
        "resolved_qa_version_ids": (
            resolved_qa_version_ids if resolved_qa_version_ids is not None else {"manifest.json": "v-man"}
        ),
    }


# main.py fixtures (UTF-8 source written into the qa envelope's files dict).

_MAIN_PASS = """\
import json, sys
print(json.dumps([{"name": "grounding", "passed": True, "score": 0.91}]))
"""

_MAIN_FAIL = """\
import json, sys
print(json.dumps([{"name": "grounding", "passed": False, "score": 0.4, "detail": "below threshold"}]))
"""

_MAIN_NONZERO_EXIT = """\
import sys
sys.stderr.write("boom: deterministic check crashed\\n")
sys.exit(3)
"""

_MAIN_UNPARSEABLE = """\
print("this is not json at all")
"""

_MAIN_INVALID_FRAGMENT = """\
import json
print(json.dumps([{"score": 0.5}]))
"""

_MAIN_ECHO_ARGS = """\
import json, sys, os
print(json.dumps([{
    "name": "echo",
    "passed": True,
    "detail": " ".join(sys.argv[1:]),
    "cwd": os.getcwd(),
}]))
"""


@pytest.fixture
def gate_base(tmp_path):
    """A materialization base that is NOT under /tmp and NOT under the
    workspace dir, so the engine's private qa dir can never leak into the
    runner's /tmp or workspace log sweep."""
    base = tmp_path / "qa-gate-root"
    base.mkdir()
    return str(base)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    return str(ws)


def _broker_env() -> dict:
    return {"BROKER_URL": "https://broker.test", "BROKER_TOKEN": "tok-123"}


# Generous budget so the pre-flight check never trips unless a test sets it low.
BIG_BUDGET = 10_000.0


# --------------------------------------------------------------------------
# No qa folder -> None (byte-identical no-gate path)
# --------------------------------------------------------------------------


def test_no_qa_envelope_returns_none(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=None,
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert verdict is None


# --------------------------------------------------------------------------
# Empty qa folder (neither entrypoint) -> skipped
# --------------------------------------------------------------------------


def test_empty_qa_folder_is_skipped(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "skipped"
    assert verdict.checks == []
    assert verdict.verdict_version == 1


# --------------------------------------------------------------------------
# Insufficient budget -> error, no stage invoked
# --------------------------------------------------------------------------


def test_insufficient_budget_returns_error_without_invoking_stages(workspace, gate_base):
    manifest = {
        "blocking": False,
        "deterministic": {"timeout_seconds": 120},
        "agent": {"model": "sonnet", "max_turns": 20, "timeout_seconds": 300},
    }
    ran_main = {"flag": False}

    def evaluator_must_not_run(_params):
        ran_main["flag"] = True
        raise AssertionError("evaluator invoked despite insufficient budget")

    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(
            files={"main.py": _MAIN_PASS, "eval.md": "judge this"},
            manifest=manifest,
        ),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        # 120 + 300 = 420 required; give less.
        remaining_budget_seconds=100.0,
        evaluator_runner=evaluator_must_not_run,
        gate_base_dir=gate_base,
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    assert ran_main["flag"] is False
    # Surfaces the reason in a discoverable way.
    assert any("insufficient_budget" in v for v in verdict.violations)


# --------------------------------------------------------------------------
# main.py: passing fragments
# --------------------------------------------------------------------------


def test_main_py_passing_fragment_yields_evaluated_pass(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    names = [c["name"] for c in verdict.checks]
    assert names == ["grounding"]
    assert verdict.checks[0]["passed"] is True
    assert verdict.checks[0]["type"] == "deterministic"


# --------------------------------------------------------------------------
# main.py: a failing fragment -> pass False
# --------------------------------------------------------------------------


def test_main_py_failing_fragment_yields_pass_false(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_FAIL}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


# --------------------------------------------------------------------------
# main.py: nonzero exit -> synthetic failing fragment 'main_py_exit'
# --------------------------------------------------------------------------


def test_main_py_nonzero_exit_injects_synthetic_failing_fragment(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_NONZERO_EXIT}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    # A crash is NOT a stage error in v1 — it's a synthetic failing fragment,
    # so pass is False (not None).
    assert verdict.pass_ is False
    synthetic = [c for c in verdict.checks if c["name"] == "main_py_exit"]
    assert len(synthetic) == 1
    assert synthetic[0]["passed"] is False
    # Last 4KB of stderr folded into detail.
    assert "boom: deterministic check crashed" in synthetic[0]["detail"]


# --------------------------------------------------------------------------
# main.py: unparseable stdout -> stage error
# --------------------------------------------------------------------------


def test_main_py_unparseable_stdout_is_stage_error(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_UNPARSEABLE}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# main.py: invalid fragment (missing name/passed) -> synthetic failing replacement
# --------------------------------------------------------------------------


def test_main_py_invalid_fragment_replaced_by_synthetic_failing(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_INVALID_FRAGMENT}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    # The fragment had no name/passed -> replaced by a synthetic failing one.
    assert verdict.pass_ is False
    assert all(c["passed"] is False for c in verdict.checks)
    assert len(verdict.checks) == 1
    assert verdict.checks[0]["passed"] is False


# --------------------------------------------------------------------------
# main.py invocation contract: argv + cwd
# --------------------------------------------------------------------------


def test_main_py_invoked_with_artifact_and_workspace_args_in_gate_cwd(workspace, gate_base):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_ECHO_ARGS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    echo = verdict.checks[0]
    # `--artifact <path> --workspace <workspace_dir>`
    assert "--artifact" in echo["detail"]
    assert "--workspace" in echo["detail"]
    assert workspace in echo["detail"]
    # cwd is the materialized gate dir, NOT the workspace.
    assert echo["cwd"] != workspace
    assert os.path.realpath(echo["cwd"]).startswith(os.path.realpath(gate_base))


# --------------------------------------------------------------------------
# eval.md: evaluator reads fragments from the injected result_file_path
# --------------------------------------------------------------------------


def test_evaluator_fragments_read_from_injected_result_file(workspace, gate_base):
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True, "score": 4.5}])
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "Judge the artifact for faithfulness."}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    assert len(fake.calls) == 1
    params = fake.calls[0]
    assert isinstance(params, EvaluatorHarnessParams)
    # The evaluator's instruction is the eval.md body.
    assert params.instruction == "Judge the artifact for faithfulness."
    # result_file_path is inside the gate dir, not workspace, not /tmp.
    rfp = os.path.realpath(params.result_file_path)
    assert rfp.startswith(os.path.realpath(gate_base))
    assert not rfp.startswith(os.path.realpath(workspace) + os.sep)
    assert not rfp.startswith("/tmp/")
    # gate_cwd is the materialized gate dir, workspace passed through read-only.
    assert os.path.realpath(params.gate_cwd).startswith(os.path.realpath(gate_base))
    assert params.workspace_dir == workspace
    # The faithfulness check is tagged type 'agent'.
    agent_checks = [c for c in verdict.checks if c["name"] == "faithfulness"]
    assert len(agent_checks) == 1
    assert agent_checks[0]["type"] == "agent"


# --------------------------------------------------------------------------
# eval.md: missing result file -> stage error
# --------------------------------------------------------------------------


def test_evaluator_missing_result_file_is_stage_error(workspace, gate_base):
    fake = FakeEvaluator(write_file=False)
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_evaluator_unparseable_result_file_is_stage_error(workspace, gate_base):
    fake = FakeEvaluator(write_file=True, file_contents="{not json")
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# Both stages run; pass True iff ALL fragments passed
# --------------------------------------------------------------------------


def test_both_stages_pass_true_only_when_all_fragments_pass(workspace, gate_base):
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    # Both stages ran (deterministic-first), fragments from both present.
    names = sorted(c["name"] for c in verdict.checks)
    assert names == ["faithfulness", "grounding"]
    assert len(fake.calls) == 1


def test_both_stages_one_failing_fragment_yields_pass_false(workspace, gate_base):
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": False}])
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


def test_evaluator_runner_status_error_makes_verdict_error(workspace, gate_base):
    fake = FakeEvaluator(
        write_file=True,
        result=EvaluatorResult(fragments=[], status="error"),
    )
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# qa_version_ids carried through
# --------------------------------------------------------------------------


def test_verdict_carries_resolved_qa_version_ids(workspace, gate_base):
    ids = {"manifest.json": "v-man", "main.py": "v-main"}
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}, resolved_qa_version_ids=ids),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert verdict.qa_version_ids == ids


# --------------------------------------------------------------------------
# Materialization OUTSIDE workspace AND /tmp; dir deleted afterward
# --------------------------------------------------------------------------


def test_qa_dir_materialized_outside_workspace_and_tmp_and_deleted(workspace, gate_base):
    captured = {}

    _MAIN_REPORT_CWD = """\
import json, os
print(json.dumps([{"name": "loc", "passed": True, "cwd": os.getcwd()}]))
"""
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_REPORT_CWD, "eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=FakeEvaluator(),
        gate_base_dir=gate_base,
    )
    gate_cwd = next(c["cwd"] for c in verdict.checks if c.get("name") == "loc")
    real_gate = os.path.realpath(gate_cwd)
    real_ws = os.path.realpath(workspace)
    # Outside workspace.
    assert not real_gate.startswith(real_ws + os.sep)
    assert real_gate != real_ws
    # Outside the literal /tmp the runner sweeps.
    assert not real_gate.startswith("/tmp/")
    assert real_gate != "/tmp"
    # Under the injected base.
    assert real_gate.startswith(os.path.realpath(gate_base))
    # Deleted after the gate finished.
    assert not os.path.exists(gate_cwd)
    captured["gate_cwd"] = gate_cwd


# --------------------------------------------------------------------------
# 8KB serialized cap: truncation order (violations, then per-check detail)
# --------------------------------------------------------------------------


def test_verdict_capped_at_8kb_truncates_violations_then_detail(workspace, gate_base):
    # Produce many failing fragments with big detail strings so the serialized
    # verdict blows past 8KB and forces truncation.
    # main.py BUILDS the fragments itself (don't embed Python-invalid JSON
    # literals like `false`/`true` into the source — that would NameError).
    main_src = (
        "import json\n"
        "big = 'X' * 2000\n"
        "frags = [{'name': f'check_{i}', 'passed': False, 'detail': big} for i in range(12)]\n"
        "print(json.dumps(frags))\n"
    )

    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    # Assert on the SERIALIZED verdict — that is the only thing the cap protects.
    # The untruncated `verdict.checks`/`verdict.violations` attributes are NOT
    # the contract; `to_dict()` output is.
    d = verdict.to_dict()
    serialized = json.dumps(d)
    assert len(serialized) <= 8 * 1024
    # 1) violations dropped first.
    assert d["violations"] == []
    # 2) per-check detail stripped (this fixture is big enough to force step 3,
    #    so no capped check carries 'detail') — and every check still carries
    #    name + passed.
    for c in d["checks"]:
        assert "detail" not in c
        assert "name" in c
        assert "passed" in c
    # The overall verdict still reports failure.
    assert d["pass"] is False
    assert verdict.pass_ is False


# --------------------------------------------------------------------------
# Fail-OPEN: an internal exception yields status 'error', never raises
# --------------------------------------------------------------------------


def test_internal_exception_yields_error_verdict_never_raises(workspace, gate_base):
    # Force an internal error by passing a gate_base_dir that cannot be created
    # (a path whose parent is a file, so makedirs raises NotADirectoryError).
    bad_parent = os.path.join(workspace, "output", "afile")
    with open(bad_parent, "w") as fh:
        fh.write("x")
    bad_base = os.path.join(bad_parent, "nope")

    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=bad_base,
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# Blocking is recorded but NOT enforced in v1 (observe-only)
# --------------------------------------------------------------------------


def test_blocking_true_still_runs_both_stages_observe_only(workspace, gate_base):
    # blocking: true must NOT short-circuit; both stages still run and the
    # verdict still rides the success path. (Observe-only.)
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(
            files={"main.py": _MAIN_FAIL, "eval.md": "judge"},
            manifest={"blocking": True},
        ),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    # Deterministic stage failed, but the evaluator STILL ran (no short-circuit).
    assert len(fake.calls) == 1
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


# --------------------------------------------------------------------------
# A1: an entrypoint ran but produced ZERO fragments is NOT a clean pass.
# --------------------------------------------------------------------------


def test_evaluator_empty_fragments_is_evaluated_but_pass_none(workspace, gate_base):
    # The evaluator ran to completion (status 'ok') but emitted an empty
    # fragment array. status is 'evaluated' (no stage error), but `all([])` is
    # vacuously True today — pass MUST be None, not True. An entrypoint that
    # produced zero fragments verified nothing.
    fake = FakeEvaluator(fragments=[])
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "evaluated"
    assert verdict.checks == []
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# A6: a leaked BROKER_TOKEN printed to stderr is REDACTED from the verdict.
# --------------------------------------------------------------------------


def test_main_py_stderr_broker_token_redacted_from_verdict_detail(workspace, gate_base):
    secret = "tok-super-secret-9f8a7b6c5d"
    main_src = f"import sys\nsys.stderr.write('crashed; leaked BROKER_TOKEN={secret} oops\\n')\nsys.exit(2)\n"
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    synthetic = [c for c in verdict.checks if c["name"] == "main_py_exit"]
    assert len(synthetic) == 1
    detail = synthetic[0]["detail"]
    # The raw token must NOT appear anywhere in the verdict (it travels to
    # gp-api + Braintrust). Some redacted marker should remain so the crash is
    # still discoverable.
    assert secret not in detail
    assert secret not in json.dumps(verdict.to_dict())
    assert "crashed" in detail


# --------------------------------------------------------------------------
# A9: subprocess spawn raising (python3 missing) -> error verdict, no raise.
# --------------------------------------------------------------------------


def test_main_py_subprocess_filenotfound_is_error_fail_open(workspace, gate_base, monkeypatch):
    import pmf_engine.runner.qa_gate as qa_gate_mod

    def boom(*_a, **_k):
        raise FileNotFoundError("python3 not on PATH")

    monkeypatch.setattr(qa_gate_mod.subprocess, "Popen", boom)

    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# A4: a PRESENT but invalid timeout/turns value logs a warning when coerced.
# --------------------------------------------------------------------------


def test_invalid_present_timeout_logs_coercion_warning(workspace, gate_base, gate_logs):
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(
            files={"main.py": _MAIN_PASS},
            manifest={"blocking": False, "deterministic": {"timeout_seconds": -5}},
        ),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    # Misconfiguration is discoverable; the run still proceeds on the default.
    assert verdict.status == "evaluated"
    coerced = [m for m in _messages(gate_logs) if "invalid_budget_coerced" in m]
    assert len(coerced) == 1
    assert "deterministic.timeout_seconds" in coerced[0]
    assert "-5" in coerced[0]


def test_absent_timeout_does_not_log_coercion_warning(workspace, gate_base, gate_logs):
    run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}, manifest={"blocking": False}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    # An ABSENT value taking the default is normal, not a misconfiguration.
    assert not any("invalid_budget_coerced" in m for m in _messages(gate_logs))


# --------------------------------------------------------------------------
# A5: run_id flows into the stage-error log lines for correlation.
# --------------------------------------------------------------------------


def test_run_id_in_stage_error_log_line(workspace, gate_base, gate_logs):
    run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_UNPARSEABLE}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
        run_id="run-abc-123",
        experiment_id="exp-xyz",
    )
    unparseable = [m for m in _messages(gate_logs) if "unparseable_stdout" in m]
    assert len(unparseable) == 1
    assert "run-abc-123" in unparseable[0]


# --------------------------------------------------------------------------
# A7: evaluator status-error log carries session_id/num_turns/cost + run_id.
# --------------------------------------------------------------------------


def test_evaluator_status_error_log_carries_context(workspace, gate_base, gate_logs):
    fake = FakeEvaluator(
        write_file=True,
        result=EvaluatorResult(
            fragments=[],
            status="error",
            session_id="sess-99",
            num_turns=7,
            cost_usd=0.12,
        ),
    )
    run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
        run_id="run-eval-1",
    )
    rec = [m for m in _messages(gate_logs) if "evaluator_status_error" in m]
    assert len(rec) == 1
    msg = rec[0]
    assert "sess-99" in msg
    assert "num_turns=7" in msg
    assert "0.12" in msg
    assert "run-eval-1" in msg


# --------------------------------------------------------------------------
# A2: a runaway main.py whose stdout exceeds the 1MB cap -> stage error,
# and the runner must not buffer unbounded bytes (bounded capture).
# --------------------------------------------------------------------------


def test_main_py_stdout_over_cap_is_stage_error(workspace, gate_base):
    # Stream well past the 1MB cap on stdout.
    main_src = "import sys\nchunk = 'A' * 65536\nfor _ in range(40):\n    sys.stdout.write(chunk)\nsys.stdout.flush()\n"
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# A2: main.py exceeding the deterministic timeout -> stage error (killed).
# --------------------------------------------------------------------------


def test_main_py_timeout_is_stage_error(workspace, gate_base):
    main_src = "import time\ntime.sleep(30)\n"
    verdict = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(
            files={"main.py": main_src},
            manifest={"blocking": False, "deterministic": {"timeout_seconds": 1}},
        ),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None
