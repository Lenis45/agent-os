import json
import importlib.util
import sys
import urllib.error
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


def test_hermes_provider_check_accepts_aligned_local_profile(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n"
        "  provider: custom:ollama-local\n"
        "  base_url: http://127.0.0.1:11434/v1\n"
        "  default: amori-router:1.7b\n"
        "providers:\n"
        "  ollama-local:\n"
        "    api: http://127.0.0.1:11434/v1\n",
        encoding="utf-8",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"data":[{"id":"amori-router:1.7b"}]}'

    monkeypatch.setattr(doctor, "HERMES_CONFIG", config)
    monkeypatch.setattr(doctor.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    result = doctor.hermes_provider_check()

    assert result.level == "PASS"
    assert "amori-router:1.7b" in result.detail


def test_hermes_provider_check_rejects_url_mismatch(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n"
        "  provider: custom:qwen-free\n"
        "  base_url: http://127.0.0.1:11434/v1\n"
        "  default: amori-hermes:4b\n"
        "providers:\n"
        "  qwen-free:\n"
        "    api: http://127.0.0.1:3264/api\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "HERMES_CONFIG", config)

    result = doctor.hermes_provider_check()

    assert result.level == "FAIL"
    assert "do not match" in result.detail


def test_hermes_routing_parser_ignores_lists():
    model, providers = doctor._hermes_routing_config(
        "model:\n"
        "  provider: custom:ollama-local\n"
        "  base_url: 'http://127.0.0.1:11434/v1'\n"
        "  default: amori-router:1.7b\n"
        "providers:\n"
        "  ollama-local:\n"
        "    api: http://127.0.0.1:11434/v1\n"
        "    models:\n"
        "      - amori-router:1.7b\n"
    )

    assert model["provider"] == "custom:ollama-local"
    assert model["base_url"] == "http://127.0.0.1:11434/v1"
    assert providers["ollama-local"]["api"] == "http://127.0.0.1:11434/v1"


def test_image_bridge_check_reports_auth_as_warning(monkeypatch):
    body = b'{"status":"AUTH_REQUIRED","authenticated":false}'
    error = urllib.error.HTTPError(
        "http://127.0.0.1:3264/api/status", 503, "Service Unavailable", {}, None
    )
    error.read = lambda: body
    monkeypatch.setattr(doctor.urllib.request.OpenerDirector, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = doctor.image_bridge_check()

    assert result.level == "WARN"
    assert "OAuth" in result.detail
