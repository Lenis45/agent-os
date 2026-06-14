"""
report — единый отчёт-хаб AI-команды.

Все агенты сдают отчёты сюда. Основной канал — веб-дашборд (ops_db.reports),
опционально дублируется в Telegram. Любая ошибка записи не валит вызывающего.
"""
import json
import ops_store


def report(agent, kind="note", title="", summary="", body="",
           project_id=None, task_id=None, link=None, meta=None, telegram=False) -> int:
    """Записать отчёт в ops_db.reports. Возвращает id (или 0 при ошибке)."""
    rid = 0
    try:
        conn = ops_store.get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO reports(agent, kind, title, summary, body, project_id, task_id, link, meta) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (agent, kind, title, summary, body, project_id, task_id, link, json.dumps(meta or {})),
            )
            rid = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[report] не удалось записать: {e}")
    if telegram:
        try:
            import notify
            notify.send(f"📋 [{agent}] {title}\n{summary}"[:3500])
        except Exception:
            pass
    return rid


def recent(limit=30, project_id=None, agent=None):
    try:
        conn = ops_store.get_conn()
        try:
            cur = conn.cursor()
            q = ("SELECT agent, kind, title, summary, project_id, task_id, "
                 "to_char(ts,'MM-DD HH24:MI') ts FROM reports WHERE TRUE")
            p = []
            if project_id:
                q += " AND project_id=%s"; p.append(project_id)
            if agent:
                q += " AND agent=%s"; p.append(agent)
            q += " ORDER BY ts DESC, id DESC LIMIT %s"; p.append(limit)
            cur.execute(q, p)
            cols = [d[0] for d in (cur.description or [])]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


if __name__ == "__main__":
    ops_store.init()
    rid = report("cli", kind="note", title="smoke", summary="проверка отчёт-хаба")
    print("report id:", rid, "| recent:", len(recent(5)))
    import ops_store as o
    conn = o.get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM reports WHERE agent='cli'"); conn.commit(); conn.close()
    print("cleanup ok")
