import audit_agents


def test_log_audit_ignores_failures_before_latest_successful_start(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_agents, "__file__", str(tmp_path / "audit_agents.py"))
    (tmp_path / "service.log").write_text(
        "Traceback: startup failed\n"
        "2026-08-12 INFO service запущен\n"
        "processing normally\n",
        encoding="utf-8",
    )

    assert audit_agents._log_findings("service.log") == []


def test_log_audit_keeps_failures_after_latest_successful_start(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_agents, "__file__", str(tmp_path / "audit_agents.py"))
    (tmp_path / "service.log").write_text(
        "2026-08-12 INFO service запущен\n"
        "Traceback: handler failed\n",
        encoding="utf-8",
    )

    assert audit_agents._log_findings("service.log") == ["Traceback"]
