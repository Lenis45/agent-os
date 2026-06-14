"""
db — единое подключение к Postgres для агентов (v3.0 hardening).

Пароль ТОЛЬКО из окружения (.env) — без хардкод-фолбэка в коде.
Таймауты на соединение и на запрос, чтобы агент не висел вечно.
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB_HOST = os.getenv("PGHOST", "127.0.0.1")
DB_PORT = int(os.getenv("PGPORT", "5432"))
DB_USER = os.getenv("PGUSER", "agent_user")
CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000"))


def _password() -> str:
    pw = os.getenv("POSTGRES_PASSWORD")
    if not pw:
        raise RuntimeError(
            "POSTGRES_PASSWORD не задан в окружении — проверь ~/ai-infra/agents/.env"
        )
    return pw


def connect(dbname: str = "agents"):
    """psycopg2-соединение с таймаутами. Закрывать вызывающему (или through with)."""
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=dbname, user=DB_USER,
        password=_password(), connect_timeout=CONNECT_TIMEOUT,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


def wait_ready(dbname: str = "agents", retries: int = 30, delay: float = 2.0) -> bool:
    """Дождаться готовности Postgres. На буте Docker/PG поднимается ПОЗЖE агентов
    (launchd стартует их сразу) → без ожидания агент падает с OperationalError.
    Пингует connect() с ретраями; True если поднялся, иначе False (не бросает)."""
    import time
    for i in range(retries):
        try:
            connect(dbname).close()
            if i:
                print(f"[db] Postgres готов (после {i} попыток)")
            return True
        except Exception as e:
            if i == 0:
                print(f"[db] жду Postgres… ({e.__class__.__name__})")
            time.sleep(delay)
    print("[db] Postgres не поднялся за отведённое время — продолжаю (KeepAlive перезапустит)")
    return False


def query(sql: str, params=None, dbname: str = "agents"):
    """SELECT → список кортежей. Открывает/закрывает соединение сам."""
    conn = connect(dbname)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        conn.close()


def execute(sql: str, params=None, dbname: str = "agents") -> int:
    """INSERT/UPDATE/DELETE с commit. Возвращает rowcount."""
    conn = connect(dbname)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    print("[db] ping:", query("SELECT 1")[0][0], "@", f"{DB_HOST}:{DB_PORT}")
