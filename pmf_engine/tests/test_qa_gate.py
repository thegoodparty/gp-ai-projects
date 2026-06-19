"""Tests for the PMF QA gate engine (LANE A, v1 DETERMINISTIC-ONLY, observe-only).

The engine grades the exact artifact bytes a run produced against an
experiment's qa/ folder by running ONE deterministic `main.py` subprocess, and
emits a Verdict that ALWAYS rides the success/publish path. v1 is
DETERMINISTIC-ONLY and OBSERVE-ONLY: there is no AI evaluator, no eval.md, no
blocking-fail branch, no quarantine, no fail-closed. A gate error becomes a
Verdict with status 'error' and the run still publishes (fail-open).

`run_qa_gate` returns a ``(verdict, raw_output, eval_transcript)`` tuple (or
``None`` when no qa folder): ``raw_output`` is the raw ``main.py`` stdout and
``eval_transcript`` is the evaluator's redacted per-turn JSONL transcript — the
runner forwards both to the broker for the durable S3 verdict.json write.

Tests materialize tiny `main.py` fixtures through the qa_envelope, so the engine
is exercised end-to-end with a real subprocess.

The materialization base is injected (`gate_base_dir`) so the private qa dir
lands outside both `workspace_dir` and `/tmp` (the runner's log sweep collects
/tmp .md/.json — see runner/main.py `_collect_log_files` + `_SAFE_TMP_EXTENSIONS`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from unittest import mock

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


@pytest.fixture
def gate_info_logs():
    """Capture INFO+ records emitted by the qa_gate module logger.

    Same propagate=False workaround as ``gate_logs`` but at INFO level so the
    verdict summary log line (emitted at INFO) is observable."""

    class _ListHandler(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.INFO)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    handler = _ListHandler()
    qa_gate_mod.logger.addHandler(handler)
    prev_level = qa_gate_mod.logger.level
    qa_gate_mod.logger.setLevel(logging.INFO)
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


def _verdict(result):
    """Unwrap the (verdict, raw_output, eval_transcript) tuple run_qa_gate returns.

    run_qa_gate returns None for no-qa, else a (Verdict, raw_output,
    eval_transcript) tuple. These helpers keep the per-test assertions focused
    on one piece at a time."""
    assert result is not None
    verdict, _raw, _transcript = result
    return verdict


def _raw_output(result):
    assert result is not None
    _verdict, raw, _transcript = result
    return raw


def _transcript(result):
    assert result is not None
    _verdict, _raw, transcript = result
    return transcript


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
        eval_transcript: str = "",
    ):
        self.fragments = fragments if fragments is not None else [{"name": "faithfulness", "passed": True}]
        self.write_file = write_file
        self.file_contents = file_contents
        self.result = result
        self.raise_exc = raise_exc
        self.eval_transcript = eval_transcript
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
            eval_transcript=self.eval_transcript,
        )


def _never_called_evaluator(_params: EvaluatorHarnessParams) -> EvaluatorResult:
    raise AssertionError("evaluator_runner should not have been invoked")


# --------------------------------------------------------------------------
# No qa folder -> None (byte-identical no-gate path)
# --------------------------------------------------------------------------


def test_no_qa_envelope_returns_none(workspace, gate_base):
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=None,
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    assert result is None


# --------------------------------------------------------------------------
# qa folder with no main.py -> skipped
# --------------------------------------------------------------------------


def test_qa_folder_without_main_py_is_skipped(workspace, gate_base):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "skipped"
    assert verdict.checks == []
    assert verdict.verdict_version == 1


def test_skipped_verdict_has_no_raw_output(workspace, gate_base):
    """A skipped run never spawned main.py, so there is no raw stdout to write
    to S3 — raw_output is None."""
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    assert _raw_output(result) is None


# --------------------------------------------------------------------------
# Insufficient budget -> error, main.py never invoked
# --------------------------------------------------------------------------


def test_insufficient_budget_returns_error_without_invoking_main(workspace, gate_base):
    # Only deterministic.timeout_seconds is accounted in the pre-flight budget
    # (the agent timeout term is gone with the evaluator).
    manifest = {
        "blocking": False,
        "deterministic": {"timeout_seconds": 120},
    }
    main_marker = workspace + "/MAIN_RAN"
    main_src = f"open({main_marker!r}, 'w').close()\nimport json\nprint(json.dumps([]))\n"

    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(
                files={"main.py": main_src},
                manifest=manifest,
            ),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            # 120 required; give less.
            remaining_budget_seconds=100.0,
            gate_base_dir=gate_base,
        )
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    assert not os.path.exists(main_marker), "main.py must not run when budget is insufficient"
    # Surfaces the reason in a discoverable way.
    assert any("insufficient_budget" in v for v in verdict.violations)
    # Required budget reflects ONLY the deterministic timeout (no agent term).
    assert any("120" in v for v in verdict.violations)


# --------------------------------------------------------------------------
# main.py: passing fragments
# --------------------------------------------------------------------------


def test_main_py_passing_fragment_yields_evaluated_pass(workspace, gate_base):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    names = [c["name"] for c in verdict.checks]
    assert names == ["grounding"]
    assert verdict.checks[0]["passed"] is True
    assert verdict.checks[0]["type"] == "deterministic"


def test_run_qa_gate_returns_raw_main_py_stdout(workspace, gate_base):
    """run_qa_gate returns the raw main.py stdout alongside the verdict so
    main.py can forward it to the broker for the durable S3 verdict.json write.
    The raw output is the EXACT bytes/text the check emitted on stdout."""
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    raw = _raw_output(result)
    assert raw is not None
    parsed = json.loads(raw)
    assert parsed == [{"name": "grounding", "passed": True, "score": 0.91}]


# --------------------------------------------------------------------------
# main.py: a failing fragment -> pass False
# --------------------------------------------------------------------------


def test_main_py_failing_fragment_yields_pass_false(workspace, gate_base):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_FAIL}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


# --------------------------------------------------------------------------
# main.py: nonzero exit -> synthetic failing fragment 'main_py_exit'
# --------------------------------------------------------------------------


def test_main_py_nonzero_exit_injects_synthetic_failing_fragment(workspace, gate_base):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_NONZERO_EXIT}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
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
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_UNPARSEABLE}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    verdict = _verdict(result)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    # Fix 4: even on the unparseable stage-error path, raw_output is the decoded
    # stdout so the broker can write it durably to main_output.json.
    assert _raw_output(result) == "this is not json at all\n"


def test_main_py_stdout_not_array_is_stage_error_with_raw_output(workspace, gate_base):
    # Valid JSON, but an object not the contract-C array -> stage error.
    main_src = "import json\nprint(json.dumps({'not': 'an array'}))\n"
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    verdict = _verdict(result)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    # Fix 4: raw_output is the decoded stdout the check emitted.
    assert json.loads(_raw_output(result)) == {"not": "an array"}


def test_main_py_empty_stdout_exit0_is_stage_error_with_empty_raw_output(workspace, gate_base):
    # Exit 0 with empty stdout: no fragments -> stage error. raw_output is the
    # decoded (empty) stdout, NOT None (the subprocess produced usable stdout).
    main_src = "import sys\nsys.exit(0)\n"
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    verdict = _verdict(result)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    # Fix 4: empty stdout decodes to "" (a usable, in-cap value), not None.
    assert _raw_output(result) == ""


# --------------------------------------------------------------------------
# main.py: invalid fragment (missing name/passed) -> synthetic failing replacement
# --------------------------------------------------------------------------


def test_main_py_invalid_fragment_replaced_by_synthetic_failing(workspace, gate_base):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_INVALID_FRAGMENT}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    # The fragment had no name/passed -> replaced by a synthetic failing one.
    assert verdict.pass_ is False
    assert all(c["passed"] is False for c in verdict.checks)
    assert len(verdict.checks) == 1
    assert verdict.checks[0]["passed"] is False
    # FIX 2 (test tightening, no red phase — current code already satisfies
    # these): the synthetic replacement is a named, typed, deterministic check
    # whose detail explains the substitution, so an author can tell a malformed
    # fragment from a real failing check in the verdict.
    assert verdict.checks[0]["name"] == "invalid_fragment"
    assert verdict.checks[0]["type"] == "deterministic"
    assert "invalid fragment replaced" in verdict.checks[0]["detail"]


# --------------------------------------------------------------------------
# main.py invocation contract: argv + cwd
# --------------------------------------------------------------------------


def test_main_py_invoked_with_artifact_and_workspace_args_in_gate_cwd(workspace, gate_base):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_ECHO_ARGS}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
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
# pass True iff ALL fragments passed
# --------------------------------------------------------------------------


def test_pass_true_only_when_all_fragments_pass(workspace, gate_base):
    main_src = (
        "import json\n"
        "print(json.dumps([{'name': 'a', 'passed': True}, {'name': 'b', 'passed': True}]))\n"
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    names = sorted(c["name"] for c in verdict.checks)
    assert names == ["a", "b"]


def test_pass_false_when_one_fragment_fails(workspace, gate_base):
    main_src = (
        "import json\n"
        "print(json.dumps([{'name': 'a', 'passed': True}, {'name': 'b', 'passed': False}]))\n"
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


# --------------------------------------------------------------------------
# qa_version_ids carried through
# --------------------------------------------------------------------------


def test_verdict_carries_resolved_qa_version_ids(workspace, gate_base):
    ids = {"manifest.json": "v-man", "main.py": "v-main"}
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS}, resolved_qa_version_ids=ids),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.qa_version_ids == ids


# --------------------------------------------------------------------------
# Materialization OUTSIDE workspace AND /tmp; dir deleted afterward
# --------------------------------------------------------------------------


def test_qa_dir_materialized_outside_workspace_and_tmp_and_deleted(workspace, gate_base):
    _MAIN_REPORT_CWD = """\
import json, os
print(json.dumps([{"name": "loc", "passed": True, "cwd": os.getcwd()}]))
"""
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_REPORT_CWD}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
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


# --------------------------------------------------------------------------
# No truncation: to_dict returns the FULL verdict (S3 + Braintrust, no size cap)
# --------------------------------------------------------------------------


def test_verdict_to_dict_returns_large_verdict_fully_intact(workspace, gate_base):
    # gp-api is DROPPED — it no longer consumes the SQS callback's qaVerdict, so
    # the 8KB serialization cap that existed solely to protect the callback budget
    # is gone. The verdict's system of record is now (a) the broker's durable S3
    # verdict.json (no size limit) and (b) the runner's Braintrust span output.
    # to_dict() must therefore return the FULL verdict: every violation, every
    # check, and every per-check field including the big `detail` strings.
    #
    # Produce MANY failing fragments, each with a big detail string AND several
    # non-detail fields — well past the old 8KB cap — and assert NOTHING is
    # dropped. main.py BUILDS the fragments itself (don't embed Python-invalid
    # JSON literals like `false`/`true` into the source — that would NameError).
    main_src = (
        "import json\n"
        "big = 'X' * 200\n"
        "frags = [{'name': f'check_{i}', 'passed': False, 'detail': big,\n"
        "          'score': 0.5, 'min_score': 0.8, 'duration_ms': 1234}\n"
        "         for i in range(120)]\n"
        "print(json.dumps(frags))\n"
    )

    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    d = verdict.to_dict()
    # The serialized verdict is well over the OLD 8KB cap — proving no cap clamps
    # it anymore (120 checks * (200-byte detail + score/min_score/duration_ms)
    # plus 120 violations is ~30KB+).
    assert len(json.dumps(d)) > 8 * 1024
    # All 120 checks survive.
    assert len(d["checks"]) == 120
    # All violations survive (one per failing check).
    assert len(d["violations"]) == 120
    # Every per-check field is intact: name + passed + type AND the big detail
    # plus the non-essential score / min_score / duration_ms passthroughs.
    big = "X" * 200
    for c in d["checks"]:
        assert c["type"] == "deterministic"
        assert c["passed"] is False
        assert c["name"].startswith("check_")
        assert c["detail"] == big
        assert c["score"] == 0.5
        assert c["min_score"] == 0.8
        assert c["duration_ms"] == 1234
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

    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=bad_base,
        )
    )
    assert isinstance(verdict, Verdict)
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# Blocking is recorded but NOT enforced in v1 (observe-only)
# --------------------------------------------------------------------------


def test_blocking_true_still_runs_main_observe_only(workspace, gate_base):
    # blocking: true must NOT short-circuit; main.py still runs and the verdict
    # still rides the success path. (Observe-only.)
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(
                files={"main.py": _MAIN_FAIL},
                manifest={"blocking": True},
            ),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


# --------------------------------------------------------------------------
# A1: an entrypoint ran but produced ZERO fragments is NOT a clean pass.
# --------------------------------------------------------------------------


def test_empty_fragments_is_evaluated_but_pass_none(workspace, gate_base):
    # main.py exits 0 with an empty JSON array. The stage didn't error, so
    # status is 'evaluated' — but `all([])` is vacuously True today, and an
    # entrypoint that produced zero fragments verified nothing, so pass MUST be
    # None, not True (A1).
    main_src = "import json\nprint(json.dumps([]))\n"
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
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
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
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
    def boom(*_a, **_k):
        raise FileNotFoundError("python3 not on PATH")

    monkeypatch.setattr(qa_gate_mod.subprocess, "Popen", boom)

    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    verdict = _verdict(result)
    assert isinstance(verdict, Verdict)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    # Fix 4: spawn failure never produced stdout -> raw_output None.
    assert _raw_output(result) is None


# --------------------------------------------------------------------------
# A4: a PRESENT but invalid timeout value logs a warning when coerced.
# --------------------------------------------------------------------------


def test_invalid_present_timeout_logs_coercion_warning(workspace, gate_base, gate_logs):
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(
                files={"main.py": _MAIN_PASS},
                manifest={"blocking": False, "deterministic": {"timeout_seconds": -5}},
            ),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
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
        gate_base_dir=gate_base,
        run_id="run-abc-123",
        experiment_id="exp-xyz",
    )
    unparseable = [m for m in _messages(gate_logs) if "unparseable_stdout" in m]
    assert len(unparseable) == 1
    assert "run-abc-123" in unparseable[0]


# --------------------------------------------------------------------------
# Verdict summary INFO log fires so a smoke is CloudWatch-verifiable.
# --------------------------------------------------------------------------


def test_verdict_summary_logged_at_info(workspace, gate_base, gate_info_logs):
    """After aggregating, the gate logs the verdict at INFO with status, pass,
    check count, and run_id so a deployed smoke can verify the gate ran from
    CloudWatch alone."""
    run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
        run_id="run-info-1",
    )
    info = [
        r for r in gate_info_logs.records
        if "qa_gate_verdict" in r.getMessage() and r.levelno == logging.INFO
    ]
    assert len(info) == 1
    msg = info[0].getMessage()
    assert "status=evaluated" in msg
    assert "pass=True" in msg
    assert "checks=1" in msg
    assert "run-info-1" in msg


def test_verdict_summary_logged_for_skipped(workspace, gate_base, gate_info_logs):
    """The summary log also fires on the skipped path (no main.py) so an empty
    qa folder is visible in CloudWatch, not silent."""
    run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
        run_id="run-skip-1",
    )
    info = [
        r for r in gate_info_logs.records
        if "qa_gate_verdict" in r.getMessage() and r.levelno == logging.INFO
    ]
    assert len(info) == 1
    msg = info[0].getMessage()
    assert "status=skipped" in msg
    assert "run-skip-1" in msg


# --------------------------------------------------------------------------
# A2: a runaway main.py whose stdout exceeds the 1MB cap -> stage error,
# and the runner must not buffer unbounded bytes (bounded capture).
# --------------------------------------------------------------------------


def test_main_py_stdout_over_cap_is_stage_error(workspace, gate_base):
    # Stream well past the 1MB cap on stdout.
    main_src = "import sys\nchunk = 'A' * 65536\nfor _ in range(40):\n    sys.stdout.write(chunk)\nsys.stdout.flush()\n"
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


# --------------------------------------------------------------------------
# A2: main.py exceeding the deterministic timeout -> stage error (killed).
# --------------------------------------------------------------------------


def test_main_py_timeout_is_stage_error(workspace, gate_base):
    main_src = "import time\ntime.sleep(30)\n"
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(
            files={"main.py": main_src},
            manifest={"blocking": False, "deterministic": {"timeout_seconds": 1}},
        ),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    verdict = _verdict(result)
    assert verdict.status == "error"
    assert verdict.pass_ is None
    # Fix 4: a killed subprocess never yielded usable stdout -> raw_output None.
    assert _raw_output(result) is None


def test_default_gate_root_honors_qa_gate_root_env(monkeypatch):
    """The gate root is env-configurable so the task def can point it at a
    writable mount; blank/unset falls back to the (Dockerfile-created) default."""
    monkeypatch.delenv("QA_GATE_ROOT", raising=False)
    assert qa_gate_mod._default_gate_root() == qa_gate_mod.DEFAULT_QA_GATE_ROOT
    monkeypatch.setenv("QA_GATE_ROOT", "/custom/writable-root")
    assert qa_gate_mod._default_gate_root() == "/custom/writable-root"
    monkeypatch.setenv("QA_GATE_ROOT", "   ")
    assert qa_gate_mod._default_gate_root() == qa_gate_mod.DEFAULT_QA_GATE_ROOT


# --------------------------------------------------------------------------
# Fix 1 (HIGH): raw_output is capped by ENCODED bytes, never the broker cap.
# decode(errors='replace') turns each invalid byte into U+FFFD (3 bytes), so a
# within-cap stdout of invalid bytes could decode to ~3x its size and blow the
# broker's 1 MiB cap. The returned raw_output must always encode to <= the cap.
# --------------------------------------------------------------------------


def test_raw_output_capped_by_encoded_bytes_for_invalid_utf8(workspace, gate_base):
    # main.py writes ~1 MiB (just under the cap) of invalid UTF-8 bytes to
    # stdout, with NO valid JSON. Under errors='replace' this would decode to
    # ~3 MiB; the returned raw_output must encode to <= _MAIN_STDOUT_CAP.
    near_cap = qa_gate_mod._MAIN_STDOUT_CAP - 4096
    main_src = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'\\xff' * {near_cap})\n"
        "sys.stdout.buffer.flush()\n"
    )
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    # Invalid bytes are not parseable JSON -> stage error, but raw_output is
    # still the (capped) decoded stdout.
    verdict = _verdict(result)
    assert verdict.status == "error"
    raw = _raw_output(result)
    assert raw is not None
    assert len(raw.encode("utf-8")) <= qa_gate_mod._MAIN_STDOUT_CAP


# --------------------------------------------------------------------------
# Fix 2 (MEDIUM): the broker token printed to STDOUT is redacted from
# raw_output (which the broker writes durably to main_output.json on S3).
# --------------------------------------------------------------------------


def test_raw_output_redacts_broker_token_printed_to_stdout(workspace, gate_base):
    secret = "tok-stdout-secret-1a2b3c4d5e"
    # main.py prints the literal broker token on stdout (not as JSON fragments).
    main_src = f"print('leaked BROKER_TOKEN={secret} to stdout')\n"
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    raw = _raw_output(result)
    assert raw is not None
    # The live token must NOT survive into the durable S3 main_output.json.
    assert secret not in raw
    assert qa_gate_mod._REDACTED in raw


# --------------------------------------------------------------------------
# Fix 3 (MEDIUM): a broker token in an author-emitted fragment string field
# (detail) is redacted before it lands in the aggregated verdict's checks.
# --------------------------------------------------------------------------


def test_fragment_detail_redacts_broker_token(workspace, gate_base):
    secret = "tok-fragment-secret-9z8y7x6w5v"
    main_src = (
        "import json\n"
        f"print(json.dumps([{{'name': 'leaky', 'passed': False, 'detail': 'saw BROKER_TOKEN={secret} oops'}}]))\n"
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    leaky = [c for c in verdict.checks if c["name"] == "leaky"]
    assert len(leaky) == 1
    detail = leaky[0]["detail"]
    # The live token must NOT survive into the verdict (it travels to gp-api +
    # Braintrust + the durable verdict.json).
    assert secret not in detail
    assert qa_gate_mod._REDACTED in detail
    # The whole serialized verdict is clean too.
    assert secret not in json.dumps(verdict.to_dict())


# ==========================================================================
# eval.md: the AI-evaluator stage (auto-detected second entrypoint, contract B)
#
# The gate auto-detects qa/eval.md and runs ONE evaluator agent via the
# injected `evaluator_runner` adapter. Tests inject a FakeEvaluator that mirrors
# the real run_evaluator contract: it writes a fragment array to the injected
# result_file_path and returns an EvaluatorResult; the engine reads the
# canonical fragments back from that file. The evaluator stage produces
# `type: "agent"` fragments and contributes its model cost to the verdict's
# cost_usd (the deterministic stage contributes 0).
# ==========================================================================


def test_evaluator_fragments_read_from_injected_result_file(workspace, gate_base):
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True, "score": 4.5}])
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "Judge the artifact for faithfulness."}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
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


def test_eval_only_folder_has_no_raw_output(workspace, gate_base):
    """An eval.md-only folder never spawns main.py, so there is no deterministic
    stdout to write to S3 — raw_output is None even though the gate ran."""
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert _verdict(result).status == "evaluated"
    assert _raw_output(result) is None


def test_evaluator_missing_result_file_is_stage_error(workspace, gate_base):
    fake = FakeEvaluator(write_file=False)
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_evaluator_unparseable_result_file_is_stage_error(workspace, gate_base):
    fake = FakeEvaluator(write_file=True, file_contents="{not json")
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_evaluator_result_not_array_is_stage_error(workspace, gate_base):
    fake = FakeEvaluator(write_file=True, file_contents=json.dumps({"name": "x", "passed": True}))
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_evaluator_runner_status_error_makes_verdict_error(workspace, gate_base):
    fake = FakeEvaluator(
        write_file=True,
        result=EvaluatorResult(fragments=[], status="error"),
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_evaluator_runner_raising_is_stage_error_fail_open(workspace, gate_base):
    """A runner that raises (a real bridge/SDK defect) is FAIL-OPEN: the stage
    surfaces an error verdict, the gate never re-raises, the run still publishes."""
    fake = FakeEvaluator(raise_exc=RuntimeError("evaluator bridge blew up"))
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_eval_md_present_without_runner_is_stage_error(workspace, gate_base):
    """The gate detects eval.md but no evaluator_runner was injected (a wiring
    defect). FAIL-OPEN: this surfaces an error verdict, never an exception."""
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=None,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None


def test_both_stages_pass_true_only_when_all_fragments_pass(workspace, gate_base):
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    # Both stages ran (deterministic-first), fragments from both present.
    names = sorted(c["name"] for c in verdict.checks)
    assert names == ["faithfulness", "grounding"]
    assert len(fake.calls) == 1


def test_both_stages_one_failing_fragment_yields_pass_false(workspace, gate_base):
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": False}])
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


def test_both_stages_forward_deterministic_raw_output(workspace, gate_base):
    """When both stages run, raw_output is the DETERMINISTIC main.py stdout
    (the evaluator fragments live in the gate dir, not the raw output)."""
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    raw = _raw_output(result)
    assert raw is not None
    assert json.loads(raw) == [{"name": "grounding", "passed": True, "score": 0.91}]


def test_insufficient_budget_for_both_stages_skips_both(workspace, gate_base):
    """When both entrypoints are present, the pre-flight budget sums BOTH
    stages' ceilings (deterministic.timeout_seconds + agent.timeout_seconds);
    insufficient budget skips spawning anything (decision 11)."""
    manifest = {
        "blocking": False,
        "deterministic": {"timeout_seconds": 120},
        "agent": {"timeout_seconds": 300},
    }
    main_marker = workspace + "/MAIN_RAN_BUDGET"
    main_src = f"open({main_marker!r}, 'w').close()\nimport json\nprint(json.dumps([]))\n"
    fake = FakeEvaluator()
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(
                files={"main.py": main_src, "eval.md": "judge"},
                manifest=manifest,
            ),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            # 420 required (120 + 300); give less.
            remaining_budget_seconds=400.0,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None
    assert not os.path.exists(main_marker), "no stage may run when budget is insufficient"
    assert len(fake.calls) == 0
    assert any("insufficient_budget" in v for v in verdict.violations)
    # Required budget reflects the SUM of both present stages.
    assert any("420" in v for v in verdict.violations)


def test_blocking_true_still_runs_both_stages_observe_only(workspace, gate_base):
    # blocking: true must NOT short-circuit; both stages still run and the
    # verdict still rides the success path. (Observe-only.)
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    verdict = _verdict(
        run_qa_gate(
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
    )
    # Deterministic stage failed, but the evaluator STILL ran (no short-circuit).
    assert len(fake.calls) == 1
    assert verdict.status == "evaluated"
    assert verdict.pass_ is False


def test_evaluator_empty_fragments_is_evaluated_but_pass_none(workspace, gate_base):
    # The evaluator ran to completion (status 'ok') but emitted an empty
    # fragment array. status is 'evaluated' (no stage error), but `all([])` is
    # vacuously True today — pass MUST be None, not True. An entrypoint that
    # produced zero fragments verified nothing.
    fake = FakeEvaluator(fragments=[])
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.checks == []
    assert verdict.pass_ is None


def test_verdict_cost_is_zero_for_deterministic_only(workspace, gate_base):
    """A deterministic-only run contributes 0 to cost_usd (no model spend)."""
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=_never_called_evaluator,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.cost_usd == 0.0


def test_verdict_cost_sums_evaluator_model_cost(workspace, gate_base):
    """cost_usd is summed across the stages that ran: 0 for the deterministic
    stage plus the evaluator's model cost. With both stages the verdict carries
    exactly the evaluator's cost (decision 12)."""
    fake = FakeEvaluator(
        fragments=[{"name": "faithfulness", "passed": True}],
        result=EvaluatorResult(
            fragments=[{"name": "faithfulness", "passed": True}],
            cost_usd=0.0731,
            status="ok",
        ),
    )
    # FakeEvaluator with an explicit result does NOT write the file; force a write.
    fake.write_file = True
    fake.fragments = [{"name": "faithfulness", "passed": True}]
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "evaluated"
    assert verdict.cost_usd == pytest.approx(0.0731)


def test_evaluator_fragment_detail_redacts_broker_token(workspace, gate_base):
    """A6 / contract D: a leaked BROKER_TOKEN that an evaluator prints into a
    fragment detail must be REDACTED before it lands in the aggregated verdict
    (the verdict travels to gp-api + Braintrust + the durable verdict.json).
    Evaluator (`type: agent`) fragments pass through the SAME _normalize_fragment
    redaction the deterministic fragments do."""
    secret = "tok-eval-fragment-secret-7q6r5s4t"
    fake = FakeEvaluator(
        fragments=[{"name": "faithfulness", "passed": False, "detail": f"saw BROKER_TOKEN={secret} in artifact"}]
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"eval.md": "judge"}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
            remaining_budget_seconds=BIG_BUDGET,
            evaluator_runner=fake,
            gate_base_dir=gate_base,
        )
    )
    agent = [c for c in verdict.checks if c["name"] == "faithfulness"]
    assert len(agent) == 1
    assert agent[0]["type"] == "agent"
    detail = agent[0]["detail"]
    # The live token must NOT survive into an evaluator fragment's detail.
    assert secret not in detail
    assert qa_gate_mod._REDACTED in detail
    # The whole serialized verdict is clean too.
    assert secret not in json.dumps(verdict.to_dict())


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


# ==========================================================================
# eval_transcript: the 3rd run_qa_gate tuple element (the JSONL evaluator
# transcript, REDACTED runner-side by the gate's _redact_secrets chokepoint
# before it is forwarded to the broker for the durable S3 eval_transcript.jsonl
# write). None for a main-only folder (no evaluator ran); the redacted string
# for an eval.md folder.
# ==========================================================================


def test_run_evaluator_threads_redacted_transcript_to_caller(workspace, gate_base):
    """The evaluator's RAW transcript (carrying the live BROKER_TOKEN) is
    REDACTED by the gate's _redact_secrets before it leaves the gate, then
    returned as the 3rd tuple element. The token must be gone; the result must
    still be parseable JSONL (redaction is value-only, structure-preserving)."""
    token = _broker_env()["BROKER_TOKEN"]  # "tok-123"
    raw_transcript = "\n".join([
        json.dumps({"turn": 1, "kind": "assistant", "text": "grading", "tools": []}),
        json.dumps({
            "turn": 1,
            "kind": "tool_result",
            "results": [{"tool_use_id": "t1", "is_error": False,
                         "content": f'headers {{"X-Broker-Token": "{token}"}}'}],
        }),
        json.dumps({"turn": 0, "kind": "result", "status": "ok", "subtype": "result",
                    "is_error": False, "num_turns": 2, "session_id": "sess-x",
                    "cost_usd": 0.04, "duration_ms": 900}),
    ])
    fake = FakeEvaluator(
        fragments=[{"name": "faithfulness", "passed": True}],
        eval_transcript=raw_transcript,
    )
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    transcript = _transcript(result)
    assert transcript is not None
    # The live token is masked; the redaction marker is present.
    assert token not in transcript
    assert qa_gate_mod._REDACTED in transcript
    # Still parseable JSONL — every non-empty line is a JSON object.
    lines = [ln for ln in transcript.splitlines() if ln.strip()]
    assert len(lines) == 3
    for ln in lines:
        json.loads(ln)


def test_eval_only_folder_returns_transcript_main_only_returns_none(workspace, gate_base):
    """The 3rd tuple element distinguishes three states:
    - main.py-only (no evaluator ran)        -> None
    - eval.md, evaluator emitted a transcript -> the redacted string
    - eval.md, evaluator emitted empty ''      -> '' (ran but empty)."""
    # main.py-only: no evaluator, so transcript is None.
    main_only = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=_never_called_evaluator,
        gate_base_dir=gate_base,
    )
    assert _transcript(main_only) is None

    # eval.md with an empty transcript -> '' (ran but produced nothing).
    fake_empty = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}], eval_transcript="")
    eval_empty = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake_empty,
        gate_base_dir=gate_base,
    )
    assert _transcript(eval_empty) == ""

    # eval.md with a non-empty transcript -> the string.
    fake_full = FakeEvaluator(
        fragments=[{"name": "faithfulness", "passed": True}],
        eval_transcript=json.dumps({"turn": 0, "kind": "result", "status": "ok"}),
    )
    eval_full = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake_full,
        gate_base_dir=gate_base,
    )
    assert _transcript(eval_full) == json.dumps({"turn": 0, "kind": "result", "status": "ok"})


def test_both_stages_transcript_is_from_evaluator(workspace, gate_base):
    """When both stages run, the 3rd tuple element is the evaluator's transcript
    (the deterministic stage has no transcript). raw_output is still the
    deterministic stdout — the two are independent."""
    fake = FakeEvaluator(
        fragments=[{"name": "faithfulness", "passed": True}],
        eval_transcript=json.dumps({"turn": 0, "kind": "result", "status": "ok"}),
    )
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": _MAIN_PASS, "eval.md": "judge"}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert json.loads(_transcript(result))["kind"] == "result"
    assert json.loads(_raw_output(result)) == [{"name": "grounding", "passed": True, "score": 0.91}]


def test_skipped_and_no_qa_have_no_transcript(workspace, gate_base):
    """Skipped (qa folder, no entrypoints) and no-qa (None envelope) both carry
    no evaluator transcript."""
    skipped = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={}),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    assert _transcript(skipped) is None

    no_qa = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=None,
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    assert no_qa is None


# ==========================================================================
# FIX 1 (security): the gate's _SECRET_PATTERNS must mask the JSON-quoted
# `"X-Broker-Token": "<value>"` shape (and a `Bearer <value>` shape) the runner's
# main.py redaction already covers via _BROKER_TOKEN_PATTERN / _BEARER_TOKEN_PATTERN.
# The gate's key=value pattern uses a key group of [A-Za-z0-9_]*, which does NOT
# match a key containing '-' nor span the closing '"' on the JSON key, so a token
# OTHER than the live BROKER_TOKEN printed in that shape leaks today.
# ==========================================================================


def test_x_broker_token_json_shape_redacted_in_fragment_detail_even_when_not_live(workspace, gate_base):
    """A token in the `"X-Broker-Token": "<other>"` JSON shape — a DIFFERENT
    value than the live BROKER_TOKEN — must be redacted in a fragment detail.
    The explicit-token replacement can't catch it (wrong value); the gate's old
    key=value pattern can't either (its key group is [A-Za-z0-9_]* and the JSON
    key's closing '"' breaks adjacency). Only the ported _BROKER_TOKEN_PATTERN
    (mirroring runner/main.py) masks it. This detail flows through
    _normalize_fragment into verdict.checks, which egresses to gp-api +
    Braintrust + the durable verdict.json."""
    live = "tok-live-broker-0000000000"
    other = "tok-OTHER-not-the-live-one-1234abcd"
    detail = f'headers were {{"X-Broker-Token": "{other}"}}'
    main_src = (
        "import json\n"
        f"print(json.dumps([{{'name': 'leaky', 'passed': False, 'detail': {json.dumps(detail)}}}]))\n"
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": live},
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    leaky = [c for c in verdict.checks if c["name"] == "leaky"]
    assert len(leaky) == 1
    leaky_detail = leaky[0]["detail"]
    # The non-live token value must NOT survive into the verdict.
    assert other not in leaky_detail
    assert other not in json.dumps(verdict.to_dict())
    assert qa_gate_mod._REDACTED in leaky_detail
    # The key is preserved so the structure stays diagnosable.
    assert "X-Broker-Token" in leaky_detail


def test_x_broker_token_raw_stdout_shape_redacted_in_raw_output(workspace, gate_base):
    """When main.py prints the structural `"X-Broker-Token": "<other>"` shape
    directly to stdout (the unescaped form the Claude SDK serializes a headers
    dict into), the value must be masked in raw_output (the durable S3
    main_output.json) by the ported _BROKER_TOKEN_PATTERN, even when <other> is
    not the live BROKER_TOKEN. Non-JSON stdout -> stage error, but raw_output is
    still the redacted decoded stdout."""
    live = "tok-live-broker-0000000000"
    other = "tok-OTHER-not-the-live-one-1234abcd"
    line = f'config: "X-Broker-Token": "{other}"'
    main_src = f"print({json.dumps(line)})\n"
    result = run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"main.py": main_src}),
        workspace_dir=workspace,
        broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": live},
        remaining_budget_seconds=BIG_BUDGET,
        gate_base_dir=gate_base,
    )
    raw = _raw_output(result)
    assert raw is not None
    assert other not in raw
    assert qa_gate_mod._REDACTED in raw
    assert "X-Broker-Token" in raw


def test_bearer_token_shape_redacted_in_fragment_detail(workspace, gate_base):
    """A `Bearer <value>` token in a fragment detail must be redacted even when
    `<value>` is not the live BROKER_TOKEN. Mirrors main.py's _BEARER_TOKEN_PATTERN."""
    live = "tok-live-broker-aaaaaaaaaa"
    bearer_secret = "bearersecretvalue1234567890"
    main_src = (
        "import json\n"
        f'print(json.dumps([{{"name": "leaky", "passed": False, '
        f'"detail": "Authorization: Bearer {bearer_secret}"}}]))\n'
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": live},
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    leaky = [c for c in verdict.checks if c["name"] == "leaky"]
    assert len(leaky) == 1
    detail = leaky[0]["detail"]
    assert bearer_secret not in detail
    assert qa_gate_mod._REDACTED in detail


# ==========================================================================
# FIX 3 (error-reporting): synthetic top-level `violations` strings built from
# arbitrary exception text must be routed through _redact_secrets before egress.
# Two paths: (a) run_qa_gate's own internal-error catch, and (b) main.py's
# _run_qa_gate_hook 'qa_gate_hook_error' violation.
# ==========================================================================


def test_internal_error_violation_redacts_broker_token(workspace, gate_base, monkeypatch):
    """The fail-open internal-error catch in run_qa_gate builds a violation from
    arbitrary exception text. A BROKER_TOKEN embedded in that exception message
    must be redacted before it lands in verdict.violations (which travels to
    Braintrust + the durable verdict.json)."""
    secret = "tok-internal-error-secret-9a8b7c"

    def boom(*_a, **_k):
        raise RuntimeError(f"boom leaked BROKER_TOKEN={secret} during materialize")

    # Force the internal-error path by making materialization raise with the
    # secret in the message.
    monkeypatch.setattr(qa_gate_mod, "_materialize", boom)

    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    joined = " ".join(verdict.violations)
    assert secret not in joined
    assert secret not in json.dumps(verdict.to_dict())
    # The error is still discoverable.
    assert any("qa_gate_internal_error" in v for v in verdict.violations)


def test_hook_error_violation_redacts_broker_token(workspace, gate_base, monkeypatch):
    """main.py's _run_qa_gate_hook builds a 'qa_gate_hook_error' violation from
    arbitrary exception text on a bridge/marshaling defect. A BROKER_TOKEN in
    that exception message must be redacted before it enters the Verdict."""
    import pmf_engine.runner.main as runner_main

    secret = "tok-hook-error-secret-1q2w3e4r"

    monkeypatch.setenv("BROKER_URL", "https://broker.test")
    monkeypatch.setenv("BROKER_TOKEN", secret)

    # Make the gate spawn itself raise inside the hook with the secret in the
    # message, exercising the hook's own except branch (defense-in-depth path).
    def boom(*_a, **_k):
        raise RuntimeError(f"bridge blew up; saw BROKER_TOKEN={secret} in env")

    monkeypatch.setattr(runner_main, "run_qa_gate", boom)

    class _Cfg:
        qa_envelope = {"resolved_qa_version_ids": {"manifest.json": "v-man"}}
        run_id = "run-hook-1"
        experiment_id = "exp-hook-1"

    result = asyncio.run(
        runner_main._run_qa_gate_hook(
            config=_Cfg(),
            artifact_bytes=ARTIFACT,
            workspace_dir=workspace,
            remaining_budget_seconds=BIG_BUDGET,
        )
    )
    assert result is not None
    verdict, _raw, _transcript = result
    assert verdict.status == "error"
    joined = " ".join(verdict.violations)
    assert secret not in joined
    assert secret not in json.dumps(verdict.to_dict())
    assert any("qa_gate_hook_error" in v for v in verdict.violations)
    # The defense-in-depth fallback carries no evaluator transcript.
    assert _transcript is None


# ==========================================================================
# FIX 1 (security HIGH): _normalize_fragment redacts the BROKER_TOKEN at ANY
# depth, not only in top-level string values. A token nested inside a fragment's
# `evidence` dict, a `samples` list, or any deeper structure would otherwise
# reach the durable verdict.json + the Braintrust span UNREDACTED, because the
# old code only ran _redact_secrets over top-level string values of the check
# dict. Redaction must recurse into nested dicts/lists while leaving structure
# and non-string leaves (ints/floats/bools/None) intact.
# ==========================================================================


def test_normalize_fragment_redacts_token_nested_in_dict_and_list():
    """The live BROKER_TOKEN planted in a NESTED field (a dict value, a list
    element) of a fragment must be redacted by _normalize_fragment, while a
    non-secret nested value (surrounding text, a numeric score, a nested key
    name) is preserved intact and the structure is unchanged."""
    token = "tok-nested-secret-9f8a7b6c5d4e"
    broker_env = {"BROKER_URL": "https://broker.test", "BROKER_TOKEN": token}
    frag = {
        "name": "leaky",
        "passed": False,
        "score": 0.42,
        "evidence": {
            "leaked": f"x-api-key: {token}",
            "note": "this surrounding text must survive",
            "depth": {"deeper": f"still {token} here", "kept": "deep-keep"},
        },
        "samples": [f"echoed {token}", "clean sample", {"inner": f"again {token}"}],
        "count": 3,
        "ok": True,
    }
    check = qa_gate_mod._normalize_fragment(frag, "deterministic", broker_env)

    serialized = json.dumps(check)
    # The token is gone EVERYWHERE — nested dict values, nested lists, deep dicts.
    assert token not in serialized
    assert qa_gate_mod._REDACTED in serialized

    # Non-secret nested values are preserved intact (text, numbers, bools, keys).
    assert "this surrounding text must survive" in check["evidence"]["note"]
    assert check["evidence"]["depth"]["kept"] == "deep-keep"
    assert check["score"] == 0.42
    assert check["count"] == 3
    assert check["ok"] is True
    # Structure is preserved: evidence stays a dict, samples stays a list of the
    # same length with the nested dict still a dict.
    assert isinstance(check["evidence"], dict)
    assert isinstance(check["samples"], list)
    assert len(check["samples"]) == 3
    assert isinstance(check["samples"][2], dict)
    # The clean sample text is preserved verbatim.
    assert "clean sample" in check["samples"]
    # type tagging still applied.
    assert check["type"] == "deterministic"


def test_nested_fragment_token_redacted_through_full_gate(workspace, gate_base):
    """End-to-end: main.py emits a fragment with the live BROKER_TOKEN buried in
    a nested `evidence` dict and a `samples` list. The token must not survive
    into the aggregated verdict (which egresses to gp-api + Braintrust + the
    durable verdict.json), while non-secret nested text/numbers are preserved."""
    secret = "tok-nested-e2e-secret-1a2b3c4d5e"
    # main.py BUILDS the fragment itself so Python-invalid JSON literals
    # (false/true) never land in the source. The secret is injected as a string.
    main_src = (
        "import json\n"
        f"s = {json.dumps(secret)}\n"
        "frag = {'name': 'leaky', 'passed': False,\n"
        "        'evidence': {'leaked': 'saw ' + s + ' in headers', 'kept': 'evidence-keep'},\n"
        "        'samples': ['echoed ' + s, 'sample-keep'],\n"
        "        'score': 0.7}\n"
        "print(json.dumps([frag]))\n"
    )
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": main_src}),
            workspace_dir=workspace,
            broker_env={"BROKER_URL": "https://broker.test", "BROKER_TOKEN": secret},
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    leaky = [c for c in verdict.checks if c["name"] == "leaky"]
    assert len(leaky) == 1
    serialized = json.dumps(verdict.to_dict())
    assert secret not in serialized
    assert qa_gate_mod._REDACTED in serialized
    # Non-secret nested values survive.
    assert leaky[0]["evidence"]["kept"] == "evidence-keep"
    assert "sample-keep" in leaky[0]["samples"]
    assert leaky[0]["score"] == 0.7


def test_invalid_fragment_synthetic_detail_redacts_nested_token():
    """A malformed fragment (missing the required bool `passed`) is replaced by
    the synthetic `invalid_fragment` check whose `detail` embeds the rejected
    fragment via json.dumps. A BROKER_TOKEN buried in that rejected fragment must
    NOT survive into the synthetic detail — that detail egresses to the durable
    verdict.json + the Braintrust span exactly like a valid fragment's. The
    non-secret structure of the rejected fragment is still echoed for diagnosis."""
    token = "tok-invalid-frag-secret-7e0f70d6"
    broker_env = {"BROKER_URL": "https://broker.test", "BROKER_TOKEN": token}
    frag = {"name": "missing_passed", "evidence": {"buried": f"saw {token} in headers"}}
    check = qa_gate_mod._normalize_fragment(frag, "agent", broker_env)

    assert check["name"] == "invalid_fragment"
    assert check["passed"] is False
    assert check["type"] == "agent"
    serialized = json.dumps(check)
    assert token not in serialized
    assert qa_gate_mod._REDACTED in check["detail"]
    # The rejected fragment is still echoed for diagnosis: its non-secret name
    # and the surrounding text survive so an author can identify the bad fragment.
    assert "missing_passed" in check["detail"]
    assert "in headers" in check["detail"]


def test_invalid_non_dict_fragment_synthetic_detail_redacts_token():
    """A fragment that is not a dict at all (e.g. a bare string the evaluator
    printed) is also routed to the synthetic `invalid_fragment` branch via
    json.dumps. A BROKER_TOKEN inside that string must be masked in the detail."""
    token = "tok-nondict-frag-secret-1a2b3c4d"
    broker_env = {"BROKER_URL": "https://broker.test", "BROKER_TOKEN": token}
    check = qa_gate_mod._normalize_fragment(f"raw fragment with {token} leaked", "agent", broker_env)

    assert check["name"] == "invalid_fragment"
    assert token not in json.dumps(check)
    assert qa_gate_mod._REDACTED in check["detail"]


# ==========================================================================
# FIX 2 (security MEDIUM): when _redact_secrets is called with a non-empty
# broker_env that LACKS a usable BROKER_TOKEN, the bare-token replacement (the
# ONLY thing that catches the bare uuid) is silently a no-op — a single point of
# failure. The gate must log an ERROR (observability — redaction is degraded)
# and still apply the shape-based regexes. We do NOT add a broad UUID pattern:
# a legitimate run_id/uuid in a fragment must NOT be over-redacted.
# ==========================================================================


def test_redact_secrets_flags_degraded_state_when_token_missing():
    """A non-empty broker_env that lacks a usable BROKER_TOKEN means the bare-
    token replacement can't run — the redaction is degraded. _redact_secrets must
    log an error so the degraded state is observable, while still applying the
    shape-based regexes (so a Bearer/X-Broker-Token shape is still masked)."""
    broker_env = {"BROKER_URL": "https://broker.test"}
    text = "Authorization: Bearer leakedvalue1234567890 in headers"
    with mock.patch.object(qa_gate_mod.logger, "error") as err:
        out = qa_gate_mod._redact_secrets(text, broker_env)
    # The degraded state is flagged at error level.
    assert err.call_count == 1
    # Shape-based regexes STILL run — the Bearer value is masked even with no token.
    assert "leakedvalue1234567890" not in out
    assert qa_gate_mod._REDACTED in out


def test_redact_secrets_does_not_flag_or_over_redact_with_valid_token():
    """With a usable BROKER_TOKEN, redaction is NOT degraded: no error is logged,
    and a normal UUID (e.g. a run_id) that is NOT the token is preserved — the
    fix must not introduce a broad UUID pattern that over-redacts."""
    token = "tok-valid-broker-aaaaaaaaaa"
    broker_env = {"BROKER_URL": "https://broker.test", "BROKER_TOKEN": token}
    run_id = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    text = f"processing run_id={run_id} normally"
    with mock.patch.object(qa_gate_mod.logger, "error") as err:
        out = qa_gate_mod._redact_secrets(text, broker_env)
    assert err.call_count == 0
    # A legitimate uuid run_id is NOT over-redacted.
    assert run_id in out
    assert qa_gate_mod._REDACTED not in out


def test_redact_secrets_empty_broker_env_does_not_flag():
    """An EMPTY broker_env carries no broker context at all (no broker configured),
    so the degraded-redaction error must NOT fire — flagging is only for the case
    where a broker IS expected but the token is missing/empty."""
    with mock.patch.object(qa_gate_mod.logger, "error") as err:
        out = qa_gate_mod._redact_secrets("nothing secret here", {})
    assert err.call_count == 0
    assert out == "nothing secret here"


# ==========================================================================
# FIX 4 (test-engineer): lock four existing guards that survived mutation. Each
# asserts a specific value and would FAIL under the stated mutation.
# ==========================================================================


def test_budget_exactly_equal_to_required_runs_stage(workspace, gate_base):
    """When remaining_budget_seconds EXACTLY equals the required ceiling, the
    stage RUNS — the pre-flight uses strict `<` (only spawns nothing when budget
    is STRICTLY less than required). Mutation caught: `<` -> `<=` would skip the
    exactly-equal case and return an error verdict."""
    manifest = {"blocking": False, "deterministic": {"timeout_seconds": 120}}
    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope=_envelope(files={"main.py": _MAIN_PASS}, manifest=manifest),
            workspace_dir=workspace,
            broker_env=_broker_env(),
            # EXACTLY equal to the 120s required ceiling.
            remaining_budget_seconds=120.0,
            gate_base_dir=gate_base,
        )
    )
    # Equal budget is sufficient: the stage runs and the fragment is evaluated,
    # NOT an insufficient_budget error.
    assert verdict.status == "evaluated"
    assert verdict.pass_ is True
    assert not any("insufficient_budget" in v for v in verdict.violations)


def test_truthy_nonbool_passed_replaced_by_invalid_fragment():
    """A fragment with a TRUTHY but NON-BOOL `passed` (e.g. the int 1) is invalid
    and must be replaced by the synthetic `invalid_fragment` failing check.
    Mutation caught: dropping the `isinstance(frag.get("passed"), bool)` clause
    would let `passed: 1` through as a real (truthy) check."""
    frag = {"name": "x", "passed": 1}
    check = qa_gate_mod._normalize_fragment(frag, "deterministic", _broker_env())
    assert check["name"] == "invalid_fragment"
    assert check["passed"] is False
    assert check["type"] == "deterministic"
    assert "invalid fragment replaced" in check["detail"]


def test_materialize_rejects_subdir_name_via_basename_equality_clause(tmp_path):
    """`_materialize` rejects a qa file name that is NOT its own basename
    (`sub/dir/x.py`) with a specific ``ValueError`` BEFORE any write. This name
    has no `..` and no leading `/`, so ONLY the `name != os.path.basename(name)`
    clause rejects it. Mutation caught: dropping that clause changes the failure
    from a deliberate ``ValueError('unsafe qa file basename')`` raised pre-write
    to an incidental ``FileNotFoundError`` (the intermediate dirs don't exist) —
    so asserting the SPECIFIC ValueError + message isolates the clause."""
    gate_dir = str(tmp_path / "gate")
    os.mkdir(gate_dir)
    with pytest.raises(ValueError, match="unsafe qa file basename") as exc:
        qa_gate_mod._materialize(gate_dir, {"blocking": False}, {"sub/dir/x.py": "print('nested')"})
    assert "sub/dir/x.py" in str(exc.value)
    # The unsafe file is rejected pre-write: neither its nested name nor any
    # intermediate `sub/` dir is created under the gate dir. (manifest.json is
    # written first and legitimately remains.)
    assert not os.path.exists(os.path.join(gate_dir, "sub"))
    assert not os.path.exists(os.path.join(gate_dir, "sub", "dir", "x.py"))


def test_path_traversal_qa_file_escapes_nothing_through_full_gate(workspace, gate_base):
    """End-to-end: a malicious `../evil.py` shipped alongside a valid `main.py`
    (so materialization actually runs) surfaces an error verdict and writes
    NOTHING outside the gate dir. The unsafe name is rejected pre-write by
    `_materialize`'s guards; fail-open turns the rejection into an error verdict."""
    sentinel_parent = os.path.dirname(os.path.realpath(gate_base))
    evil_target = os.path.join(sentinel_parent, "evil.py")
    if os.path.exists(evil_target):
        os.remove(evil_target)

    verdict = _verdict(
        run_qa_gate(
            artifact_bytes=ARTIFACT,
            qa_envelope={
                "manifest": {"blocking": False},
                "files": {"main.py": _MAIN_PASS, "../evil.py": f"open({evil_target!r}, 'w').close()"},
                "resolved_qa_version_ids": {"manifest.json": "v-man"},
            },
            workspace_dir=workspace,
            broker_env=_broker_env(),
            remaining_budget_seconds=BIG_BUDGET,
            gate_base_dir=gate_base,
        )
    )
    assert verdict.status == "error"
    assert verdict.pass_ is None
    assert not os.path.exists(evil_target)


def test_agent_max_turns_clamped_to_engine_ceiling_before_evaluator(workspace, gate_base):
    """A manifest agent.max_turns above the engine ceiling is clamped to
    _MAX_AGENT_TURNS before reaching the evaluator. The value the evaluator
    actually receives (params.max_turns) must equal _MAX_AGENT_TURNS, not the
    author's 999. Mutation caught: dropping the `min(..., _MAX_AGENT_TURNS)`
    clamp would forward the raw 999."""
    fake = FakeEvaluator(fragments=[{"name": "faithfulness", "passed": True}])
    manifest = {"blocking": False, "agent": {"max_turns": 999}}
    run_qa_gate(
        artifact_bytes=ARTIFACT,
        qa_envelope=_envelope(files={"eval.md": "judge"}, manifest=manifest),
        workspace_dir=workspace,
        broker_env=_broker_env(),
        remaining_budget_seconds=BIG_BUDGET,
        evaluator_runner=fake,
        gate_base_dir=gate_base,
    )
    assert len(fake.calls) == 1
    assert fake.calls[0].max_turns == qa_gate_mod._MAX_AGENT_TURNS
    # Sanity: the author's raw value was NOT forwarded.
    assert fake.calls[0].max_turns != 999
