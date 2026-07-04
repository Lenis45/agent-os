"""Стражи деплоя ponytail: правила есть, apply() их вшивает, dev_worker подключён,
корневой AGENTS.md синхронен, install-скрипт валиден."""
import pathlib
import subprocess
import shutil
import pytest

import ponytail

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = ROOT / "agents"


def test_rules_nonempty():
    assert "YAGNI" in ponytail.RULES and len(ponytail.RULES) > 200


def test_apply_injects():
    goal = "Ты решаешь код-задачи"
    out = ponytail.apply(goal)
    assert goal in out and "PONYTAIL" in out and "YAGNI" in out


def test_apply_off_is_noop():
    goal = "Ты решаешь код-задачи"
    assert ponytail.apply(goal, level="off") == goal


def test_dev_worker_uses_ponytail():
    """Регресс-страж: код-воркер не должен потерять инъекцию ponytail."""
    src = (AGENTS_DIR / "worker_handlers.py").read_text(encoding="utf-8")
    assert "import ponytail" in src
    assert "ponytail.apply(" in src


def test_agents_md_in_sync():
    """Корневой AGENTS.md (для кодинг-CLI) несёт ту же лестницу, что и Python-копия."""
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "YAGNI" in agents_md
    assert "the minimum code that works" in agents_md


def test_install_script_valid():
    """install_ponytail.sh должен быть синтаксически валидным bash."""
    script = ROOT / "scripts" / "install_ponytail.sh"
    assert script.exists()
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("нет bash")
    r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
