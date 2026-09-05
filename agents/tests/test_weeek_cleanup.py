import importlib.util
import json
import stat
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_weeek_tasks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("cleanup_weeek_tasks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_is_private_and_contains_no_token(tmp_path: Path):
    module = _load_module()
    inventory = module.Inventory(
        project_id=1,
        project_title="Amori",
        current=[{"id": 1, "title": "Task", "isCompleted": False}],
        deleted=[],
        crm_contacts=21,
        crm_organizations=4,
    )

    path = module.save_manifest(inventory, tmp_path / "private")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert payload["counts"]["current"] == 1
    assert "token" not in path.read_text(encoding="utf-8").lower()


def test_cleanup_completes_open_tasks_then_moves_all_to_trash():
    module = _load_module()
    before = module.Inventory(
        project_id=1,
        project_title="Amori",
        current=[
            {"id": 1, "isCompleted": False},
            {"id": 2, "isCompleted": True},
            {"id": 3, "isCompleted": False},
        ],
        deleted=[{"id": 99, "isDeleted": True}],
        crm_contacts=21,
        crm_organizations=4,
    )

    class Client:
        def __init__(self):
            self.completed = []
            self.deleted = []

        def complete_task(self, task_id):
            self.completed.append(task_id)

        def delete_task(self, task_id):
            self.deleted.append(task_id)

        def inventory(self, title):
            return module.Inventory(1, title, [], before.deleted + before.current, 21, 4)

    client = Client()
    after = module.execute_cleanup(client, before)

    assert client.completed == [1, 3]
    assert client.deleted == [1, 2, 3]
    assert after.current == []
