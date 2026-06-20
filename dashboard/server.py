#!/usr/bin/env python3
"""
Amori Infra Ops — единая живая панель личной AI-инфры (read-only).

Показывает: статусы всех агентов (launchd/cron + heartbeat), LLM-расходы из
ops_db.llm_usage, Tier-1 сессии, прогоны backup/monitor/restore_test, контейнеры,
БД (agents/ops_db/customer_db/n8n), Qdrant. Ничего не запускает — только читает.

Запуск: /opt/anaconda3/bin/python3 ~/ai-infra/dashboard/server.py  → http://localhost:8099
"""
import json, subprocess, urllib.request, os, time, threading, concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datetime
from decimal import Decimal
import psycopg2
from psycopg2 import pool as pgpool


def _jsonable(v):
    """psycopg2 отдаёт нативные типы (Decimal/date) — приводим к JSON-сериализуемым."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/ai-infra/agents/.env"))
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("INFRA_DASH_PORT", "8099"))
PG = "ai_postgres"
DOCKER = os.environ.get("DOCKER_BIN", "/usr/local/bin/docker")

# Агенты: (key, имя, расписание, тип запуска, контур, launchd-label|None)
AGENTS = [
    ("orchestrator",     "Orchestrator (Emilia)", "24/7",        "longrun", "personal", "ai.orchestrator"),
    ("support_agent",    "Support Bot",           "24/7",        "longrun", "customer", "amori.support"),
    ("knowledge_curator","Knowledge Curator",     "24/7",        "longrun", "personal", "knowledge.curator"),
    ("chief_of_staff",   "Chief of Staff",        "9:00, 19:00", "sched",   "personal", "chief.of.staff"),
    ("email_watchdog",   "Email Watchdog",        "8:00",        "sched",   "personal", "email.watchdog"),
    ("task_sync",        "Task Sync",             "10:00 (cron)","sched",   "personal", None),
    ("calendar_agent",   "Calendar Agent",        "8:30 (cron)", "sched",   "personal", None),
    ("lead_manager",     "Lead Manager",          "10/11 (cron)","sched",   "customer", None),
    ("email_agent",      "Email Agent",           "on-demand",   "ondemand","customer", None),
]
CONTAINERS = ["ai_postgres", "ai_qdrant", "ai_redis", "ai_langfuse", "ai_n8n"]


def sh(args, timeout=8):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


# ── Постоянные psycopg2-соединения вместо `docker exec psql` на каждый запрос ──
# Раньше build_state() запускал ~15 subprocess'ов docker exec за вызов /api/state,
# что под нагрузкой давало контеншн и тихие пустые ответы (registry:0/usage:0).
# Теперь — пул соединений на БД к проброшенному на хост Postgres (как в ops_store).
PG_HOST = os.environ.get("OPS_DB_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("OPS_DB_PORT", "5432"))
PG_USER = os.environ.get("OPS_DB_USER", "agent_user")
PG_PASS = os.environ.get("POSTGRES_PASSWORD")  # из agents/.env (load_dotenv выше); без хардкода
if not PG_PASS:
    raise RuntimeError("POSTGRES_PASSWORD не задан — проверь ~/ai-infra/agents/.env")

_pools = {}
_pools_lock = threading.Lock()


def _pool(dbname):
    p = _pools.get(dbname)
    if p is not None:
        return p
    with _pools_lock:
        p = _pools.get(dbname)
        if p is None:
            try:
                p = pgpool.ThreadedConnectionPool(
                    1, 10, host=PG_HOST, port=PG_PORT, dbname=dbname,
                    user=PG_USER, password=PG_PASS, connect_timeout=5,
                    options="-c statement_timeout=8000",  # запрос не висит дольше 8с
                )
                _pools[dbname] = p
            except Exception as e:
                print(f"[db {dbname}] pool init failed: {e}")
                return None
        return _pools.get(dbname)


def _query(dbname, query, params=None, fetch="all"):
    """fetch: 'scalar' → first row, 'all' → list[dict], 'exec' → bool. Логирует ошибки."""
    pool = _pool(dbname)
    if pool is None:
        return False if fetch == "exec" else None
    conn = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute(query, params)
        if fetch == "exec":
            conn.commit()
            return True
        if fetch == "scalar":
            row = cur.fetchone()
            conn.rollback()
            return row
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
        conn.rollback()
        return [{c: _jsonable(v) for c, v in zip(cols, r)} for r in rows]
    except Exception as e:
        print(f"[db {dbname}] query error: {str(e)[:200]}")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False if fetch == "exec" else None
    finally:
        if conn is not None:
            try:
                pool.putconn(conn)
            except Exception:
                pass


def psql(dbname, query, timeout=8):
    """Скаляр (первая колонка первой строки) как строка, или None."""
    row = _query(dbname, query, fetch="scalar")
    return str(row[0]) if row and row[0] is not None else None


def psql_json(dbname, query):
    """Список dict'ов из SELECT (заменяет старый json_agg-трюк)."""
    r = _query(dbname, query, fetch="all")
    return r if r is not None else []


def psql_exec(dbname, query, params=None, timeout=8):
    """Записывающий запрос с параметрами (psycopg2 %s). True при успехе."""
    return _query(dbname, query, params, fetch="exec") is True


# Дефолтные модели роутинга (зеркало router.ROUTING) — для отображения + выбора в UI.
ROUTING_DEFAULT = {
    "orchestrator": "groq/llama-3.3-70b-versatile",
    "chief_of_staff": "groq/llama-3.3-70b-versatile",
    "email_watchdog": "groq/llama-3.3-70b-versatile",
    "knowledge_curator": "groq/llama-3.3-70b-versatile",
    "task_sync": "ollama/gpt-oss:20b",
    "calendar_agent": "groq/llama-3.3-70b-versatile",
    "support_agent": "groq/llama-3.3-70b-versatile",
    "lead_manager": "groq/llama-3.3-70b-versatile",
    "email_agent": "groq/llama-3.3-70b-versatile",
}
MODEL_CHOICES = [
    # Порядок = по качеству (то, что агенты реально могут вызвать через litellm).
    # «Топ» Claude/Codex — ручной уровень (нет API-ключей), здесь не выбирается.
    "qwen-free/qwen3.7-max",         # Qwen (FreeQwenApi :3264) — лучший общий, free
    "qwen-free/qwen3-coder-plus",    # Qwen — для кода, free
    "groq/llama-3.3-70b-versatile",  # Groq — быстрый дефолт, free
    "gemini/gemini-2.0-flash",       # Gemini — есть ключ, free tier
    "ollama/gpt-oss:20b",            # локально (ПК с Ollama)
    "ollama/qwen2.5-coder:7b",       # локально — код
    "ollama/gemma3:4b",              # локальная Gemma на ПК
]


def launchd_pid(label):
    out, rc = sh(["launchctl", "list", label])
    if rc != 0:
        return None, False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith('"PID"') or s.startswith("PID"):
            d = "".join(c for c in s if c.isdigit())
            return (int(d) if d else None), True
    return None, True


def container_running(name):
    out, rc = sh([DOCKER, "inspect", "--format", "{{.State.Status}}", name])
    return rc == 0 and out == "running"


def http_ok(url):
    try:
        urllib.request.urlopen(urllib.request.Request(url), timeout=4)
        return True
    except Exception:
        return False


def agents_state(usage_by_agent):
    out = []
    for key, name, sched, typ, contour, label in AGENTS:
        st = {"key": key, "name": name, "schedule": sched, "type": typ, "contour": contour}
        if label:
            pid, loaded = launchd_pid(label)
            st["loaded"] = loaded
            st["pid"] = pid
            if typ == "longrun":
                st["status"] = "running" if pid else ("loaded" if loaded else "down")
            else:
                st["status"] = "scheduled" if loaded else "down"
        else:
            st["status"] = "cron" if typ == "sched" else "on-demand"
        u = usage_by_agent.get(key)
        st["last_call"] = u["last"] if u else None
        st["calls"] = u["calls"] if u else 0
        out.append(st)
    return out


def build_state():
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        f_usage = ex.submit(psql_json, "ops_db",
            "SELECT agent, count(*) calls, sum(prompt_tokens+completion_tokens) tokens, "
            "round(sum(cost_rub)::numeric,2) cost, to_char(max(ts),'MM-DD HH24:MI') last "
            "FROM llm_usage GROUP BY agent ORDER BY calls DESC")
        f_runs = ex.submit(psql_json, "ops_db",
            "SELECT kind, status, to_char(ts,'MM-DD HH24:MI') ts FROM infra_runs ORDER BY ts DESC LIMIT 8")
        f_hb = ex.submit(psql_json, "ops_db",
            "SELECT component, status, to_char(last_seen,'MM-DD HH24:MI') seen FROM infra_heartbeats ORDER BY component")
        f_cost = ex.submit(psql, "ops_db",
            "SELECT round(COALESCE(sum(cost_rub),0)::numeric,2) FROM llm_usage WHERE date_trunc('month',ts)=date_trunc('month',now())")
        f_paid = ex.submit(psql, "ops_db",
            "SELECT round(COALESCE(sum(cost_rub),0)::numeric,2) FROM llm_usage WHERE tier=2 AND date_trunc('month',ts)=date_trunc('month',now())")
        f_tier1 = ex.submit(psql_json, "ops_db",
            "SELECT task_type, status, to_char(opened_at,'MM-DD HH24:MI') at FROM tier1_sessions ORDER BY opened_at DESC LIMIT 6")
        f_containers = ex.submit(lambda: {c: container_running(c) for c in CONTAINERS})
        f_qdrant = ex.submit(lambda: _qdrant())
        f_dbs = ex.submit(_db_summary)
        f_projects = ex.submit(psql_json, "ops_db",
            "SELECT p.id, p.name, p.domain, p.status, "
            "count(t.*) total, count(t.*) FILTER (WHERE t.status='done') done, "
            "count(t.*) FILTER (WHERE t.status IN ('queued','claimed','running')) active, "
            "count(t.*) FILTER (WHERE t.status='failed') failed "
            "FROM projects p LEFT JOIN tasks t ON t.project_id=p.id "
            "GROUP BY p.id ORDER BY p.created_at DESC LIMIT 12")
        f_reports = ex.submit(psql_json, "ops_db",
            "SELECT agent, kind, title, summary, to_char(ts,'MM-DD HH24:MI') ts "
            "FROM reports ORDER BY ts DESC, id DESC LIMIT 25")
        f_registry = ex.submit(psql_json, "ops_db",
            "SELECT agent_key, display_name, role, domain, parent_agent, kind, enabled "
            "FROM agent_registry ORDER BY (parent_agent IS NULL) DESC, parent_agent, kind, agent_key")
        f_tasks = ex.submit(psql_json, "ops_db",
            "SELECT id, project_id, title, domain, assignee, status, "
            "to_char(updated_at,'MM-DD HH24:MI') upd FROM tasks "
            "WHERE status IN ('queued','claimed','running','failed') "
            "OR updated_at > now()-interval '1 day' ORDER BY "
            "array_position(ARRAY['running','claimed','queued','failed','done'], status), id DESC LIMIT 60")
        f_content = ex.submit(psql_json, "ops_db",
            "SELECT id, channel, kind, status, COALESCE(NULLIF(title,''), left(body,60)) title, "
            "left(body,400) body, left(image_brief,300) image_brief, "
            "to_char(created_at,'MM-DD HH24:MI') created FROM content_items "
            "ORDER BY array_position(ARRAY['pending','approved','published','rejected'], status), id DESC LIMIT 20")

        usage = f_usage.result()
        usage_by_agent = {u["agent"]: {"calls": u["calls"], "last": u["last"]} for u in usage}
        containers = f_containers.result()
        runs = f_runs.result()
        hb = f_hb.result()
        budget_cap = psql("ops_db", "SELECT value FROM budget_config WHERE key='monthly_paid_cap_rub'") or "?"

    agents = agents_state(usage_by_agent)
    # Модель на агента: override из agent_config, иначе дефолт роутинга
    overrides = {r["agent_key"]: r["model"] for r in
                 psql_json("ops_db", "SELECT agent_key, model FROM agent_config WHERE model IS NOT NULL AND model<>''")}
    for a in agents:
        a["model"] = overrides.get(a["key"]) or ROUTING_DEFAULT.get(a["key"], "groq/llama-3.3-70b-versatile")
        a["model_overridden"] = a["key"] in overrides
    up = sum(1 for a in agents if a["status"] in ("running", "scheduled", "cron"))
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "agents_up": up, "agents_total": len(agents),
            "containers_up": sum(1 for v in containers.values() if v), "containers_total": len(containers),
            "month_cost": f_cost.result() or "0", "paid_cost": f_paid.result() or "0", "paid_cap": budget_cap,
        },
        "agents": agents, "usage": usage, "runs": runs, "heartbeats": hb,
        "tier1": f_tier1.result(), "containers": containers,
        "qdrant": f_qdrant.result(), "dbs": f_dbs.result(),
        "projects": f_projects.result(), "reports": f_reports.result(),
        "registry": f_registry.result(), "tasks": f_tasks.result(),
        "content": f_content.result(),
        "model_choices": MODEL_CHOICES,
    }


def _qdrant():
    try:
        with urllib.request.urlopen("http://localhost:6333/collections", timeout=4) as r:
            cols = json.load(r)["result"]["collections"]
            return [c["name"] for c in cols]
    except Exception:
        return []


def _db_summary():
    res = {}
    tables = {
        "agents": ["team_members", "conversations", "chief_digests", "known_entities", "task_snapshots"],
        "customer_db": ["leads", "support_tickets", "support_messages"],
        "ops_db": ["llm_usage", "tier1_sessions", "infra_runs"],
    }
    for db, tbls in tables.items():
        res[db] = {}
        for t in tbls:
            c = psql(db, f"SELECT count(*) FROM {t}")
            res[db][t] = int(c) if (c and c.isdigit()) else None
    return res


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)
    def do_GET(self):
        path = self.path.split("?", 1)[0]  # отрезаем query (?t=... ломал роут '/')
        if path.startswith("/api/state"):
            try: self._send(200, json.dumps(build_state()))
            except Exception as e: self._send(500, json.dumps({"error": str(e)}))
        elif path == "/" or path.startswith("/index"):
            self._serve_html("index.html")
        elif path.startswith("/office"):
            self._serve_html("office.html")
        elif path.startswith("/api/docs"):
            try:
                doc = os.path.expanduser("~/ai-infra/docs/HOW_IT_WORKS.md")
                with open(doc, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/plain; charset=utf-8")
            except Exception as e:
                self._send(404, f"# Документ не найден\n\n{e}", "text/plain; charset=utf-8")
        elif path.startswith("/docs"):
            self._serve_html("docs.html")
        else:
            self._send(404, "not found")

    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        try:
            if self.path.startswith("/api/agent/model"):
                key = str(body.get("key", "")).strip()
                model = str(body.get("model", "")).strip()
                if key not in ROUTING_DEFAULT:
                    return self._send(400, json.dumps({"error": "unknown agent"}))
                # пустая/дефолтная модель = снять override (model=NULL)
                _sql = ("INSERT INTO agent_config(agent_key,model,updated_at) VALUES (%s,%s,now()) "
                        "ON CONFLICT (agent_key) DO UPDATE SET model=%s, updated_at=now()")
                if not model or model == ROUTING_DEFAULT.get(key):
                    ok = psql_exec("ops_db", _sql, (key, None, None))
                elif model in MODEL_CHOICES:
                    ok = psql_exec("ops_db", _sql, (key, model, model))
                else:
                    return self._send(400, json.dumps({"error": "invalid model"}))
                return self._send(200 if ok else 500, json.dumps({"ok": ok, "key": key, "model": model}))
            elif self.path.startswith("/api/project/new"):
                goal = str(body.get("goal", "")).strip()
                if not goal:
                    return self._send(400, json.dumps({"error": "no goal"}))
                ag = os.path.expanduser("~/ai-infra/agents")
                subprocess.Popen(["/opt/anaconda3/bin/python3", os.path.join(ag, "project_manager.py"), goal], cwd=ag)
                return self._send(200, json.dumps({"ok": True, "goal": goal}))
            elif self.path.startswith("/api/content/new"):
                brief = str(body.get("brief", "")).strip()
                channel = str(body.get("channel", "telegram")).strip() or "telegram"
                kind = str(body.get("kind", "post")).strip() or "post"
                if not brief:
                    return self._send(400, json.dumps({"error": "no brief"}))
                ag = os.path.expanduser("~/ai-infra/agents")
                subprocess.Popen(["/opt/anaconda3/bin/python3", os.path.join(ag, "content_factory.py"),
                                  brief, channel, kind], cwd=ag)
                return self._send(200, json.dumps({"ok": True, "brief": brief}))
            elif self.path.startswith("/api/content/approve") or self.path.startswith("/api/content/reject"):
                try:
                    cid = int(body.get("id"))
                except Exception:
                    return self._send(400, json.dumps({"error": "bad id"}))
                action = "approve" if "approve" in self.path else "reject"
                ag = os.path.expanduser("~/ai-infra/agents")
                subprocess.Popen(["/opt/anaconda3/bin/python3", os.path.join(ag, "content_factory.py"),
                                  action, str(cid)], cwd=ag)
                return self._send(200, json.dumps({"ok": True, "id": cid, "action": action}))
            elif self.path.startswith("/api/budget"):
                try:
                    cap = int(body.get("cap"))
                except Exception:
                    return self._send(400, json.dumps({"error": "bad cap"}))
                if not (0 <= cap <= 1000000):
                    return self._send(400, json.dumps({"error": "cap out of range"}))
                ok = psql_exec("ops_db",
                    "UPDATE budget_config SET value=%s WHERE key='monthly_paid_cap_rub'", (str(cap),))
                return self._send(200 if ok else 500, json.dumps({"ok": ok, "cap": cap}))
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))

    def _serve_html(self, fname):
        try:
            with open(os.path.join(HERE, fname), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(404, f"{fname} not found")


if __name__ == "__main__":
    print(f"Amori Infra Ops → http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
