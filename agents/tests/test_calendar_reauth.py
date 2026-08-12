import importlib.util
import stat
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reauthorize_calendar.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reauthorize_calendar", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_token_install_is_atomic_and_keeps_private_backup(tmp_path: Path):
    module = _load_module()
    token = tmp_path / "token.json"
    token.write_text("old", encoding="utf-8")

    module._install_token("new", token)

    assert token.read_text(encoding="utf-8") == "new"
    assert token.with_suffix(".json.bak").read_text(encoding="utf-8") == "old"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.with_suffix(".json.bak").stat().st_mode) == 0o600
