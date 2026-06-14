# agents/

The core of the system — 9 Python agents sharing a set of battle-tested libraries.

## Shared libraries

Every agent imports from the same four files. No copy-pasting, no divergence.

| File | What it does |
|---|---|
| `db.py` | `connect(dbname)` — env-only password, connect timeout, `wait_ready()` for launchd boot races |
| `llm.py` | `build_agent(role, model)` via `router.get_model()`; `run(agent, prompt)` records usage to `ops_db.llm_usage`; `groq_chat()` for Groq-direct; `parse_json()` |
| `notify.py` | `send_msg(text)` — Telegram with 4000-char chunking and 3× retry on SSL errors |
| `applog.py` | `get_logger(name)` — structured logging to `agents/<name>.log` |
| `retry.py` | `net_retry` (tenacity decorator) · `safe(fn)` — wraps external calls |

## Model routing (`router.py`)

```python
ROUTING = {
    "chief_of_staff":    "groq/llama-3.3-70b-versatile",  # needs speed
    "email_watchdog":    "groq/llama-3.3-70b-versatile",
    "task_sync":         "ollama/gpt-oss:20b",             # local — stays private
    "research_agent":    "ollama/gpt-oss:20b",             # heavy analysis — local
    "code_agent":        "ollama/qwen2.5-coder:7b",        # code — local
    # default:           "groq/llama-3.3-70b-versatile"
}
```

- UI overrides per-agent (from `ops_db.agent_config`) take priority, cached 30s.
- If Ollama is unreachable → automatic fallback to Groq.
- `cost_guard.py` downgrades paid models to free tier when monthly budget is exhausted.

## Task queue (`tasks.py`)

```sql
-- Atomic claim — no two workers grab the same task
SELECT * FROM ops_db.tasks
WHERE status = 'queued' AND deps_satisfied(id)
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

Workers call `tasks.claim(agent_key)`, run their handler, then `tasks.complete(id, result)` or `tasks.fail(id, error)`.

Dependent tasks (e.g. reviewer waits for copywriter) see all ancestor results via a recursive CTE:
```sql
WITH RECURSIVE ancestors AS (
  SELECT parent_task_id FROM tasks WHERE id = $1
  UNION ALL
  SELECT t.parent_task_id FROM tasks t JOIN ancestors a ON t.id = a.parent_task_id
)
SELECT result FROM tasks WHERE id IN (SELECT parent_task_id FROM ancestors);
```

This means the reviewer genuinely reads the copywriter's text — not a stub.

## Agent roster

| Agent key | File | Role | Schedule |
|---|---|---|---|
| `orchestrator` | `orchestrator.py` | Main assistant (Emilia) — tools, dialog, Telegram | 24/7 |
| `chief_of_staff` | `chief_of_staff.py` | Digests, summaries | 9:00 + 19:00 |
| `email_watchdog` | `email_watchdog.py` | Filters incoming email | 08:00 |
| `knowledge_curator` | `knowledge_curator.py` | Maintains Qdrant knowledge base | 24/7 |
| `task_sync` | `task_sync.py` | Syncs tasks/KPIs from external tools | 10:00 |
| `calendar_agent` | `calendar_agent.py` | Calendar sync | 08:30 |
| `lead_manager` | `lead_manager.py` | CRM: leads, follow-ups, sales report | 10:00 + 11:00 |
| `email_agent` | `email_agent.py` | Sends outbound emails to leads | on demand |
| `support_agent` | `support_agent.py` | Customer support bot | 24/7 |

## Worker handlers (`worker_handlers.py`)

Six specialized handlers registered via `base_agent.register()`:

- `content_writer` — sales posts, emails, landing copy
- `content_designer` — visual brief + image-gen prompt (text only; real rendering needs ComfyUI/paid API)
- `content_reviewer` — verdict + edits; sees both text AND visual brief from ancestors
- `web_researcher` — structured research brief (works on model knowledge; live search needs a Search API)
- `dev_worker` — code + tests in text; no repo access yet
- `ops` → `lead_manager` (CRM operations)

## Content factory (`content_factory.py`)

```
brief + channel + kind
        ↓
content_writer (LLM)   →  text
        ↓
content_designer (LLM) →  visual_brief + image_prompt
        ↓
content_reviewer (LLM) →  verdict + edits
        ↓
status = "pending"  +  Telegram preview to owner
        ↓
owner clicks ✅ in dashboard
        ↓
publish → Telegram channel (if TELEGRAM_CHANNEL_ID set) or saved as "ready"
```

## Cost guard (`cost_guard.py`)

Monthly budget cap per agent. When `paid_api_spend_rub >= monthly_paid_cap_rub`:
- `guard_model(model, agent)` replaces paid models with `groq/llama-3.3-70b-versatile`
- Free/local models: no-op

## Tests

```bash
python -m pytest tests/ -q
```

`tests/test_libs.py` — unit tests: parse_json, count_tokens, cost_guard tiers, DB round-trips  
`tests/test_agents.py` — smoke imports all 9 agents; regression guards:
- no local `send_telegram` (must use `notify.send_msg`)
- no hardcoded PG password
- no `Langfuse()` constructor
- customer-domain agents connect to `customer_db`, not `agents`
- no dead `if False` blocks

Weekly auto-run via `ai.tests` launchd job (Sunday 06:00) → Telegram alert on failure only.
