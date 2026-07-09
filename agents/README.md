# agents/

Python runtime for the personal Amori AI team.

This folder contains long-running Telegram agents, scheduled automation,
queue workers, shared safety contracts, database helpers, audit tools, and the
test suite that protects the system from regressions.

Verified: **2026-07-09** · tests: **90 passed**.

---

## Runtime Shape

```mermaid
flowchart TB
    subgraph Entrypoints["Entrypoints"]
        Orchestrator["orchestrator.py<br/>Emilia / Telegram"]
        Support["support_agent.py<br/>customer support"]
        Curator["knowledge_curator.py<br/>Obsidian + memory"]
        Worker["worker_dispatch.py<br/>task queue"]
        Cron["cron jobs<br/>task_sync, calendar, leads"]
    end

    subgraph Shared["Shared libraries"]
        DB["db.py / ops_store.py"]
        LLM["llm.py / router.py / cost_guard.py"]
        Contracts["agent_contracts.py"]
        Notify["notify.py"]
        Memory["memory.py"]
        Reports["report.py"]
    end

    subgraph Stores["Stores"]
        Ops[("ops_db")]
        Customer[("customer_db")]
        Agents[("agents")]
        Qdrant[("Qdrant")]
        Vault["Obsidian vault"]
    end

    Entrypoints --> Shared
    DB --> Ops
    DB --> Customer
    DB --> Agents
    Memory --> Qdrant
    Curator --> Vault
    Reports --> Ops
```

---

## Shared Libraries

| File | Responsibility |
|---|---|
| `db.py` | PostgreSQL connection helper for `agents`, `ops_db`, `customer_db`; env-only password; connect/query timeouts |
| `ops_store.py` | Schema/init and operational writes for `ops_db`: usage, runs, heartbeats, projects, tasks, reports |
| `llm.py` | LLM wrapper, provider fallback, Groq SDK wrapper, JSON parser, unsupported Amori claim detector |
| `router.py` | Per-agent model routing with UI overrides and Ollama -> Groq fallback |
| `cost_guard.py` | LLM usage accounting and paid API budget downgrade |
| `agent_contracts.py` | Shared deterministic output contracts: product claims, external action phrases, env checks |
| `notify.py` | Telegram notifications with chunking and retries |
| `retry.py` | `net_retry` and `safe` wrappers |
| `memory.py` | Qdrant + Postgres memory; sentence-transformer is opt-in, hash vector fallback keeps agents alive |
| `base_agent.py` | Queue-worker execution scaffold |
| `worker_handlers.py` | Role-specific handlers for content/research/dev/ops workers |
| `audit_agents.py` | Per-agent operational audit from logs, usage, tasks, reports, heartbeats |
| `audit_agent_outputs.py` | Recent report audit for risky claims and fake external actions |

---

## Model Routing

Default routing uses fast/free models where possible and falls back safely:

```python
ROUTING = {
    "chief_of_staff":     "groq/openai/gpt-oss-120b",
    "email_watchdog":     "groq/openai/gpt-oss-120b",
    "knowledge_curator":  "groq/openai/gpt-oss-120b",
    "context_translator": "groq/openai/gpt-oss-120b",
    "task_sync":          "ollama/gpt-oss:20b",
    "research_agent":     "ollama/gpt-oss:20b",
    "code_agent":         "ollama/qwen2.5-coder:7b",
    "content_agent":      "ollama/gpt-oss:20b",
    "analyst_agent":      "ollama/gpt-oss:20b",
}
```

Rules:

- UI overrides live in `ops_db.agent_config` and are cached for 30 seconds.
- If Ollama is unavailable, router returns `groq/openai/gpt-oss-120b`.
- Deprecated Groq `llama-3.3-70b-versatile` is normalized away.
- Paid model usage is guarded by `cost_guard.py`.

---

## Data Boundaries

| DB / store | Used by | Data |
|---|---|---|
| `ops_db` | most agents | projects, tasks, reports, content, audit, usage, heartbeats |
| `customer_db` | `lead_manager`, `email_agent`, `support_agent` | leads, support tickets, customer messages |
| `agents` | personal agents | team members, chief digests, known entities |
| Qdrant | memory agents | shared/project vector memory |
| Obsidian vault | `knowledge_curator` | owner notes and task translations |

Customer data does not belong in `ops_db` except derived operational metadata.

---

## Long-Running And Scheduled Agents

| Agent | File | Runtime | Purpose |
|---|---|---|---|
| `orchestrator` | `orchestrator.py` | launchd 24/7 | Main Telegram assistant, tools, voice/photo/document analysis |
| `support_agent` | `support_agent.py` | launchd 24/7 | Customer support bot, ticketing, escalation |
| `knowledge_curator` | `knowledge_curator.py` | launchd 24/7 | Obsidian capture, memory, task translation |
| `worker_dispatch` | `worker_dispatch.py` | launchd 24/7 | Drains `ops_db.tasks` for enabled workers |
| `chief_of_staff` | `chief_of_staff.py` | schedule | Telegram digest and waiting-reply analysis |
| `email_watchdog` | `email_watchdog.py` | schedule | IMAP digest |
| `calendar_agent` | `calendar_agent.py` | cron | Calendar sync; fails closed if Google OAuth is invalid |
| `task_sync` | `task_sync.py` | cron | WEEEK/Taiga task KPI report |
| `lead_manager` | `lead_manager.py` | cron/on-demand | CRM, follow-ups, lead report |
| `email_agent` | `email_agent.py` | on-demand | Outbound emails to leads |
| `infra_monitor` | `infra_monitor.py` | schedule | System health checks and alerting |
| `provider_health` | `provider_health.py` | schedule/on-demand | LLM provider checks |

---

## Queue Workers

`worker_dispatch.py` reads enabled workers from `ops_db.agent_registry`.

Specialized handlers:

| Worker | Handler | Contract |
|---|---|---|
| `content_writer` | `worker_handlers.content_writer` | Publish-ready copy, no unsupported Amori product claims |
| `content_designer` | `content_designer` | Visual brief and image prompt, no fake UI/product features |
| `content_reviewer` | `content_reviewer` | Verdict, issues, improved version, claims check |
| `web_researcher` | `web_researcher` | Structured research brief; current data must be verified externally |
| `dev_worker` | `dev_worker` | Proposed code/tests only; must not claim implementation without tool result |
| `lead_manager` | `ops_worker` | Operational plan/result; must not claim external action without integration result |

```mermaid
sequenceDiagram
    participant Q as ops_db.tasks
    participant D as worker_dispatch
    participant H as role handler
    participant R as ops_db.reports

    D->>Q: claim queued task<br/>FOR UPDATE SKIP LOCKED
    Q-->>D: task + dependency results
    D->>H: run handler(task)
    H-->>D: result
    D->>Q: complete / fail
    D->>R: report result or alert
```

---

## Content Factory Semantics

`content_factory.py` is a human-in-the-loop pipeline:

```text
brief
  -> content_writer
  -> content_designer
  -> content_reviewer
  -> content_items(status='pending')
  -> owner approval
  -> Telegram publish if configured
```

Status meanings:

| Status | Meaning |
|---|---|
| `pending` | AI generated content is waiting for review |
| `approved` | Human approved, but no real external delivery has happened |
| `published` | Telegram API accepted the send |
| `rejected` | Human rejected |

Missing `TELEGRAM_CHANNEL_ID` leaves content `approved`, not `published`.

---

## Safety Contracts

The system does not rely on prompt wording alone.

| Guard | File | Prevents |
|---|---|---|
| Product claim detection | `llm.py`, `agent_contracts.py` | real-time GPS, exact location/accuracy, geofences, health/activity monitoring, ready app, guarantees |
| External action detection | `agent_contracts.py` | fake "published/sent/implemented/tested" claims |
| Safe content fallback | `worker_handlers.py`, `email_agent.py`, `support_agent.py` | unsafe marketing copy reaching users |
| Obsidian path safety | `knowledge_curator.py` | writes outside vault |
| Calendar fail-closed | `calendar_agent.py` | adding guessed events or crashing on invalid OAuth |
| WEEEK fail-soft | `lead_manager.py` | external CRM failure breaking local lead storage |

---

## Tests And Audits

```bash
cd ~/ai-infra/agents
/opt/anaconda3/bin/python3 -m pytest tests -q
/opt/anaconda3/bin/python3 audit_agents.py
/opt/anaconda3/bin/python3 audit_agent_outputs.py --limit 80
```

Coverage includes:

- shared libs: JSON parsing, token estimates, cost guard, DB round-trips;
- agent smoke imports and regression guards;
- no hardcoded PG password / no Langfuse constructor regressions;
- customer agents use `customer_db`;
- unsupported product claims are detected;
- content factory does not fake publication;
- email fallback rewrites unsafe copy;
- Obsidian writes stay inside the vault.

---

## Current Known Operator Issues

| Issue | Impact | Fix |
|---|---|---|
| Google Calendar `invalid_grant` | Calendar sync cannot write events | Re-auth and refresh `agents/token.json` |
| One IMAP `AUTHENTICATIONFAILED` | One mailbox missing from digest | Generate new app password |
| Historical June reports contain bad claims | Audit flags old data | Mark or archive old reports after review |
| Telegram polling tracebacks | Log noise | Long-running services recover; improve log filtering/rotation later |
