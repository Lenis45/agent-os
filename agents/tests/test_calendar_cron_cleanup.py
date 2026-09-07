import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "remove_legacy_calendar_cron.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("remove_legacy_calendar_cron", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_only_calendar_entries_are_removed():
    module = _load_module()
    before = (
        "0 10 * * * /python task_sync.py\n"
        "30 8 * * * /python /repo/agents/calendar_agent.py >> calendar.log 2>&1\n"
        "0 11 * * * /python lead_manager.py report\n"
    )

    after, removed = module.without_calendar_entries(before)

    assert len(removed) == 1
    assert "calendar_agent.py" not in after
    assert "task_sync.py" in after
    assert "lead_manager.py" in after
