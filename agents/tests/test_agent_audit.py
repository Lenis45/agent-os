from datetime import datetime

import audit_agents


def test_log_audit_ignores_recovered_old_traceback(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "2026-08-28 10:00:00 ERROR [agent] request failed\n"
        "Traceback (most recent call last):\n"
        "Connection refused\n"
        "2026-08-30 10:00:00 INFO [agent] запущен\n",
        encoding="utf-8",
    )

    assert audit_agents._log_findings(
        str(log), now=datetime(2026, 8, 30, 12, 0, 0)
    ) == []


def test_log_audit_reports_recent_traceback(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "2026-08-30 11:30:00 ERROR [agent] request failed\n"
        "Traceback (most recent call last):\n"
        "Connection refused\n",
        encoding="utf-8",
    )

    findings = audit_agents._log_findings(
        str(log), now=datetime(2026, 8, 30, 12, 0, 0)
    )

    assert "Traceback" in findings
    assert "Connection refused" in findings
