"""
tier1_log — лог ручных Claude Pro / ChatGPT Plus сессий (v3.0).

Закрывает главный пробел v2.0: Tier-1 (самый дорогой слой) был полностью «слепым».
Теперь цикл Context Builder замыкается — что выдали человеку и что он принёс назад
фиксируется в ops_db.tier1_sessions. Появляется ответ на вопрос:
«какие задачи реально едят ручной Tier-1 и где пора автоматизировать».

Жизненный цикл:
    sid = open_session(source="telegram", task_type="code_debug", prompt_text=PROMPT)
    # ... человек вставил промпт в Claude, принёс ответ ...
    answer(sid, response_text=ANSWER)
    close(sid, status="applied")   # applied | abandoned
"""
import json
import ops_store


def open_session(source: str = "telegram", task_type: str = None,
                 model: str = "claude-pro-web", prompt_text: str = None,
                 meta: dict = None) -> int:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tier1_sessions(source, task_type, model, prompt_text, status, meta) "
            "VALUES (%s,%s,%s,%s,'open',%s) RETURNING id",
            (source, task_type, model, prompt_text, json.dumps(meta or {})),
        )
        sid = cur.fetchone()[0]
        conn.commit()
        return sid
    finally:
        conn.close()


def answer(session_id: int, response_text: str, est_cost_rub: float = 0.0) -> None:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tier1_sessions SET response_text=%s, est_cost_rub=%s, status='answered' "
            "WHERE id=%s",
            (response_text, est_cost_rub, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def close(session_id: int, status: str = "applied") -> None:
    if status not in ("applied", "abandoned", "answered"):
        status = "applied"
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tier1_sessions SET closed_at=now(), status=%s WHERE id=%s",
            (status, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def log_session(prompt_text: str, response_text: str, source: str = "telegram",
                task_type: str = None, model: str = "claude-pro-web",
                status: str = "applied", est_cost_rub: float = 0.0) -> int:
    """Одношаговая запись завершённой сессии (когда промпт и ответ уже известны)."""
    sid = open_session(source=source, task_type=task_type, model=model, prompt_text=prompt_text)
    answer(sid, response_text=response_text, est_cost_rub=est_cost_rub)
    close(sid, status=status)
    return sid


def stats(days: int = 30) -> dict:
    """Сводка по Tier-1 за период — для дайджестов Chief of Staff."""
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*), COALESCE(SUM(est_cost_rub),0), "
            "  count(*) FILTER (WHERE status='applied'), "
            "  count(*) FILTER (WHERE status='abandoned') "
            "FROM tier1_sessions WHERE opened_at > now() - (%s || ' days')::interval",
            (days,),
        )
        total, cost, applied, abandoned = cur.fetchone()
        cur.execute(
            "SELECT task_type, count(*) c FROM tier1_sessions "
            "WHERE opened_at > now() - (%s || ' days')::interval "
            "GROUP BY task_type ORDER BY c DESC LIMIT 5",
            (days,),
        )
        by_type = [{"task_type": r[0], "count": r[1]} for r in cur.fetchall()]
        return {"days": days, "total": total, "est_cost_rub": float(cost),
                "applied": applied, "abandoned": abandoned, "by_type": by_type}
    finally:
        conn.close()


if __name__ == "__main__":
    ops_store.init()
    sid = log_session(
        prompt_text="# Контекст\nОшибка в Go бэкенде ...",
        response_text="1. Root cause ...\n2. Fix ...",
        source="telegram", task_type="code_debug", status="applied",
    )
    print(f"[tier1_log] записана сессия #{sid}")
    print(f"[tier1_log] stats(30d) = {stats(30)}")
