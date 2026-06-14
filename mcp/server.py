#!/usr/bin/env python3
"""
amori — общий MCP-сервер инфры Amori (stdio, FastMCP).

Даёт агентам (Claude Code / Codex / Hermes) инструменты управления AI-командой:
проекты/задачи, контент-завод, статус/отчёты, read-only SQL. Один сервер — три клиента.

Дизайн (по best practices MCP):
  • Транспорт stdio; в stdout пишет ТОЛЬКО протокол → мы НИЧЕГО не печатаем в stdout.
  • ЧТЕНИЕ — напрямую psycopg2 / HTTP (легко, без тяжёлых зависимостей).
  • ЗАПИСЬ — через subprocess к существующим CLI (project_manager.py / content_factory.py)
    под anaconda-python: переиспользуем готовую логику и НЕ тащим llm/litellm в MCP-процесс
    (иначе их print() сломал бы JSON-RPC, а venv раздулся бы).
  • Входы от LLM недоверенные → sql_read строго read-only (SELECT/WITH, whitelist БД, лимит, таймаут).
"""
import os
import re
import json
import datetime
import subprocess
import urllib.request
from decimal import Decimal

from mcp.server.fastmcp import FastMCP
import psycopg2
from dotenv import load_dotenv

AGENTS = os.path.expanduser("~/ai-infra/agents")
load_dotenv(os.path.join(AGENTS, ".env"))
PY = "/opt/anaconda3/bin/python3"  # anaconda-python со всеми зависимостями ai-infra
DASH = os.environ.get("INFRA_DASH_URL", "http://localhost:8099")
DBS = {"ops_db", "customer_db"}

PG = dict(
    host=os.getenv("OPS_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("OPS_DB_PORT", "5432")),
    user=os.getenv("OPS_DB_USER", "agent_user"),
    password=os.getenv("POSTGRES_PASSWORD"),
)

mcp = FastMCP("amori")


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def _rows(db: str, q: str, params=None) -> list:
    conn = psycopg2.connect(dbname=db, connect_timeout=5,
                            options="-c statement_timeout=8000", **PG)
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
    """Запустить CLI ai-infra (anaconda-python) и вернуть хвост вывода."""
    try:
        r = subprocess.run([PY, *args], cwd=AGENTS, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "⏳ операция выполняется дольше ожидаемого (фон продолжит); проверь статус позже."
    out = (r.stdout or "").strip()[-1500:]
    if r.returncode:
        out += "\n[stderr] " + (r.stderr or "").strip()[-600:]
    return out or "(нет вывода)"


# ─────────── Проекты и задачи AI-команды ───────────
@mcp.tool()
def new_project(goal: str) -> str:
    """Запустить проект AI-команды Amori: цель раскладывается LLM на 3-6 задач и
    раздаётся доменным воркерам (контент/ресёрч/dev/ops). ЗАПИСЬ — тратит LLM-вызовы.
    Возвращает id проекта и список задач."""
    return _run("project_manager.py", goal, timeout=260)


@mcp.tool()
def list_projects() -> list:
    """Список проектов AI-команды с прогрессом (всего/готово/активно)."""
    return _rows("ops_db",
        "SELECT p.id, p.name, p.domain, p.status, count(t.*) total, "
        "count(t.*) FILTER (WHERE t.status='done') done, "
        "count(t.*) FILTER (WHERE t.status IN ('queued','claimed','running')) active "
        "FROM projects p LEFT JOIN tasks t ON t.project_id=p.id "
        "GROUP BY p.id ORDER BY p.id DESC LIMIT 50")


@mcp.tool()
def project_status(project_id: int) -> list:
    """Задачи конкретного проекта со статусами (id, assignee, domain, status, title)."""
    return _rows("ops_db",
        "SELECT id, assignee, domain, status, left(title,90) title "
        "FROM tasks WHERE project_id=%s ORDER BY id", (int(project_id),))


@mcp.tool()
def list_tasks(status: str = "") -> list:
    """Задачи очереди. Опц. фильтр status: queued|claimed|running|done|failed."""
    if status:
        return _rows("ops_db",
            "SELECT id, project_id, assignee, status, left(title,90) title "
            "FROM tasks WHERE status=%s ORDER BY id DESC LIMIT 60", (status,))
    return _rows("ops_db",
        "SELECT id, project_id, assignee, status, left(title,90) title "
        "FROM tasks ORDER BY id DESC LIMIT 60")


# ─────────── Контент-завод (продажи) ───────────
@mcp.tool()
def create_content(brief: str, channel: str = "telegram", kind: str = "post") -> str:
    """Сгенерировать продающий контент и положить на аппрув. ЗАПИСЬ — тратит LLM.
    channel: telegram|vk|email|landing|ad. kind: post|email|ad_creative|landing.
    Дальше одобри его через approve_content(id)."""
    ch = channel if channel in {"telegram", "vk", "email", "landing", "ad"} else "telegram"
    kd = kind if kind in {"post", "email", "ad_creative", "landing"} else "post"
    return _run("content_factory.py", brief, ch, kd, timeout=260)


@mcp.tool()
def approve_content(content_id: int) -> str:
    """Одобрить и опубликовать единицу контента. ВНЕШНЕЕ ДЕЙСТВИЕ (публикация в канал)."""
    return _run("content_factory.py", "approve", str(int(content_id)))


@mcp.tool()
def reject_content(content_id: int) -> str:
    """Отклонить единицу контента (на доработку)."""
    return _run("content_factory.py", "reject", str(int(content_id)))


@mcp.tool()
def list_content() -> list:
    """Список контента контент-завода со статусами (pending/approved/published/rejected)."""
    return _rows("ops_db",
        "SELECT id, channel, kind, status, COALESCE(NULLIF(title,''), left(body,60)) title, "
        "to_char(created_at,'MM-DD HH24:MI') created FROM content_items ORDER BY id DESC LIMIT 30")


# ─────────── Статус / отчёты / read-only SQL ───────────
@mcp.tool()
def system_status() -> dict:
    """Сводка системы Amori: агенты up/total, контейнеры, расход LLM, активные задачи,
    контент на аппрув, heartbeats. Читает дашборд /api/state."""
    with urllib.request.urlopen(DASH + "/api/state", timeout=8) as r:
        d = json.load(r)
    tasks = d.get("tasks", [])
    content = d.get("content", [])
    return {
        "summary": d.get("summary"),
        "agents": len(d.get("agents", [])),
        "projects": len(d.get("projects", [])),
        "tasks_active": sum(1 for t in tasks if t.get("status") in ("queued", "claimed", "running")),
        "tasks_failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "content_pending": sum(1 for c in content if c.get("status") == "pending"),
        "heartbeats": d.get("heartbeats"),
    }


@mcp.tool()
def recent_reports(limit: int = 15) -> list:
    """Последние отчёты агентов AI-команды (agent, kind, title, время)."""
    n = max(1, min(int(limit), 50))
    return _rows("ops_db",
        "SELECT agent, kind, left(title,90) title, to_char(ts,'MM-DD HH24:MI') ts "
        "FROM reports ORDER BY ts DESC, id DESC LIMIT %s", (n,))


_SQL_OK = re.compile(r"^\s*(select|with)\b", re.I)


@mcp.tool()
def sql_read(db: str, query: str) -> list:
    """READ-ONLY SQL для аналитики. db: ops_db|customer_db. Только один SELECT/WITH-запрос
    (без ; DDL/DML), авто-LIMIT 200, таймаут 8с. Записать ничего нельзя."""
    if db not in DBS:
        return [{"error": f"db must be one of {sorted(DBS)}"}]
    q = query.strip().rstrip(";")
    if not _SQL_OK.match(q) or ";" in q:
        return [{"error": "only a single SELECT/WITH query is allowed (no ; / DDL / DML)"}]
    if not re.search(r"\blimit\b", q, re.I):
        q += " LIMIT 200"
    try:
        return _rows(db, q)
    except Exception as e:
        return [{"error": str(e)[:300]}]


if __name__ == "__main__":
    mcp.run()  # stdio transport
