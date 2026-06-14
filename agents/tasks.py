"""
tasks — очередь задач AI-команды на БД (ops_db). Без Celery.

Атомарный захват задачи воркером — SELECT … FOR UPDATE SKIP LOCKED, поэтому
несколько воркеров безопасно тянут из одной очереди. Поддержка под-задач
(parent_task_id) и зависимостей (deps: задача не берётся, пока её deps не 'done').

API: create_project, enqueue, claim, start, complete, fail, block,
     get_task, list_tasks, project_summary.
"""
import json
import ops_store

_COLS = ("id", "project_id", "parent_task_id", "title", "spec", "domain", "assignee",
         "status", "priority", "deps", "result", "error", "claimed_by", "attempts",
         "created_at", "updated_at", "meta")


def _conn():
    return ops_store.get_conn()


def _row_to_dict(cur, row):
    if row is None:
        return None
    cols = [d[0] for d in (cur.description or [])]
    d = dict(zip(cols, row))
    for k in ("created_at", "updated_at", "claimed_at", "started_at", "finished_at", "ts"):
        if k in d and d[k] is not None:
            d[k] = d[k].isoformat()
    return d


def _event(cur, task_id, event, detail=None):
    cur.execute("INSERT INTO task_events(task_id, event, detail) VALUES (%s,%s,%s)",
                (task_id, event, json.dumps(detail or {})))


def create_project(name, goal=None, domain=None, owner_agent=None, meta=None) -> int:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects(name, goal, domain, owner_agent, meta) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name, goal, domain, owner_agent, json.dumps(meta or {})),
        )
        pid = cur.fetchone()[0]
        conn.commit()
        return pid
    finally:
        conn.close()


def enqueue(title, spec=None, project_id=None, assignee=None, domain=None,
            priority=5, parent_task_id=None, deps=None, meta=None) -> int:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks(project_id, parent_task_id, title, spec, domain, assignee, "
            "priority, deps, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (project_id, parent_task_id, title, spec, domain, assignee, priority,
             json.dumps(deps or []), json.dumps(meta or {})),
        )
        tid = cur.fetchone()[0]
        _event(cur, tid, "created", {"assignee": assignee, "domain": domain})
        conn.commit()
        return tid
    finally:
        conn.close()


def claim(agent_key):
    """Атомарно взять самую приоритетную queued-задачу агента, чьи deps все 'done'."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH nxt AS (
              SELECT t.id FROM tasks t
              WHERE t.status='queued' AND t.assignee=%s
                AND NOT EXISTS (
                  SELECT 1 FROM jsonb_array_elements_text(t.deps) AS d(dep)
                  JOIN tasks dt ON dt.id = d.dep::bigint
                  WHERE dt.status <> 'done'
                )
              ORDER BY t.priority, t.id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE tasks SET status='claimed', claimed_by=%s, claimed_at=now(),
                   attempts=attempts+1, updated_at=now()
            FROM nxt WHERE tasks.id = nxt.id
            RETURNING tasks.id, tasks.project_id, tasks.title, tasks.spec,
                      tasks.domain, tasks.assignee, tasks.meta
            """,
            (agent_key, agent_key),
        )
        row = cur.fetchone()
        result = None
        if row:
            cols = [d[0] for d in (cur.description or [])]  # до _event (он обнулит description)
            result = dict(zip(cols, row))
            _event(cur, row[0], "claimed", {"by": agent_key})
        conn.commit()
        return result
    finally:
        conn.close()


def _set_status(task_id, status, extra_sql="", params=(), event=None, detail=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE tasks SET status=%s, updated_at=now() {extra_sql} WHERE id=%s",
            (status, *params, task_id),
        )
        if event:
            _event(cur, task_id, event, detail)
        conn.commit()
    finally:
        conn.close()


def start(task_id):
    _set_status(task_id, "running", ", started_at=now()", (), "started")


def complete(task_id, result=None):
    _set_status(task_id, "done", ", result=%s, finished_at=now()", (result,),
                "completed", {"len": len(result or "")})


def fail(task_id, error=None):
    _set_status(task_id, "failed", ", error=%s, finished_at=now()", (str(error or "")[:2000],),
                "failed", {"error": str(error or "")[:200]})


def block(task_id, reason=None):
    _set_status(task_id, "blocked", ", error=%s", (str(reason or "")[:2000],),
                "blocked", {"reason": str(reason or "")[:200]})


def get_task(task_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
        return _row_to_dict(cur, cur.fetchone())
    finally:
        conn.close()


def dep_results(task_id):
    """Результаты ВСЕХ задач-предков (транзитивно) — апстрим-контекст для воркера.

    Берёт не только прямые deps, но и deps-of-deps по всей цепочке, чтобы,
    например, ревьюер видел и текст копирайтера, и бриф дизайнера.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH RECURSIVE anc AS (
              SELECT d.id FROM tasks t
              JOIN jsonb_array_elements_text(t.deps) AS x(dep) ON TRUE
              JOIN tasks d ON d.id = x.dep::bigint
              WHERE t.id=%s
              UNION
              SELECT d2.id FROM anc
              JOIN tasks a ON a.id = anc.id
              JOIN jsonb_array_elements_text(a.deps) AS x2(dep) ON TRUE
              JOIN tasks d2 ON d2.id = x2.dep::bigint
            )
            SELECT d.id, d.title, d.result FROM anc
            JOIN tasks d ON d.id = anc.id
            WHERE d.result IS NOT NULL ORDER BY d.id
            """,
            (task_id,),
        )
        return [{"id": r[0], "title": r[1], "result": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def list_tasks(status=None, project_id=None, assignee=None, limit=100):
    conn = _conn()
    try:
        cur = conn.cursor()
        q = "SELECT id, project_id, title, domain, assignee, status, priority, " \
            "to_char(updated_at,'MM-DD HH24:MI') upd FROM tasks WHERE TRUE"
        p = []
        if status:
            q += " AND status=%s"; p.append(status)
        if project_id:
            q += " AND project_id=%s"; p.append(project_id)
        if assignee:
            q += " AND assignee=%s"; p.append(assignee)
        q += " ORDER BY priority, id DESC LIMIT %s"; p.append(limit)
        cur.execute(q, p)
        cols = [d[0] for d in (cur.description or [])]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def project_summary():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, p.name, p.domain, p.status, "
            "count(t.*) total, "
            "count(t.*) FILTER (WHERE t.status='done') done, "
            "count(t.*) FILTER (WHERE t.status IN ('queued','claimed','running')) active, "
            "count(t.*) FILTER (WHERE t.status='failed') failed "
            "FROM projects p LEFT JOIN tasks t ON t.project_id=p.id "
            "GROUP BY p.id ORDER BY p.created_at DESC LIMIT 50"
        )
        cols = [d[0] for d in (cur.description or [])]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    ops_store.init()
    pid = create_project("smoke-test", goal="проверка очереди", domain="ops", owner_agent="cli")
    t1 = enqueue("шаг 1", spec="сделать A", project_id=pid, assignee="dev_worker", domain="dev")
    t2 = enqueue("шаг 2", spec="сделать B после A", project_id=pid, assignee="dev_worker",
                 domain="dev", deps=[t1])
    print(f"project={pid} tasks={t1},{t2}")
    c = claim("dev_worker")
    print("claimed:", c["id"] if c else None, "(должен быть", t1, "— шаг2 заблокирован deps)")
    start(c["id"]); complete(c["id"], "A готово")
    c2 = claim("dev_worker")
    print("после done t1, claimed:", c2["id"] if c2 else None, "(должен быть", t2, ")")
    complete(c2["id"], "B готово")
    print("summary:", project_summary()[0])
    # cleanup
    import ops_store as o
    conn = o.get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id=%s", (pid,)); conn.commit(); conn.close()
    print("cleanup ok")
