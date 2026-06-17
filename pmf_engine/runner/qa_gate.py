"""PMF QA gate engine (v1 DETERMINISTIC-ONLY, observe-only).

Grades the exact artifact bytes a run produced against an experiment's qa/
folder and emits a Verdict that ALWAYS rides the success/publish path. v1 is
DETERMINISTIC-ONLY (one ``qa/main.py`` subprocess, no AI evaluator, no
``eval.md``) and OBSERVE-ONLY: the gate never blocks. A gate error becomes a
Verdict with ``status == "error"`` and the run still publishes (fail-open);
there is no quarantine, no qa_gate_failed report, no fail-closed branch.

The qa folder is convention-based (contract B):
  - ``qa/main.py``  -> the single deterministic entrypoint, run as a subprocess.
Absent (qa folder present, no main.py) -> ``status == "skipped"``.

The qa files are materialized into a PRIVATE dir OUTSIDE ``/workspace`` AND
OUTSIDE ``/tmp`` (the runner's log sweep collects /tmp .md/.json — see
runner/main.py ``_collect_log_files`` + ``_SAFE_TMP_EXTENSIONS``), then deleted
when the gate finishes.

``run_qa_gate`` returns a ``(verdict, raw_output)`` tuple (or ``None`` when no
qa folder): ``raw_output`` is the raw ``main.py`` stdout the runner forwards to
the broker for the durable S3 ``verdict.json`` write (contract D).

See ``~/work/docs/pmf-qa-gate-contracts.md`` contracts A/B/C/D.
"""

from __future__ import annotations

import collections
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal

from shared.logger import get_logger

logger = get_logger(__name__)

# Dedicated, non-swept materialization root. The runner sweeps the literal
# "/tmp" (and the whole workspace_dir) for log files, so qa bytes — the
# verdict — must land somewhere neither sweep touches.
# A10: the root itself is intentionally never swept/removed — the Fargate task
# is single-use, so the container is torn down after one run; only the per-run
# mkdtemp subdir under it is rmtree'd (in the finally below).
DEFAULT_QA_GATE_ROOT = "/qa-gate"


def _default_gate_root() -> str:
    """Resolve the gate's materialization root. Honors the QA_GATE_ROOT env var
    (so the task def can relocate it to a writable mount without an image
    rebuild), else DEFAULT_QA_GATE_ROOT. The runner image creates /qa-gate owned
    by the non-root `agent` user, so the default is writable in the container."""
    return os.environ.get("QA_GATE_ROOT", "").strip() or DEFAULT_QA_GATE_ROOT

# Platform defaults (contract A). Authors override via qa/manifest.json.
_DEFAULT_DETERMINISTIC_TIMEOUT = 120

_MAIN_PY = "main.py"

# stdout cap for the deterministic subprocess (contract B: 1 MB).
_MAIN_STDOUT_CAP = 1 * 1024 * 1024
# Last N bytes of stderr folded into the synthetic main_py_exit fragment detail.
_STDERR_TAIL = 4 * 1024

# A6: marker that replaces a redacted secret in folded stderr.
_REDACTED = "[REDACTED]"

# A6: secret-ish patterns masked in the stderr tail before it lands in the
# Verdict (which travels to gp-api + Braintrust). The explicit BROKER_TOKEN
# value (from broker_env) is masked separately; these catch common token shapes
# a misbehaving main.py might print without us knowing the literal.
_SECRET_PATTERNS = [
    # Bearer tokens.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    # AWS access key ids.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # key=value / key: value where the key name looks secret-ish.
    re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)[A-Za-z0-9_]*)"
        r"\s*[=:]\s*[\"']?([^\s\"']{4,})"
    ),
]

# Serialized-verdict cap (contract C: 8 KB protects the callback budget).
_VERDICT_BYTE_CAP = 8 * 1024

VerdictStatus = Literal["evaluated", "error", "skipped"]


@dataclass
class Verdict:
    """Engine output (contract C). ``pass_`` carries the ``pass`` field
    (``pass`` is a Python keyword); ``to_dict`` emits it under the wire key
    ``pass`` and applies the 8 KB serialization cap."""

    status: VerdictStatus
    verdict_version: int = 1
    qa_version_ids: dict = field(default_factory=dict)
    pass_: bool | None = None
    checks: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    duration_ms: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        """Serialize to the contract-C wire shape, capped at 8 KB.

        Truncation order (contract C): drop ``violations`` first, then per-check
        ``detail``, then strip every non-essential check field (keeping
        ``name``/``passed``). ``pass``/``status``/version/ids always survive.
        """
        checks: list[dict] = [dict(c) for c in self.checks]
        violations: list[str] = list(self.violations)

        def _assemble() -> dict:
            return {
                "verdict_version": self.verdict_version,
                "qa_version_ids": self.qa_version_ids,
                "status": self.status,
                "pass": self.pass_,
                "checks": checks,
                "violations": violations,
                "duration_ms": self.duration_ms,
                "cost_usd": self.cost_usd,
            }

        if _serialized_len(_assemble()) <= _VERDICT_BYTE_CAP:
            return _assemble()

        # 1) drop violations
        violations = []
        if _serialized_len(_assemble()) <= _VERDICT_BYTE_CAP:
            return _assemble()

        # 2) drop per-check detail
        for c in checks:
            c.pop("detail", None)
        if _serialized_len(_assemble()) <= _VERDICT_BYTE_CAP:
            return _assemble()

        # 3) strip every check down to name/passed
        checks = [{"name": c.get("name"), "passed": c.get("passed")} for c in checks]
        return _assemble()


def _serialized_len(payload: dict) -> int:
    return len(json.dumps(payload))


def run_qa_gate(
    artifact_bytes: bytes,
    qa_envelope: dict | None,
    workspace_dir: str,
    broker_env: dict,
    remaining_budget_seconds: float,
    gate_base_dir: str | None = None,
    run_id: str | None = None,
    experiment_id: str | None = None,
) -> tuple[Verdict, str | None] | None:
    """Run the QA gate over ``artifact_bytes`` and return ``(verdict, raw_output)``,
    or None.

    Returns None when ``qa_envelope`` is None (no qa folder — the caller is
    byte-identical to a pre-gate run). Otherwise always returns a
    ``(Verdict, raw_output)`` tuple and NEVER raises: any unexpected internal
    error is folded into a Verdict with ``status == "error"`` (fail-open,
    observe-only). ``raw_output`` is the raw ``main.py`` stdout (str), or None
    when no main.py ran (skipped / insufficient budget / internal error before
    the subprocess) — the runner forwards it to the broker for the durable S3
    ``verdict.json`` write.

    ``run_id``/``experiment_id`` are OPTIONAL log-correlation keys (cross-lane
    interface): Lane B's main.py passes ``config.run_id`` / ``config.experiment_id``.
    They default to None (byte-identical to omitting them) and only appear in
    gate log lines, never in the Verdict.
    """
    if qa_envelope is None:
        return None

    started = time.monotonic()
    base = gate_base_dir or _default_gate_root()
    gate_dir: str | None = None
    try:
        files = qa_envelope.get("files") or {}
        manifest = qa_envelope.get("manifest") or {}
        qa_version_ids = qa_envelope.get("resolved_qa_version_ids") or {}

        has_main = _MAIN_PY in files

        if not has_main:
            verdict = Verdict(
                status="skipped",
                qa_version_ids=qa_version_ids,
                duration_ms=_elapsed_ms(started),
            )
            _log_verdict(verdict, run_id)
            return verdict, None

        det_timeout = _resolve_budget(manifest, run_id)
        _warn_if_blocking(manifest)

        # Pre-flight budget: never spawn the subprocess if the remaining outer
        # budget can't cover the deterministic timeout (decision 11). The
        # evaluator term is gone — only deterministic.timeout_seconds counts.
        required = det_timeout
        if remaining_budget_seconds < required:
            verdict = Verdict(
                status="error",
                qa_version_ids=qa_version_ids,
                pass_=None,
                violations=[
                    f"insufficient_budget: {remaining_budget_seconds:.0f}s remaining < "
                    f"{required}s required for the deterministic stage"
                ],
                duration_ms=_elapsed_ms(started),
            )
            _log_verdict(verdict, run_id)
            return verdict, None

        # Materialize qa files into a private dir OUTSIDE workspace AND /tmp.
        os.makedirs(base, exist_ok=True)
        gate_dir = tempfile.mkdtemp(dir=base, prefix="qa-")
        _materialize(gate_dir, manifest, files)

        det_checks, det_error, raw_output = _run_deterministic(
            gate_dir=gate_dir,
            artifact_bytes=artifact_bytes,
            workspace_dir=workspace_dir,
            broker_env=broker_env,
            timeout_seconds=det_timeout,
            run_id=run_id,
        )
        checks = det_checks
        stage_error = det_error

        status: VerdictStatus = "error" if stage_error else "evaluated"
        # A1: an entrypoint that produced ZERO fragments verified nothing —
        # `all([])` is vacuously True, so an empty checks list is NOT a clean
        # pass. Map empty -> None (not True). Stage errors already force None.
        pass_: bool | None
        if stage_error or not checks:
            pass_ = None
        else:
            pass_ = all(c["passed"] for c in checks)
        violations = _build_violations(checks)

        verdict = Verdict(
            status=status,
            qa_version_ids=qa_version_ids,
            pass_=pass_,
            checks=checks,
            violations=violations,
            duration_ms=_elapsed_ms(started),
            cost_usd=0.0,
        )
        _log_verdict(verdict, run_id)
        return verdict, raw_output
    except Exception as e:  # fail-open — observe-only never raises to the caller
        logger.exception("qa_gate_internal_error errorType=%s: %s", type(e).__name__, e)
        verdict = Verdict(
            status="error",
            qa_version_ids=(qa_envelope.get("resolved_qa_version_ids") or {}) if isinstance(qa_envelope, dict) else {},
            pass_=None,
            violations=[f"qa_gate_internal_error: {type(e).__name__}: {e}"],
            duration_ms=_elapsed_ms(started),
        )
        _log_verdict(verdict, run_id)
        return verdict, None
    finally:
        if gate_dir is not None:
            shutil.rmtree(gate_dir, ignore_errors=True)


def _log_verdict(verdict: Verdict, run_id: str | None) -> None:
    """Emit the verdict summary at INFO so a deployed smoke can verify the gate
    ran from CloudWatch alone (no S3 / Braintrust round-trip needed)."""
    logger.info(
        "qa_gate_verdict status=%s pass=%s checks=%d run_id=%s",
        verdict.status,
        verdict.pass_,
        len(verdict.checks),
        run_id,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _resolve_budget(manifest: dict, run_id: str | None = None) -> int:
    deterministic = manifest.get("deterministic") or {}
    return _int_or_default(
        deterministic.get("timeout_seconds"), _DEFAULT_DETERMINISTIC_TIMEOUT, "deterministic.timeout_seconds", run_id
    )


def _int_or_default(value, default: int, field_name: str = "", run_id: str | None = None) -> int:
    """Coerce a positive-int budget value, falling back to ``default``.

    A4: when the value is PRESENT (not None) but invalid (bool, non-int, or
    <= 0), log a warning so author misconfiguration is discoverable. An ABSENT
    value (None) taking the default is normal and silent.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if value is not None:
            logger.warning(
                "qa_gate_invalid_budget_coerced_to_default field=%s value=%r default=%d run_id=%s",
                field_name,
                value,
                default,
                run_id,
            )
        return default
    return value


def _warn_if_blocking(manifest: dict) -> None:
    if manifest.get("blocking") is True:
        logger.warning(
            "qa_gate_blocking_observed: manifest declares blocking=true but v1 is "
            "observe-only — treating as observe until enforcement ships"
        )


def _materialize(gate_dir: str, manifest: dict, files: dict) -> None:
    """Write manifest.json + each qa entrypoint into the private gate dir."""
    with open(os.path.join(gate_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    for name, body in files.items():
        # Flat layout, basename-only (publisher/broker enforce this upstream;
        # guard here so a malformed envelope can't escape the gate dir).
        if name != os.path.basename(name) or name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"unsafe qa file basename: {name!r}")
        with open(os.path.join(gate_dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)


def _run_deterministic(
    *,
    gate_dir: str,
    artifact_bytes: bytes,
    workspace_dir: str,
    broker_env: dict,
    timeout_seconds: int,
    run_id: str | None = None,
) -> tuple[list[dict], bool, str | None]:
    """Run qa/main.py as `python3 main.py --artifact <p> --workspace <ws>`,
    cwd=gate_dir, with BROKER_URL/BROKER_TOKEN in env. Returns
    ``(checks, error, raw_output)``.

    - nonzero exit -> synthetic failing fragment 'main_py_exit' (NOT a stage
      error): pass is decided by fragments. The folded stderr tail is REDACTED
      (A6) so a leaked broker token never lands in the Verdict.
    - over-cap stdout / unparseable stdout / timeout -> stage error.
    - ``raw_output`` is the captured stdout (decoded) the runner forwards to the
      broker for the durable S3 verdict.json write; None when the subprocess
      never produced usable stdout (spawn/timeout/read failure).

    A2: stdout/stderr are read with bounded buffers via ``subprocess.Popen`` so
    a runaway main.py cannot buffer GBs into runner memory. We read at most
    ``_MAIN_STDOUT_CAP + 1`` bytes of stdout (the +1 lets us DETECT over-cap)
    and only a bounded stderr tail.
    """
    artifact_path = os.path.join(gate_dir, "_artifact_under_test")
    with open(artifact_path, "wb") as fh:
        fh.write(artifact_bytes)

    env = dict(os.environ)
    for k in ("BROKER_URL", "BROKER_TOKEN"):
        if k in broker_env and broker_env[k] is not None:
            env[k] = str(broker_env[k])

    try:
        proc = subprocess.Popen(
            [
                "python3",
                _MAIN_PY,
                "--artifact",
                artifact_path,
                "--workspace",
                workspace_dir,
            ],
            cwd=gate_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        logger.exception("qa_gate_main_py_spawn_failed errorType=%s run_id=%s: %s", type(e).__name__, run_id, e)
        return [], True, None

    try:
        stdout, stderr, over_cap = _read_bounded(proc, timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_quietly(proc)
        logger.warning("qa_gate_main_py_timeout timeout=%ss run_id=%s", timeout_seconds, run_id)
        return [], True, None
    except Exception as e:
        _kill_quietly(proc)
        logger.exception("qa_gate_main_py_read_failed errorType=%s run_id=%s: %s", type(e).__name__, run_id, e)
        return [], True, None

    returncode = proc.returncode

    if over_cap:
        logger.warning("qa_gate_main_py_stdout_over_cap cap=%d run_id=%s", _MAIN_STDOUT_CAP, run_id)
        return [], True, None

    # A11 (fail-open at source): the value the runner forwards to the broker for
    # the durable S3 main_output.json write must (a) never exceed the broker's
    # 1 MiB cap and (b) never carry the live BROKER_TOKEN. decode(errors='replace')
    # would expand each invalid byte to U+FFFD (3 bytes), so a within-cap stdout
    # could decode past the cap; redaction then runs on the decoded text. The
    # final encoded-byte cap is the hard guarantee.
    raw_output = _safe_raw_output(stdout, broker_env)

    checks: list[dict] = []
    if stdout.strip():
        try:
            raw = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            logger.warning("qa_gate_main_py_unparseable_stdout run_id=%s", run_id)
            return [], True, raw_output
        if not isinstance(raw, list):
            logger.warning("qa_gate_main_py_stdout_not_array type=%s run_id=%s", type(raw).__name__, run_id)
            return [], True, raw_output
        checks = [_normalize_fragment(frag, "deterministic", broker_env) for frag in raw]
    elif returncode == 0:
        # Exit 0 with empty stdout = no fragments emitted. Unparseable (empty
        # is not a JSON array) -> stage error.
        logger.warning("qa_gate_main_py_empty_stdout_exit0 run_id=%s", run_id)
        return [], True, raw_output

    if returncode != 0:
        stderr_tail = stderr[-_STDERR_TAIL:].decode("utf-8", errors="replace")
        stderr_tail = _redact_secrets(stderr_tail, broker_env)
        checks.append(
            {
                "name": "main_py_exit",
                "type": "deterministic",
                "passed": False,
                "detail": f"main.py exited {returncode}; stderr tail: {stderr_tail}",
            }
        )

    return checks, False, raw_output


def _read_bounded(proc: subprocess.Popen, timeout_seconds: int) -> tuple[bytes, bytes, bool]:
    """Read at most ``_MAIN_STDOUT_CAP + 1`` bytes of stdout and a bounded
    stderr tail from ``proc`` within ``timeout_seconds``, then wait for exit.

    Returns ``(stdout, stderr_tail, over_cap)``. ``over_cap`` is True when more
    than ``_MAIN_STDOUT_CAP`` bytes were produced (we read one extra byte to
    detect this, never the whole runaway stream). Raises
    ``subprocess.TimeoutExpired`` if the process outlives the timeout.

    ``communicate`` with a streaming kernel pipe still materializes the full
    output, so we instead read fixed-size slices directly off the pipes.

    We read both pipes concurrently via a selector (avoids the classic deadlock
    of draining one full pipe while the other fills its kernel buffer): stdout
    is bounded to ``_MAIN_STDOUT_CAP + 1`` retained bytes (extra bytes are read
    and discarded so the child never blocks on a full pipe), stderr is kept to a
    bounded ring of the most recent ``_STDERR_TAIL`` bytes.
    """
    sel = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)

    stdout_chunks: list[bytes] = []
    stdout_len = 0
    over_cap = False
    # Bounded ring of the most recent stderr bytes.
    stderr_tail: collections.deque[bytes] = collections.deque()
    stderr_tail_len = 0

    deadline = time.monotonic() + timeout_seconds
    open_streams = 2
    try:
        while open_streams > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
            events = sel.select(timeout=remaining)
            if not events:
                raise subprocess.TimeoutExpired(proc.args, timeout_seconds)
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                if key.fileobj is proc.stdout:
                    if not over_cap:
                        remaining_budget = (_MAIN_STDOUT_CAP + 1) - stdout_len
                        if remaining_budget > 0:
                            take = chunk[:remaining_budget]
                            stdout_chunks.append(take)
                            stdout_len += len(take)
                        if stdout_len > _MAIN_STDOUT_CAP:
                            over_cap = True
                    # Once over cap we keep draining (so the child doesn't block
                    # on a full pipe) but discard the bytes.
                else:
                    stderr_tail.append(chunk)
                    stderr_tail_len += len(chunk)
                    # Trim the ring to the last _STDERR_TAIL bytes.
                    while stderr_tail_len - len(stderr_tail[0]) >= _STDERR_TAIL and len(stderr_tail) > 1:
                        stderr_tail_len -= len(stderr_tail.popleft())
    finally:
        sel.close()

    # Wait for the child to fully exit so returncode is populated.
    proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    # Pipes hit EOF above; close the file objects so their fds aren't held
    # until GC (single-use task, but keep it tidy).
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    stdout = b"".join(stdout_chunks)[: _MAIN_STDOUT_CAP + 1]
    stderr = b"".join(stderr_tail)[-_STDERR_TAIL:]
    return stdout, stderr, over_cap


def _kill_quietly(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and reap it, closing its pipes so no fd leaks.

    Used on the timeout / read-error paths where ``_read_bounded`` raised
    before draining the pipes to EOF."""
    try:
        proc.kill()
    except Exception:
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _redact_secrets(text: str, broker_env: dict) -> str:
    """A6: mask the BROKER_TOKEN value (from ``broker_env``) and common
    secret-ish patterns in ``text`` before it lands in the Verdict.

    The explicit token is masked first (longest, most specific), then generic
    patterns catch token shapes we don't know the literal of.
    """
    token = broker_env.get("BROKER_TOKEN")
    if token and isinstance(token, str) and len(token) >= 4:
        text = text.replace(token, _REDACTED)

    def _mask_kv(m: re.Match) -> str:
        return f"{m.group(1)}={_REDACTED}"

    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(_mask_kv, text)
        else:
            text = pat.sub(_REDACTED, text)
    return text


def _safe_raw_output(stdout: bytes, broker_env: dict) -> str:
    """Build the raw_output the runner forwards to the broker for the durable
    S3 main_output.json write (contract D).

    A11 (fail-open at source): the broker 400s a publish whose raw output
    exceeds its 1 MiB cap, so the value the runner sends can NEVER exceed
    ``_MAIN_STDOUT_CAP`` encoded bytes. We decode with ``errors='ignore'`` (so
    invalid bytes are dropped, never expanded to 3-byte U+FFFD like
    ``errors='replace'`` would), redact the live BROKER_TOKEN + secret shapes
    (so the durable copy and anything downstream never carry it — A6), then cap
    the ENCODED form: if redaction pushed it back over the cap (``[REDACTED]``
    is longer than a short token), truncate the encoded bytes to the cap and
    decode-ignore so the final string always encodes to <= the cap."""
    text = stdout.decode("utf-8", errors="ignore")
    text = _redact_secrets(text, broker_env)
    if len(text.encode("utf-8")) > _MAIN_STDOUT_CAP:
        text = text.encode("utf-8")[:_MAIN_STDOUT_CAP].decode("utf-8", errors="ignore")
    return text


def _normalize_fragment(frag: object, stage_type: str, broker_env: dict | None = None) -> dict:
    """Coerce one raw fragment into a check dict. An invalid fragment (not an
    object, or missing a string ``name`` / bool ``passed``) is replaced by a
    synthetic FAILING fragment naming the defect (contract C).

    A6 (redact fragment detail): author-emitted string field values flow
    verbatim into ``Verdict.checks`` -> the durable verdict.json + the SQS
    callback, so a token printed into a fragment ``detail`` (or any string
    field) would leak. Every string value is run through ``_redact_secrets``
    using ``broker_env`` so the live BROKER_TOKEN never lands in the verdict."""
    if not isinstance(frag, dict) or not isinstance(frag.get("name"), str) or not isinstance(frag.get("passed"), bool):
        return {
            "name": "invalid_fragment",
            "type": stage_type,
            "passed": False,
            "detail": f"invalid fragment replaced: {json.dumps(frag, default=str)[:512]}",
        }
    check = dict(frag)
    check["type"] = stage_type
    if broker_env is not None:
        for k, v in check.items():
            if isinstance(v, str):
                check[k] = _redact_secrets(v, broker_env)
    return check


def _build_violations(checks: list[dict]) -> list[str]:
    """Human-readable strings for failed checks (contract C ``violations``)."""
    out: list[str] = []
    for c in checks:
        if c.get("passed") is False:
            name = c.get("name", "<unnamed>")
            detail = c.get("detail")
            out.append(f"{name}: {detail}" if detail else str(name))
    return out
