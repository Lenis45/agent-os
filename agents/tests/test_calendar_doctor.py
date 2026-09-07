import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "calendar_doctor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("calendar_doctor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_token_diagnostics_detect_scope_and_permissions(tmp_path: Path):
    module = _load_module()
    token = tmp_path / "token.json"
    token.write_text(
        json.dumps(
            {
                "refresh_token": "present",
                "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            }
        ),
        encoding="utf-8",
    )
    token.chmod(0o600)

    checks = module.check_token(token)

    assert all(check.level == "PASS" for check in checks)


def test_schedule_diagnostics_detect_duplicate_scheduler():
    module = _load_module()
    cron = "30 8 * * * /python /repo/agents/calendar_agent.py\n"

    result = module.check_schedule(crontab_text=cron, launchd_loaded=True)

    assert result.level == "FAIL"
    assert "duplicate" in result.detail


def test_schedule_diagnostics_accept_launchd_only():
    module = _load_module()

    result = module.check_schedule(crontab_text="", launchd_loaded=True)

    assert result.level == "PASS"


def test_old_invalid_grant_is_not_reported_against_new_token(tmp_path: Path):
    module = _load_module()
    log = tmp_path / "calendar.log"
    token = tmp_path / "token.json"
    log.write_text("2026-09-05 08:00:03 ERROR invalid_grant: expired\n", encoding="utf-8")
    token.write_text("{}", encoding="utf-8")
    timestamp = datetime(2026, 9, 5, 9, 0).timestamp()
    os.utime(token, (timestamp, timestamp))

    result = module.check_recent_invalid_grant(log, token)

    assert result.level == "PASS"
    assert "after current token: 0" in result.detail
