"""
worker_dispatch — диспетчер очереди задач AI-команды.

Гоняет всех включённых воркеров (agent_registry.kind in worker/lead): каждый круг
пытается взять и выполнить задачи из очереди. Под launchd `ai.worker` (KeepAlive).
Сонлив когда очередь пуста, активен когда есть задачи. Пишет heartbeat в ops_db.

Специализированные хендлеры воркеров (Фаза 3) регистрируются здесь через
base_agent.register(); пока все используют универсальный LLM-хендлер.
"""
import time
import ops_store
import base_agent
import worker_handlers

PER_WORKER_PER_ROUND = 3


def enabled_workers():
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_key FROM agent_registry "
                    "WHERE enabled AND kind IN ('worker', 'lead') ORDER BY agent_key")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def one_pass() -> int:
    """Один проход по всем воркерам. Возвращает число обработанных задач."""
    processed = 0
    for w in enabled_workers():
        for _ in range(PER_WORKER_PER_ROUND):
            if base_agent.process_one(w):
                processed += 1
            else:
                break
    return processed


def main():
    ops_store.wait_ready()  # на буте Postgres поднимается позже агента
    ops_store.init()
    keys = worker_handlers.register_all()
    print(f"[dispatch] worker dispatcher запущен; спецхендлеры: {', '.join(keys)}")
    idle = 0
    while True:
        try:
            n = one_pass()
        except Exception as e:
            print(f"[dispatch] ошибка прохода: {e}")
            n = 0
        try:
            ops_store.heartbeat("worker_dispatch", "ok", {"processed": n})
        except Exception:
            pass
        idle = 0 if n else idle + 1
        time.sleep(3 if idle < 3 else 20)


if __name__ == "__main__":
    main()
