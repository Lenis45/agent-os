from datetime import datetime, timedelta, timezone

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


def test_fresh_ok_heartbeat_marks_recovery():
    now = datetime.now(timezone.utc)

    assert audit_agents._fresh_ok_heartbeat(
        {"status": "ok", "last_seen": now - timedelta(minutes=5)}, now=now
    )
    assert not audit_agents._fresh_ok_heartbeat(
        {"status": "warn", "last_seen": now - timedelta(minutes=5)}, now=now
    )
    assert not audit_agents._fresh_ok_heartbeat(
        {"status": "ok", "last_seen": now - timedelta(hours=25)}, now=now
    )
