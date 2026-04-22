"""
TDD for the agent-facing `verify_quote` CLI.

Contract: the atom agent fetches a page, writes the body to disk, then invokes
this script with the body path + the quote it wants to check. No LLM involved
in verification — the atom should not be generating Python for this.

Usage:
    python -m pmf_engine.research.verify_quote <body_path> --quote "<literal text>"
    python -m pmf_engine.research.verify_quote <body_path> --quote-file <quote_path>
    echo "<body>" | python -m pmf_engine.research.verify_quote - --quote "..."

Output: single JSON object on stdout, {"match": bool, "similarity": float, "closest_match": str | null}
Exit code: 0 = match, 1 = no match, 2 = usage/IO error
"""

from __future__ import annotations
import json
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest

from pmf_engine.research.verify_quote import main


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def _run_main(argv: list[str], capsys, stdin: str | None = None) -> tuple[int, dict]:
    """Invoke main(argv) and parse the stdout JSON. Returns (exit_code, parsed_output)."""
    if stdin is not None:
        sys.stdin = StringIO(stdin)
    try:
        rc = main(argv)
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    finally:
        if stdin is not None:
            sys.stdin = sys.__stdin__
    captured = capsys.readouterr()
    try:
        parsed = json.loads(captured.out) if captured.out.strip() else {}
    except json.JSONDecodeError:
        parsed = {"_raw": captured.out, "_stderr": captured.err}
    return rc, parsed


def test_exits_zero_and_prints_match_true_when_quote_in_body(tmp_path, capsys):
    body = _write(tmp_path, "body.html", "Durham FY2026 property tax rate is $0.5551 per $100.")
    rc, out = _run_main([str(body), "--quote", "property tax rate is $0.5551"], capsys)
    assert rc == 0
    assert out["match"] is True
    assert out["similarity"] == 1.0


def test_exits_one_and_prints_closest_when_quote_not_in_body(tmp_path, capsys):
    body = _write(tmp_path, "body.html", "Durham FY2026 property tax rate is $0.5551 per $100.")
    rc, out = _run_main([str(body), "--quote", "Durham mayor announced new stadium"], capsys)
    assert rc == 1
    assert out["match"] is False
    assert out["closest_match"] is not None
    assert 0.0 <= out["similarity"] < 1.0


def test_quote_file_flag_reads_quote_from_file(tmp_path, capsys):
    body = _write(tmp_path, "body.html", "The city of Durham adopted its FY26 budget.")
    qfile = _write(tmp_path, "q.txt", "adopted its FY26 budget")
    rc, out = _run_main([str(body), "--quote-file", str(qfile)], capsys)
    assert rc == 0
    assert out["match"] is True


def test_stdin_body_when_path_is_dash(tmp_path, capsys):
    rc, out = _run_main(
        ["-", "--quote", "tax rate is $0.5551"],
        capsys,
        stdin="Durham FY2026 property tax rate is $0.5551 per $100.",
    )
    assert rc == 0
    assert out["match"] is True


def test_missing_body_file_exits_two(tmp_path, capsys):
    rc, _ = _run_main([str(tmp_path / "nope.html"), "--quote", "x"], capsys)
    assert rc == 2


def test_no_quote_provided_exits_two(tmp_path, capsys):
    body = _write(tmp_path, "body.html", "hello")
    rc, _ = _run_main([str(body)], capsys)
    assert rc == 2


def test_aggressive_normalization_default(tmp_path, capsys):
    """Unicode dashes and smart quotes in body should match ASCII quote."""
    body = _write(tmp_path, "body.html", "Resolution \u201cSupport Transit\u201d \u2014 adopted 7\u20130.")
    rc, out = _run_main([str(body), "--quote", 'Resolution "Support Transit" - adopted 7-0'], capsys)
    assert rc == 0
    assert out["match"] is True


def test_outputs_valid_json_on_both_success_and_failure(tmp_path, capsys):
    """The CLI output contract: stdout is always a single JSON object, whether
    the quote matched or not. Orchestrators parse it the same way."""
    body = _write(tmp_path, "body.html", "hello world")
    for quote, expected_match in [("hello", True), ("xyz", False)]:
        rc, out = _run_main([str(body), "--quote", quote], capsys)
        assert "match" in out
        assert "similarity" in out
        assert "closest_match" in out
