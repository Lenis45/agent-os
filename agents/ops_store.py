"""
ops_store — изолированное хранилище операционных/observability данных (v3.0).

Живёт в отдельной БД `ops_db` (тот же инстанс ai_postgres), чтобы не смешиваться
с Langfuse-схемой в базе `agents`. Сюда пишутся:
  - llm_usage       : учёт ВСЕХ LLM-вызовов (free + paid) — закрывает слепоту наблюдаемости
  - tier1_sessions  : лог ручных Claude/GPT сессий (Tier-1) — главный пробел v2.0
  - budget_config   : месячный потолок расходов для cost_guard

Схема идемпотентна (CREATE TABLE IF NOT EXISTS) — init() можно звать сколько угодно.
"""
import os
import psycopg2
from dotenv import load_dotenv

# Грузим именно agents/.env (а не из CWD процесса) — иначе при импорте из другого
# каталога пароль не подхватывался и срабатывал хардкод-фолбэк (утечка).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Тот же инстанс/креды что и у остальных агентов, но отдельная БД ops_db.
DB_HOST = os.getenv("OPS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("OPS_DB_PORT", "5432"))
DB_NAME = os.getenv("OPS_DB_NAME", "ops_db")
DB_USER = os.getenv("OPS_DB_USER", "agent_user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
if not DB_PASSWORD:
    raise RuntimeError("POSTGRES_PASSWORD не задан в окружении — проверь ~/ai-infra/agents/.env")


def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent           TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    tier            SMALLINT    NOT NULL DEFAULT 3,   -- 1=manual flagship, 2=paid API, 3=free/local
    prompt_tokens   INTEGER     NOT NULL DEFAULT 0,
    cached_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER   NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    token_count_source TEXT     NOT NULL DEFAULT 'estimated', -- provider | local_o200k | heuristic
    cost_rub        NUMERIC(12,4) NOT NULL DEFAULT 0,
    source          TEXT        NOT NULL DEFAULT 'agent',
    meta            JSONB       NOT NULL DEFAULT '{}'
);
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cached_prompt_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS latency_ms INTEGER;
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS token_count_source TEXT NOT NULL DEFAULT 'estimated';
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts    ON llm_usage (ts);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_usage (agent);
CREATE INDEX IF NOT EXISTS idx_llm_usage_tier  ON llm_usage (tier);

CREATE TABLE IF NOT EXISTS tier1_sessions (
    id            BIGSERIAL PRIMARY KEY,
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ,
    source        TEXT        NOT NULL DEFAULT 'telegram',  -- откуда пришла задача
    task_type     TEXT,                                     -- code_debug, strategy, creative...
    model         TEXT        NOT NULL DEFAULT 'claude-pro-web',
    prompt_text   TEXT,
    response_text TEXT,
    est_cost_rub  NUMERIC(12,4) NOT NULL DEFAULT 0,         -- оценка (подписка ~0, но считаем условную)
    status        TEXT        NOT NULL DEFAULT 'open',      -- open | answered | applied | abandoned
    meta          JSONB       NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tier1_opened ON tier1_sessions (opened_at);
CREATE INDEX IF NOT EXISTS idx_tier1_status ON tier1_sessions (status);

CREATE TABLE IF NOT EXISTS budget_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Прогоны инфраструктурных задач (backup / restore_test / monitor / digest)
CREATE TABLE IF NOT EXISTS infra_runs (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind      TEXT NOT NULL,                 -- backup | restore_test | monitor | digest
    status    TEXT NOT NULL,                 -- ok | partial | fail
    detail    JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_infra_runs_kind ON infra_runs (kind, ts);

-- Heartbeat компонентов: last_seen для dead-man's-switch
CREATE TABLE IF NOT EXISTS infra_heartbeats (
    component  TEXT PRIMARY KEY,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status     TEXT NOT NULL DEFAULT 'ok',
    meta       JSONB NOT NULL DEFAULT '{}'
);

-- Настройки агентов из UI (переопределение модели и т.п.). router читает fail-safe.
CREATE TABLE IF NOT EXISTS agent_config (
    agent_key  TEXT PRIMARY KEY,
    model      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Мини-AI-команда: проекты, задачи, отчёты, иерархия =====

-- Реестр агентов: иерархия (лид→работники) и домены. UI читает дерево команды.
CREATE TABLE IF NOT EXISTS agent_registry (
    agent_key    TEXT PRIMARY KEY,
    display_name TEXT,
    role         TEXT,
    domain       TEXT,            -- content | research | dev | ops | infra | personal
    parent_agent TEXT,            -- agent_key тимлида (NULL = верхний уровень)
    kind         TEXT NOT NULL DEFAULT 'worker',  -- lead | worker | assistant
    enabled      BOOLEAN NOT NULL DEFAULT true,
    meta         JSONB NOT NULL DEFAULT '{}',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Проекты, которые ведёт AI-команда
CREATE TABLE IF NOT EXISTS projects (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    goal        TEXT,
    domain      TEXT,
    owner_agent TEXT,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | done | paused | cancelled
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta        JSONB NOT NULL DEFAULT '{}'
);

-- Задачи (очередь + результат). Поддержка под-задач (parent_task_id) и зависимостей.
CREATE TABLE IF NOT EXISTS tasks (
    id             BIGSERIAL PRIMARY KEY,
    project_id     BIGINT REFERENCES projects(id) ON DELETE CASCADE,
    parent_task_id BIGINT,
    title          TEXT NOT NULL,
    spec           TEXT,
    domain         TEXT,
    assignee       TEXT,         -- agent_key, кому назначена
    status         TEXT NOT NULL DEFAULT 'queued',  -- queued|claimed|running|done|failed|blocked
    priority       SMALLINT NOT NULL DEFAULT 5,     -- меньше = важнее
    deps           JSONB NOT NULL DEFAULT '[]',     -- [task_id,...] которые должны быть done
    result         TEXT,
    error          TEXT,
    claimed_by     TEXT,
    attempts       SMALLINT NOT NULL DEFAULT 0,
    claimed_at     TIMESTAMPTZ,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta           JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks (status, priority, id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks (assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project  ON tasks (project_id);

-- Аудит задач
CREATE TABLE IF NOT EXISTS task_events (
    id      BIGSERIAL PRIMARY KEY,
    task_id BIGINT NOT NULL,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    event   TEXT NOT NULL,
    detail  JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events (task_id, ts);

-- Единый отчёт-хаб: все агенты сдают отчёты сюда (основной канал — веб-дашборд)
CREATE TABLE IF NOT EXISTS reports (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent      TEXT NOT NULL,
    project_id BIGINT,
    task_id    BIGINT,
    kind       TEXT NOT NULL DEFAULT 'note',  -- digest|result|alert|content|research|note
    title      TEXT,
    summary    TEXT,
    body       TEXT,
    link       TEXT,
    meta       JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reports_ts      ON reports (ts);
CREATE INDEX IF NOT EXISTS idx_reports_agent   ON reports (agent, ts);
CREATE INDEX IF NOT EXISTS idx_reports_project ON reports (project_id, ts);

-- Контент-завод (продажи): единицы контента с аппрув-гейтом и публикацией
CREATE TABLE IF NOT EXISTS content_items (
    id           BIGSERIAL PRIMARY KEY,
    project_id   BIGINT,
    task_id      BIGINT,
    channel      TEXT NOT NULL DEFAULT 'telegram',  -- telegram | vk | email | landing | ad
    kind         TEXT NOT NULL DEFAULT 'post',       -- post | email | ad_creative | landing
    brief        TEXT,
    title        TEXT,
    body         TEXT,
    image_brief  TEXT,
    review       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',    -- draft|pending|approved|ready|rejected|published|failed
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at  TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    meta         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_content_status ON content_items (status, id);

-- Unified Intelligence Gateway
CREATE TABLE IF NOT EXISTS smart_requests (
    id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_message_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    prompt_text TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'ask',
    cwd TEXT,
    target_device TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'accepted',
    route JSONB NOT NULL DEFAULT '{}',
    input_artifact_ids JSONB NOT NULL DEFAULT '[]',
    result_text TEXT,
    evidence JSONB NOT NULL DEFAULT '[]',
    error_code TEXT,
    error_message TEXT,
    parent_request_id UUID,
    thread_id UUID,
    attempts SMALLINT NOT NULL DEFAULT 0,
    leased_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
ALTER TABLE smart_requests ADD COLUMN IF NOT EXISTS input_artifact_ids JSONB NOT NULL DEFAULT '[]';
ALTER TABLE smart_requests ADD COLUMN IF NOT EXISTS parent_request_id UUID;
ALTER TABLE smart_requests ADD COLUMN IF NOT EXISTS thread_id UUID;
UPDATE smart_requests SET thread_id=id WHERE thread_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_smart_requests_queue ON smart_requests (status, target_device, created_at);
CREATE INDEX IF NOT EXISTS idx_smart_requests_session ON smart_requests (source, actor_id, session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_smart_requests_thread ON smart_requests (thread_id, created_at);

CREATE TABLE IF NOT EXISTS smart_sessions (
    source TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    current_thread_id UUID,
    last_request_id UUID REFERENCES smart_requests(id) ON DELETE SET NULL,
    reset_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, actor_id, session_id)
);

CREATE TABLE IF NOT EXISTS smart_request_events (
    id BIGSERIAL PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES smart_requests(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    message TEXT,
    progress SMALLINT,
    meta JSONB NOT NULL DEFAULT '{}',
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_smart_request_events ON smart_request_events (request_id, id);

CREATE TABLE IF NOT EXISTS smart_artifacts (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES smart_requests(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'output',
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_smart_artifacts_request ON smart_artifacts (request_id, created_at);

CREATE TABLE IF NOT EXISTS smart_deliveries (
    id BIGSERIAL PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES smart_requests(id) ON DELETE CASCADE,
    artifact_id UUID,
    destination TEXT NOT NULL,
    external_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts SMALLINT NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS smart_workers (
    worker_id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '[]',
    versions JSONB NOT NULL DEFAULT '{}',
    auth_status JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'online',
    active_request_id UUID,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta JSONB NOT NULL DEFAULT '{}'
);
"""

DEFAULT_BUDGET = {
    # Месячный потолок на ПЛАТНЫЙ API (₽). Подписки сюда не входят.
    "monthly_paid_cap_rub": "2500",
    # Поведение при превышении: downgrade (в free tier) | block | queue_tier1
    "over_budget_action": "downgrade",
}


def init(seed_budget: bool = True) -> None:
    """Создать схему и (опц.) засеять дефолтный бюджет. Идемпотентно."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(SCHEMA)
        if seed_budget:
            for k, v in DEFAULT_BUDGET.items():
                cur.execute(
                    "INSERT INTO budget_config(key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING",
                    (k, v),
                )
        conn.commit()
    finally:
        conn.close()


def wait_ready(retries: int = 30, delay: float = 2.0) -> bool:
    """Дождаться готовности ops_db (на буте Postgres поднимается позже агентов)."""
    import time
    for i in range(retries):
        try:
            get_conn().close()
            if i:
                print(f"[ops_store] ops_db готова (после {i} попыток)")
            return True
        except Exception as e:
            if i == 0:
                print(f"[ops_store] жду ops_db… ({e.__class__.__name__})")
            time.sleep(delay)
    print("[ops_store] ops_db не поднялась — продолжаю (KeepAlive перезапустит)")
    return False


def get_budget(key: str, default: str = None) -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM budget_config WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_budget(key: str, value: str) -> None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO budget_config(key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def get_automation_state(key: str, default=None):
    """Read compact state used to avoid repeating unchanged automation work."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM automation_state WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_automation_state(key: str, value: dict) -> None:
    import json
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO automation_state(key, value, updated_at) VALUES (%s,%s,now()) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
            (key, json.dumps(value or {})),
        )
        conn.commit()
    finally:
        conn.close()


def record_run(kind: str, status: str, detail: dict = None) -> None:
    """Зафиксировать прогон инфра-задачи (backup/restore_test/monitor/digest)."""
    import json
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO infra_runs(kind, status, detail) VALUES (%s,%s,%s)",
            (kind, status, json.dumps(detail or {})),
        )
        conn.commit()
    finally:
        conn.close()


def heartbeat(component: str, status: str = "ok", meta: dict = None) -> None:
    """Обновить last_seen компонента (dead-man's-switch)."""
    import json
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO infra_heartbeats(component, last_seen, status, meta) "
            "VALUES (%s, now(), %s, %s) "
            "ON CONFLICT (component) DO UPDATE SET last_seen=now(), status=EXCLUDED.status, meta=EXCLUDED.meta",
            (component, status, json.dumps(meta or {})),
        )
        conn.commit()
    finally:
        conn.close()


def get_agent_models() -> dict:
    """{agent_key: model} из agent_config — переопределения модели из UI (для router)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_key, model FROM agent_config WHERE model IS NOT NULL AND model <> ''")
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def set_agent_model(agent_key: str, model) -> None:
    """Задать/снять переопределение модели агента (model=None/'' снимает)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if model:
            cur.execute(
                "INSERT INTO agent_config(agent_key, model, updated_at) VALUES (%s,%s,now()) "
                "ON CONFLICT (agent_key) DO UPDATE SET model=EXCLUDED.model, updated_at=now()",
                (agent_key, model),
            )
        else:
            cur.execute(
                "INSERT INTO agent_config(agent_key, model, updated_at) VALUES (%s,NULL,now()) "
                "ON CONFLICT (agent_key) DO UPDATE SET model=NULL, updated_at=now()",
                (agent_key,),
            )
        conn.commit()
    finally:
        conn.close()


def last_run(kind: str):
    """Вернуть (ts, status) последнего прогона данного вида или None."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ts, status FROM infra_runs WHERE kind=%s ORDER BY ts DESC LIMIT 1", (kind,))
        return cur.fetchone()
    finally:
        conn.close()


if __name__ == "__main__":
    init()
    print(f"[ops_store] ops_db готова @ {DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"[ops_store] monthly_paid_cap_rub = {get_budget('monthly_paid_cap_rub')}")
    print(f"[ops_store] over_budget_action  = {get_budget('over_budget_action')}")
