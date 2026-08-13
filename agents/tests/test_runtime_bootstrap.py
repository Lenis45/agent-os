import sys

import runtime_bootstrap


def test_runtime_bootstrap_does_not_reexec_inside_project_venv(monkeypatch):
    called = []
    monkeypatch.setattr(sys, "prefix", str(runtime_bootstrap.RUNTIME))
    monkeypatch.setattr(runtime_bootstrap.os, "execv", lambda *args: called.append(args))

    runtime_bootstrap.ensure_isolated_runtime()

    assert called == []


def test_runtime_bootstrap_reexecs_legacy_python(monkeypatch, tmp_path):
    called = []
    python = tmp_path / "python"
    python.write_text("")
    monkeypatch.setattr(sys, "prefix", "/opt/anaconda3")
    monkeypatch.setattr(runtime_bootstrap, "PYTHON", python)
    monkeypatch.setattr(runtime_bootstrap.os, "execv", lambda *args: called.append(args))

    runtime_bootstrap.ensure_isolated_runtime()

    assert called
    executable, argv = called[0]
    assert executable == str(runtime_bootstrap.PYTHON)
    assert argv[0] == str(runtime_bootstrap.PYTHON)
