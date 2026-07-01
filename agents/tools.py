"""
tools — реестр инструментов AI-команды для praisonaiagents tool calling.

Те же 11 операций что в mcp/server.py, но как Python-функции с type hints
и docstring-ами — praisonaiagents автоматически превращает их в JSON-схему
и передаёт модели (Ollama / Groq / Qwen) как function_call.

Использование в агенте:
    from tools import AMORI_TOOLS
    agent = build_agent("analyst_agent", tools=AMORI_TOOLS, ...)

Или выборочно:
    from tools import sql_read, list_tasks
    agent = build_agent("research_agent", tools=[sql_read, list_tasks], ...)
"""
import os
import re
import json
import datetime
import subprocess
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import psycopg2

_PY = "/opt/anaconda3/bin/python3"
_AGENTS_DIR = os.path.dirname(os.path.abspath(__file__))

_PG = dict(
    host=os.getenv("OPS_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("OPS_DB_PORT", "5432")),
    user=os.getenv("OPS_DB_USER", "agent_user"),
    password=os.getenv("POSTGRES_PASSWORD"),
)
_DBS = {"ops_db", "customer_db"}


# ──────────────────────────────────────────────────────────────────
# Внутренние хелперы
# ──────────────────────────────────────────────────────────────────

def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def _rows(db: str, q: str, params=None) -> list:
    conn = psycopg2.connect(dbname=db, connect_timeout=5,
                            options="-c statement_timeout=8000", **_PG)
    try:
        cur = conn.cursor()
        cur.execute(q, params)
        cols = [d[0] for d in (cur.description or [])]
        rows = [{c: _jsonable(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
        conn.rollback()
        return rows
    finally:
        conn.close()


def _run(*args, timeout=200) -> str:
    try:
        r = subprocess.run([_PY, *args], cwd=_AGENTS_DIR, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "⏳ операция выполняется дольше ожидаемого (фон продолжит); проверь статус позже."
    out = (r.stdout or "").strip()[-1500:]
    if r.returncode:
        out += "\n[stderr] " + (r.stderr or "").strip()[-600:]
    return out or "(нет вывода)"


# ──────────────────────────────────────────────────────────────────
# Инструменты: проекты и задачи
# ──────────────────────────────────────────────────────────────────

def new_project(goal: str) -> str:
    """Запустить новый проект AI-команды Amori. Цель раскладывается на 3-6 задач
    и раздаётся воркерам (контент/ресёрч/dev/ops). Возвращает id проекта и список задач."""
    return _run("project_manager.py", goal, timeout=260)


def list_projects() -> list:
    """Получить список всех проектов AI-команды с прогрессом (всего задач / готово / активно)."""
    return _rows("ops_db",
        "SELECT p.id, p.name, p.domain, p.status, "
        "count(t.*) total, "
        "count(t.*) FILTER (WHERE t.status='done') done, "
        "count(t.*) FILTER (WHERE t.status IN ('queued','claimed','running')) active "
        "FROM projects p LEFT JOIN tasks t ON t.project_id=p.id "
        "GROUP BY p.id ORDER BY p.id DESC LIMIT 50")


def project_status(project_id: int) -> list:
    """Получить статус задач конкретного проекта по его ID (id, assignee, domain, status, title)."""
    return _rows("ops_db",
        "SELECT id, assignee, domain, status, left(title,90) title "
        "FROM tasks WHERE project_id=%s ORDER BY id", (int(project_id),))


def list_tasks(status: str = "") -> list:
    """Получить список задач очереди. Опциональный фильтр status: queued|claimed|running|done|failed."""
    if status:
        return _rows("ops_db",
            "SELECT id, project_id, assignee, status, left(title,90) title "
            "FROM tasks WHERE status=%s ORDER BY id DESC LIMIT 60", (status,))
    return _rows("ops_db",
        "SELECT id, project_id, assignee, status, left(title,90) title "
        "FROM tasks ORDER BY id DESC LIMIT 60")


# ──────────────────────────────────────────────────────────────────
# Инструменты: контент-завод
# ──────────────────────────────────────────────────────────────────

def create_content(brief: str, channel: str = "telegram", kind: str = "post") -> str:
    """Сгенерировать продающий контент и поставить на аппрув.
    channel: telegram|vk|email|landing|ad. kind: post|email|ad_creative|landing.
    После генерации нужно одобрить через approve_content(id)."""
    ch = channel if channel in {"telegram", "vk", "email", "landing", "ad"} else "telegram"
    kd = kind if kind in {"post", "email", "ad_creative", "landing"} else "post"
    return _run("content_factory.py", brief, ch, kd, timeout=260)


def approve_content(content_id: int) -> str:
    """Одобрить единицу контента и опубликовать её в канал."""
    return _run("content_factory.py", "approve", str(int(content_id)))


def reject_content(content_id: int) -> str:
    """Отклонить единицу контента для доработки."""
    return _run("content_factory.py", "reject", str(int(content_id)))


def list_content() -> list:
    """Получить список контента контент-завода со статусами: pending/approved/published/rejected."""
    return _rows("ops_db",
        "SELECT id, channel, kind, status, "
        "COALESCE(NULLIF(title,''), left(body,60)) title, "
        "to_char(created_at,'MM-DD HH24:MI') created "
        "FROM content_items ORDER BY id DESC LIMIT 30")


# ──────────────────────────────────────────────────────────────────
# Инструменты: аналитика и статус системы
# ──────────────────────────────────────────────────────────────────

def system_status() -> dict:
    """Получить сводку системы Amori: агенты up/total, активные задачи,
    контент на аппрув, LLM расходы, heartbeats."""
    import urllib.request
    dash = os.environ.get("INFRA_DASH_URL", "http://localhost:8099")
    try:
        with urllib.request.urlopen(dash + "/api/state", timeout=8) as r:
            d = json.load(r)
    except Exception as e:
        return {"error": f"dashboard недоступен: {e}"}
    tasks = d.get("tasks", [])
    content = d.get("content", [])
    return {
        "summary": d.get("summary"),
        "agents_up": len([a for a in d.get("agents", []) if a.get("status") == "running"]),
        "agents_total": len(d.get("agents", [])),
        "tasks_active": sum(1 for t in tasks if t.get("status") in ("queued", "claimed", "running")),
        "tasks_failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "content_pending": sum(1 for c in content if c.get("status") == "pending"),
        "heartbeats": d.get("heartbeats"),
    }


def recent_reports(limit: int = 15) -> list:
    """Получить последние отчёты агентов AI-команды (agent, kind, title, время)."""
    n = max(1, min(int(limit), 50))
    return _rows("ops_db",
        "SELECT agent, kind, left(title,90) title, to_char(ts,'MM-DD HH24:MI') ts "
        "FROM reports ORDER BY ts DESC, id DESC LIMIT %s", (n,))


_SQL_OK = re.compile(r"^\s*(select|with)\b", re.I)


def sql_read(db: str, query: str) -> list:
    """Выполнить READ-ONLY SQL-запрос для аналитики.
    db: ops_db или customer_db. Только SELECT/WITH (без DDL/DML).
    Автоматически добавляется LIMIT 200 и таймаут 8с."""
    if db not in _DBS:
        return [{"error": f"db должна быть одной из {sorted(_DBS)}"}]
    q = query.strip().rstrip(";")
    if not _SQL_OK.match(q) or ";" in q:
        return [{"error": "разрешён только один SELECT/WITH запрос (без ; / DDL / DML)"}]
    if not re.search(r"\blimit\b", q, re.I):
        q += " LIMIT 200"
    try:
        return _rows(db, q)
    except Exception as e:
        return [{"error": str(e)[:300]}]


# ──────────────────────────────────────────────────────────────────
# Реестры для удобного импорта
# ──────────────────────────────────────────────────────────────────

# Все инструменты — передавать в build_agent(..., tools=AMORI_TOOLS)
AMORI_TOOLS = [
    new_project, list_projects, project_status, list_tasks,
    create_content, approve_content, reject_content, list_content,
    system_status, recent_reports, sql_read,
]

# Только чтение — безопасны для аналитических агентов
AMORI_READ_TOOLS = [
    list_projects, project_status, list_tasks,
    list_content, system_status, recent_reports, sql_read,
]

# Инструменты для code_agent и analyst_agent
AMORI_DEV_TOOLS = [
    sql_read, list_tasks, project_status, recent_reports, system_status,
]
