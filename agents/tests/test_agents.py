"""Smoke-импорт всех агентов + регресс-стражи против уже исправленных проблем."""
import pathlib
import importlib
import pytest

AGENTS = [
    "email_watchdog", "chief_of_staff", "lead_manager", "email_agent",
    "knowledge_curator", "calendar_agent", "task_sync", "support_agent", "orchestrator",
]
AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _src(name: str) -> str:
    return (AGENTS_DIR / f"{name}.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", AGENTS)
def test_agent_imports(name):
    """Импорт без ошибок — ловит NameError/битые импорты (как баг is_known в curator)."""
    importlib.import_module(name)


@pytest.mark.parametrize("name", AGENTS)
def test_no_local_send_telegram(name):
    """Никаких локальных send_telegram — только общий notify.send."""
    assert "def send_telegram" not in _src(name), f"{name}: остался локальный send_telegram"


@pytest.mark.parametrize("name", AGENTS)
def test_no_hardcoded_pg_password(name):
    """Пароль PG не должен быть захардкожен в исходнике."""
    assert "Sbyjc8wreznzGWBertLmYe8U3fYRD245" not in _src(name), f"{name}: хардкод PG-пароля"


@pytest.mark.parametrize("name", AGENTS)
def test_no_langfuse_init(name):
    """Langfuse happy-path удалён — учёт идёт через cost_guard."""
    assert "Langfuse(" not in _src(name), f"{name}: остался Langfuse()"


@pytest.mark.parametrize("name", AGENTS)
def test_uses_shared_libs(name):
    """Каждый агент опирается на общий llm; для Telegram — notify.send
    или собственный send_msg (orchestrator шлёт на динамический chat_id)."""
    src = _src(name)
    assert "import llm" in src, f"{name}: не использует llm"
    assert ("import notify" in src) or ("def send_msg" in src), \
        f"{name}: нет ни notify, ни send_msg"


def test_customer_agents_use_customer_db():
    """Клиентские агенты ходят в customer_db (152-ФЗ разделение контуров)."""
    for name in ("lead_manager", "email_agent", "support_agent"):
        assert 'db.connect("customer_db")' in _src(name), f"{name}: не на customer_db"


def test_no_dead_if_false():
    """Регресс-страж: убранный мёртвый код в chief_of_staff не вернулся."""
    assert "if False" not in _src("chief_of_staff")
