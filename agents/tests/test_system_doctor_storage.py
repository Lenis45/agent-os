import json
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


DOCTOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "system_doctor.py"
SPEC = importlib.util.spec_from_file_location("system_doctor_under_test", DOCTOR_PATH)
doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
SPEC.loader.exec_module(doctor)


def write_status(path, *, action="none", age_hours=0, **extra):
    payload = {
        "ts": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "action": action,
        "free_gb_after": 18,
        **extra,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_storage_maintenance_reports_staged_update(monkeypatch, tmp_path):
    status = tmp_path / "storage.json"
    write_status(status, action="reboot_required", macos_update_gb=20, target_build="25G83")
    monkeypatch.setattr(doctor, "STORAGE_STATUS", status)

    result = doctor.storage_maintenance_check()

    assert result.level == "WARN"
    assert "reboot required" in result.detail
    assert "25G83" in result.detail


def test_storage_maintenance_reports_fresh_healthy_run(monkeypatch, tmp_path):
    status = tmp_path / "storage.json"
    write_status(status)
    monkeypatch.setattr(doctor, "STORAGE_STATUS", status)

    result = doctor.storage_maintenance_check()

    assert result.level == "PASS"
    assert "free=18 GiB" in result.detail


def test_storage_maintenance_warns_when_stale(monkeypatch, tmp_path):
    status = tmp_path / "storage.json"
    write_status(status, age_hours=31)
    monkeypatch.setattr(doctor, "STORAGE_STATUS", status)

    result = doctor.storage_maintenance_check()

    assert result.level == "WARN"
    assert "last run" in result.detail
