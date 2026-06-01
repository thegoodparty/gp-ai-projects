from __future__ import annotations

import pathlib

from pmf_engine.runner.pmf_runtime import egress_guard

_SITECUSTOMIZE = pathlib.Path(__file__).resolve().parents[1] / "sitecustomize.py"


def _exec_sitecustomize() -> None:
    source = _SITECUSTOMIZE.read_text()
    code = compile(source, str(_SITECUSTOMIZE), "exec")
    exec(code, {"__name__": "sitecustomize", "__file__": str(_SITECUSTOMIZE)})


def test_install_failure_warns_and_does_not_propagate(monkeypatch, capsys):
    monkeypatch.setenv("BROKER_URL", "http://broker.local")

    def boom(*args, **kwargs):
        raise RuntimeError("socket API changed")

    monkeypatch.setattr(egress_guard, "install", boom)

    _exec_sitecustomize()

    err = capsys.readouterr().err
    assert "SANDBOX EGRESS GUARD FAILED TO INSTALL" in err
    assert "WITHOUT socket-level egress protection" in err
    assert "RuntimeError" in err
    assert "socket API changed" in err


def test_install_success_calls_once_no_warning(monkeypatch, capsys):
    monkeypatch.setenv("BROKER_URL", "http://broker.local")

    calls: list[tuple] = []

    def fake_install(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(egress_guard, "install", fake_install)

    _exec_sitecustomize()

    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "EGRESS GUARD" not in err


def test_no_broker_url_does_not_install_or_warn(monkeypatch, capsys):
    monkeypatch.delenv("BROKER_URL", raising=False)

    calls: list[tuple] = []

    def fake_install(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(egress_guard, "install", fake_install)

    _exec_sitecustomize()

    assert calls == []
    err = capsys.readouterr().err
    assert "EGRESS GUARD" not in err
