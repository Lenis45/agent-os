<div align="center">

# 🏢 Personal AI Operating System

**9 autonomous agents · 5 departments · runs 24/7 on a Mac Mini**

A real, production AI agent team that operates a commercial IoT startup — not a demo, not a toy.
Built from scratch in Python, running under macOS launchd, talking to Groq and a local Ollama GPU node.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Groq](https://img.shields.io/badge/Groq-LLaMA_3.3_70B-F55036?logo=data:image/svg+xml;base64,PHN2Zy8+)
![Qdrant](https://img.shields.io/badge/Qdrant-vector_DB-DC244C)
![FastMCP](https://img.shields.io/badge/FastMCP-stdio-6B46C1)
![launchd](https://img.shields.io/badge/launchd-macOS-999999?logo=apple&logoColor=white)

</div>

---

## What this is

I needed a team to run [Amori](https://amori.online) — a GPS pet-collar startup.
Hiring people for every function isn't realistic early on, so I built one out of agents.

The system handles email, leads, customer support, content creation, task management, ops monitoring,
and knowledge curation. It generates sales content, reviews it, waits for my approval, then publishes.
It backs itself up nightly, tests itself weekly, and pages me on Telegram if something breaks.

**The rule:** agents do the work; I approve what ships.

---

## Architecture

```mermaid
flowchart TD
    You["👤 You\n(Telegram / Dashboard)"]

    subgraph Core["Core Layer"]
        Emilia["🎯 Emilia\nOrchestrator · 24/7"]
        PM["📋 Project Manager\ndecomposes goals → tasks"]
        CF["🏭 Content Factory\ntext → design brief → review"]
    end

    subgraph Queue["Task Queue (ops_db.tasks)"]
        Q[("atomic claim\nFOR UPDATE SKIP LOCKED")]
    end

    subgraph Workers["Workers — 5 Departments"]
        W1["✍️ Copywriter\ncontent_writer"]
        W2["🎨 Designer\ncontent_designer"]
        W3["✅ Reviewer\ncontent_reviewer"]
        W4["🔍 Researcher\nweb_researcher"]
        W5["💻 Dev Worker\ndev_worker"]
    end

    subgraph Scheduled["Scheduled Agents"]
        S1["📧 Email Watchdog\n8:00 daily"]
        S2["📅 Calendar Agent\n8:30 daily"]
        S3["📊 Lead Manager\n10:00 + 11:00"]
        S4["🧠 Chief of Staff\n9:00 + 19:00"]
        S5["📚 Knowledge Curator\n24/7"]
        S6["🛡️ Support Bot\n24/7"]
    end

    subgraph Ops["Ops Layer"]
        MON["🔍 Infra Monitor\nhourly alerts"]
        BAK["💾 Backup Job\n04:00 nightly"]
        TST["🧪 Test Runner\nSunday 06:00"]
    end

    You -->|goal / brief| Emilia
    Emilia --> PM
    Emilia --> CF
    PM --> Q
    Q -->|ai.worker dispatcher| Workers
    Workers -->|results cascade to dependents| Q
    Workers --> Reports["📊 Reports Hub\nops_db.reports"]
    CF -->|pending → your approval| You
    CF --> Reports

    style Core fill:#1e3a5f,color:#fff
    style Queue fill:#2d1b69,color:#fff
    style Workers fill:#1a3a2a,color:#fff
    style Scheduled fill:#3a1a1a,color:#fff
    style Ops fill:#2a2a1a,color:#fff
```

---

## How a project runs

```
You: "make 3 posts about the collar for Telegram"
       ↓
Emilia calls new_project("3 Telegram posts about the collar")
       ↓
project_manager decomposes → 3 parallel chains:
  [copywriter] → [designer] → [reviewer]   (×3 posts)
       ↓
ai.worker drains the queue atomically (FOR UPDATE SKIP LOCKED)
  reviewer gets the copywriter's text AND designer's visual brief
  (transitive ancestor results via recursive CTE)
       ↓
3 posts land in Dashboard → "🏭 Content Factory"
       ↓
You click ✅ Publish → goes to Telegram channel
```

Total time: ~2–3 minutes. You spent ~5 seconds.

---

## System map

| Layer | What | Where |
|---|---|---|
| **Agent runtime** | 9 Python agents, shared libs | [`agents/`](agents/) |
| **MCP server** | 11 tools for Claude / Codex / Hermes | [`mcp/`](mcp/) |
| **Ops dashboard** | Web UI at :8099, direct psycopg2, no docker exec | [`dashboard/`](dashboard/) |
| **Pixel office** | Live agent visualization at :5070 | [`office-fork/`](office-fork/) |
| **Infrastructure** | Docker Compose: Postgres, Qdrant, Redis, Langfuse, n8n | [`docker-compose.yml`](docker-compose.yml) |
| **Docs** | Architecture, runbook, principles | [`docs/`](docs/) |

---

## Stack

| Concern | Technology |
|---|---|
| Agent language | Python 3.12, litellm / Groq SDK, praisonai |
| LLM routing | Groq (LLaMA 3.3 70B) · local Ollama GPU node (fallback) |
| Primary DB | PostgreSQL 16 — `ops_db` (ops) · `customer_db` (CRM, 152-ФЗ) · `agents` (memory) |
| Vector memory | Qdrant — collections `project_knowledge`, `shared_memory` |
| Scheduling | macOS launchd (11 jobs) + crontab (3 jobs) |
| MCP transport | FastMCP stdio — connects to Claude Code, Codex, Hermes |
| Notifications | Telegram Bot API (chunked, 3× retry on SSL errors) |
| Observability | ops_db: `llm_usage`, `infra_runs`, `infra_heartbeats`, `task_events` |
| Backups | pg_dump × 4 DBs + Qdrant snapshots → GPG-encrypted off-site |
| Cost control | `cost_guard.py` — monthly budget cap, auto-downgrade to free tier |

---

## Databases

```
ai_postgres (Docker)
├── ops_db          ← task queue, projects, content, reports, LLM usage, heartbeats
├── customer_db     ← leads, support tickets (152-ФЗ boundary, separate from ops)
├── agents          ← conversation history, Langfuse telemetry
└── n8n             ← workflow engine
```

---

## Interfaces

| Interface | URL | Purpose |
|---|---|---|
| **Dashboard** | `:8099` | Main control panel — projects, content factory (with approval flow), kanban, reports, team hierarchy, model/budget toggles |
| **Ambient view** | `:8099/office` | Lightweight CSS "office" — department wings, project/content pulse, report feed |
| **Pixel office** | `:5070` | React+Canvas office — agents as pixel characters at desks, light up on activity |
| **Docs** | `:8099/docs` | HOW_IT_WORKS.md rendered in browser |

---

## Launchd jobs

```
ALWAYS ON          ai.orchestrator  ai.worker  amori.support  knowledge.curator  ai.dashboard  ai.office
SCHEDULED          amori.backup (04:00)  ai.monitor (hourly)  ai.digest (Mon 09:00)
                   chief.of.staff (9:00+19:00)  email.watchdog (08:00)
                   ai.restoretest (1st of month)  ai.tests (Sun 06:00)
CRONTAB            task_sync (10:00)  calendar_agent (08:30)  lead_manager (10:00 + 11:00)
```

---

## Setup

```bash
git clone https://github.com/Lenis45/agent-os
cd agent-os

# 1. Infrastructure
docker compose up -d

# 2. Python deps (anaconda recommended)
pip install litellm praisonai psycopg2-binary python-dotenv groq \
            python-telegram-bot qdrant-client tenacity

# 3. MCP server deps
cd mcp && python -m venv .venv && .venv/bin/pip install "mcp[cli]" psycopg2-binary python-dotenv

# 4. Copy and fill env
cp agents/.env.example agents/.env   # add your Groq key, Telegram token, etc.

# 5. Init databases
cd agents && python ops_store.py

# 6. Run tests
python -m pytest tests/ -q           # expect 72 tests passing

# 7. Load launchd jobs (macOS)
# See docs/RUNBOOK.md for launchctl commands
```

---

## Tests

```bash
cd ~/ai-infra/agents
python -m pytest tests/ -q
# 72 tests: shared libs (parse_json, cost_guard, DB round-trips) +
#           agent smoke imports + regression guards (no hardcoded PG pw, no Langfuse(), correct DB contours)
```

---

*See [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) for a full walkthrough in Russian.*
