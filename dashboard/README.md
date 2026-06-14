# dashboard/

The ops control panel — a single-file Python HTTP server, no framework, no external dependencies
beyond psycopg2 and dotenv.

## What it shows

**`http://localhost:8099`**

| Section | Data |
|---|---|
| **Agents** | All 9 agents: status, PID, last heartbeat, LLM calls, model, configurable via click |
| **🏭 Content factory** | Pipeline cards (pending/approved/published), full body + image brief on open, ✅/✗ buttons |
| **🗂 Task board** | Kanban: running / queued / failed / done from `ops_db.tasks` |
| **📊 Reports feed** | Latest agent reports in chronological order |
| **👥 Team** | Hierarchy tree: Emilia → domain leads → workers |
| **Infrastructure** | LLM usage + cost by agent, infra runs (backup/monitor/restore), DB health |

**`http://localhost:8099/office`** — ambient CSS view (department wings, pulse, report ticker)  
**`http://localhost:8099/docs`** — HOW_IT_WORKS.md rendered with marked.js

## Architecture

Previous version ran ~15 `docker exec psql` subprocesses per `/api/state` call — latency was ~5s,
and under load (office pixel client + browser polling simultaneously) it caused docker daemon
contention that made the registry appear empty.

Current version uses **psycopg2 ThreadedConnectionPool** directly:

```python
_pools = {
    "ops_db": ThreadedConnectionPool(1, 5, dbname="ops_db", ...),
    "customer_db": ThreadedConnectionPool(1, 3, dbname="customer_db", ...),
}

def psql_json(db: str, q: str, params=None) -> list:
    conn = _pools[db].getconn()
    try:
        cur = conn.cursor()
        cur.execute(q, params or [])
        cols = [d[0] for d in (cur.description or [])]
        return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]
    finally:
        conn.rollback()
        _pools[db].putconn(conn)
```

Result: `/api/state` latency **~5s → 0.1s**, stable under 20 concurrent requests.

The `_jsonable()` helper converts `Decimal` → `float` and `date` → ISO string so JSON serialization
never crashes on numeric/date DB values.

## POST endpoints

All writes are parameterized (`%s`) — no string interpolation of user input.

| Endpoint | Action |
|---|---|
| `POST /api/project/new` | Spawns `project_manager.py` in a subprocess |
| `POST /api/content/new` | Spawns `content_factory.py` |
| `POST /api/content/approve` | Sets content status to `approved` → publish |
| `POST /api/content/reject` | Sets content status to `rejected` |
| `POST /api/agent/model` | Updates `ops_db.agent_config` (model override) |
| `POST /api/budget` | Updates monthly paid-API cap |

## Launchd

```xml
<!-- ~/Library/LaunchAgents/ai.dashboard.plist -->
<key>KeepAlive</key><true/>
<key>ProgramArguments</key>
<array>
  <string>/opt/anaconda3/bin/python3</string>
  <string>/Users/denis/ai-infra/dashboard/server.py</string>
</array>
```

Restarts automatically on crash. Port 8099.
