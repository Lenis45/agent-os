<div align="center">

# Amori AI Operating System

**Personal AI team for Denis Kolesnikov and the Amori pet-tech startup**

Local-first agent infrastructure for founder operations, SMM/content production,
CRM, support, knowledge management, monitoring, and backups.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-Qwen3_local-111111?logo=ollama&logoColor=white)
![LLM routing](https://img.shields.io/badge/LLM-local_%E2%86%92_Codex_%7C_Claude-1A73E8)
![Qdrant](https://img.shields.io/badge/Qdrant-vector_memory-DC244C)
![Telegram](https://img.shields.io/badge/Telegram-operator_UI-26A5E4?logo=telegram&logoColor=white)
![launchd](https://img.shields.io/badge/launchd-macOS-999999?logo=apple&logoColor=white)
![Tests](https://img.shields.io/badge/tests-182_passed-2EA043)

</div>

---

## Executive Summary

This repository is the local production infrastructure for **Denis Kolesnikov**,
founder/operator of **Amori**. Amori is a pet-tech project building a GPS collar
product for pet owners. The repo does not contain product firmware or customer
secrets; it contains the private operating system that helps run the business.

The system is deliberately not "just another AI chat". It is a set of agents,
databases, dashboards, schedules, approval gates, audit trails, and recovery
scripts that let a small founder-led startup operate with a lightweight AI team.

**Core principle:** agents prepare and analyze work; Denis approves irreversible
actions such as publishing or outbound communication.

---

## Current State Snapshot

Last verified: **2026-08-14** on the local Mac host. See the factual
[system baseline](docs/SYSTEM_BASELINE_2026-08-12.md) and the current
[automation and LLM efficiency plan](docs/AUTOMATION_AND_LLM_EFFICIENCY_PLAN_2026-08-13.md).

| Area | Current fact |
|---|---|
| Runtime | macOS launchd + Docker Compose |
| Containers | 5/5 up: Postgres, Qdrant, Redis, Langfuse, n8n |
| Dashboard | `http://localhost:8099` |
| Pixel office | `http://localhost:5070` |
| Agent dashboard status | 8/8 required agents available + 1 on-demand agent |
| Queue | No active queued or failed tasks at last check |
| Tests | `182 passed` through `make test` |
| Local models | `qwen3:1.7b` text/router, `qwen3-vl:2b` vision, `amori-hermes:4b` Hermes profile |
| Production LLM chain | local classifier/answer → Codex for code/actions → Claude for architecture/research |
| Paid API budget | 2500 RUB cap, current paid spend shown as 0.00 RUB |
| Security boundary | Core Docker loopback-only; remote dashboard API requires bearer auth |
| Restore test | Passing weekly; latest backup restored into a disposable Postgres container |

Known operator items are documented instead of hidden:

| Item | Why it matters | Current handling |
|---|---|---|
| Google Calendar token | `calendar_agent` cannot modify Calendar with expired OAuth | Agent fails closed; owner re-authorization is still required |
| Optional token APIs | Separate API billing is not desired | DeepSeek, Gemini and Groq fallback disabled by default |
| Optional web proxies | Qwen/GLM/Kimi processes could be up while real generation failed | Autostart disabled until re-auth plus successful smoke-test |
| Historical bad reports | Old June reports contain fake publication/product claims | New output contracts and tests block these patterns going forward |
| Telegram network noise | VPN/TLS can produce isolated handshake/EOF timeouts | Alert requires three consecutive transient failures; bots recover automatically |
| Disk headroom | Less than 10 GiB remains after local model setup | Warning is active; restore at least 15-20 GiB before adding models/images |

---

## What The System Actually Does

| Capability | Real behavior today |
|---|---|
| Founder assistant | Telegram bot "Emilia" routes requests, answers questions, invokes tools, handles voice/photo/document input |
| Chief of Staff | Reads recent Telegram context and creates morning/evening digests |
| Email triage | Reads configured IMAP accounts and sends a concise digest of important email |
| Calendar assistant | Detects meeting candidates; modifies Google Calendar only when OAuth works and data is precise |
| Knowledge capture | Saves owner notes to Obsidian and shared memory with path safety checks |
| CRM / leads | Stores leads in `customer_db`, optionally mirrors to WEEEK when credentials are valid |
| Support bot | Handles customer support tickets in a separate customer data contour |
| SMM/content factory | Generates copy, visual brief, editorial review, and waits for human approval |
| Project queue | Decomposes goals into tasks and runs workers through a DB-backed queue |
| Monitoring | Tracks containers, launchd agents, heartbeats, backups, disk pressure, provider health |
| Backup / restore | Backs up Postgres/Qdrant/code and verifies restore using a disposable DB |

What it intentionally does **not** claim:

- It does not automatically publish marketing content without approval.
- It does not fabricate product capabilities such as real-time GPS precision,
  health monitoring, geofences, or ready mobile apps.
- It does not let the browser choose tenant, approval actor, or Telegram target.
- It does not directly edit external code repositories from `dev_worker`; dev
  output is proposed code/tests unless a real tool applies it.

---

## System Architecture

```mermaid
flowchart TB
    Denis["Denis Kolesnikov<br/>Founder / operator"]

    subgraph Interfaces["Operator interfaces"]
        TG["Telegram bots<br/>Emilia, support, curator"]
        Dash["Dashboard<br/>localhost:8099"]
        Office["Pixel office<br/>localhost:5070"]
        MCP["MCP tools<br/>Codex / Claude / Hermes"]
    end

    subgraph Agents["Agent layer"]
        Orchestrator["orchestrator<br/>Emilia"]
        PM["project_manager"]
        Factory["content_factory"]
        Worker["worker_dispatch"]
        Scheduled["scheduled agents<br/>chief, email, calendar, task sync, monitor"]
        Customer["customer agents<br/>lead manager, email agent, support"]
    end

    subgraph Data["Data layer"]
        OpsDB[("ops_db<br/>projects, tasks, reports,<br/>content, heartbeats, usage")]
        CustomerDB[("customer_db<br/>leads, support tickets")]
        AgentsDB[("agents<br/>memory, team, conversations")]
        N8NDB[("n8n<br/>workflow state")]
        Qdrant[("Qdrant<br/>shared/project memory")]
        Obsidian["Obsidian vault<br/>Knowledge_base"]
    end

    subgraph External["External systems"]
        Router["amori-ai<br/>local complexity classifier"]
        Ollama["Ollama on Mac<br/>Qwen3 text + Qwen3-VL"]
        Codex["Codex CLI<br/>ChatGPT subscription"]
        Claude["Claude Code<br/>Claude subscription"]
        OptionalAPI["Optional token APIs<br/>disabled by default"]
        Telegram["Telegram API"]
        Google["Google Calendar"]
        Mail["IMAP / SMTP"]
        Weeek["WEEEK CRM/tasks"]
        Taiga["Taiga tasks"]
    end

    Denis --> TG
    Denis --> Dash
    Denis --> Office
    Denis --> MCP

    TG --> Orchestrator
    Dash --> Orchestrator
    MCP --> Orchestrator
    Orchestrator --> PM
    Orchestrator --> Factory
    PM --> OpsDB
    Factory --> OpsDB
    Worker --> OpsDB
    Worker --> Agents
    Scheduled --> OpsDB
    Customer --> CustomerDB
    Scheduled --> AgentsDB
    Scheduled --> Qdrant
    Scheduled --> Obsidian

    Orchestrator --> Router
    Agents --> Router
    Router --> Ollama
    Router --> Codex
    Router --> Claude
    Router -. explicit opt-in only .-> OptionalAPI
    Agents --> Ollama
    Customer --> Telegram
    Scheduled --> Google
    Scheduled --> Mail
    Customer --> Weeek
    Scheduled --> Taiga
```

---

## Data Contours

The system keeps operational, customer, and personal data separated because the
same local infrastructure handles both founder workflows and customer-facing
automation.

```mermaid
flowchart LR
    subgraph Personal["Personal / founder contour"]
        TelegramDM["Telegram conversations"]
        Calendar["Calendar candidates"]
        Notes["Obsidian notes"]
        Memory["shared_memory"]
    end

    subgraph Ops["Operational contour: ops_db"]
        Projects["projects"]
        Tasks["tasks"]
        Reports["reports"]
        Content["content_items"]
        Usage["llm_usage"]
        Heartbeats["infra_heartbeats"]
    end

    subgraph Customer["Customer contour: customer_db"]
        Leads["leads"]
        Tickets["support_tickets"]
        Messages["support_messages"]
    end

    Personal -->|summaries / derived notes| Ops
    Ops -->|approved work only| Customer
    Customer -->|support and CRM reports| Ops
```

### PostgreSQL databases

| DB | Purpose | Examples |
|---|---|---|
| `ops_db` | Operations and observability | `projects`, `tasks`, `reports`, `content_items`, `agent_registry`, `llm_usage`, `infra_runs`, `infra_heartbeats` |
| `customer_db` | Customer and lead data | `leads`, `support_tickets`, `support_messages`, `support_faq` |
| `agents` | Personal/team memory and historical agent tables | `team_members`, `known_entities`, `chief_digests` |
| `n8n` | Workflow engine state | n8n internal tables |

### Non-Postgres stores

| Store | Data |
|---|---|
| Qdrant | `shared_memory`, `project_knowledge` vectors |
| Obsidian vault | Founder notes, translated tasks, knowledge capture |
| Local files | launchd plists, Telegram sessions, logs, backups |
| External APIs | Telegram, Google Calendar, IMAP/SMTP, WEEEK, Taiga, LLM providers |

Secrets live in untracked env/session files. README files intentionally document
variable names and contours, not secret values.

---

## Agent Map

```mermaid
flowchart TB
    Emilia["Emilia<br/>orchestrator"]

    Emilia --> ContentLead["Content Lead"]
    ContentLead --> Writer["content_writer<br/>copy"]
    ContentLead --> Designer["content_designer<br/>visual brief"]
    ContentLead --> Reviewer["content_reviewer<br/>QA / brand / facts"]

    Emilia --> ResearchLead["Research Lead"]
    ResearchLead --> Researcher["web_researcher"]
    ResearchLead --> Curator["knowledge_curator"]

    Emilia --> SalesLead["Sales Lead"]
    SalesLead --> LeadManager["lead_manager"]
    SalesLead --> EmailAgent["email_agent"]
    SalesLead --> Support["support_agent"]

    Emilia --> DevLead["Dev Lead"]
    DevLead --> DevWorker["dev_worker"]

    Emilia --> OpsAssistants["Ops assistants"]
    OpsAssistants --> Chief["chief_of_staff"]
    OpsAssistants --> Watchdog["email_watchdog"]
    OpsAssistants --> Calendar["calendar_agent"]
    OpsAssistants --> TaskSync["task_sync"]
    OpsAssistants --> Monitor["infra_monitor"]
```

| Agent | Runtime | Real responsibility | Main data |
|---|---|---|---|
| `orchestrator` | 24/7 launchd | Founder Telegram assistant, routing, voice/photo/docs, tool execution | conversations, tools, reports |
| `chief_of_staff` | scheduled | Telegram digest, waiting replies, tasks, agreements | Telegram messages, `chief_digests` |
| `email_watchdog` | scheduled | IMAP triage and digest | email headers/body snippets |
| `calendar_agent` | cron | Calendar candidate detection and safe sync | Google Calendar, Telegram/email context |
| `knowledge_curator` | 24/7 launchd | Obsidian capture, task translation, memory | Obsidian, Qdrant, `known_entities` |
| `task_sync` | cron | WEEEK/Taiga KPI report | external task APIs, snapshots |
| `lead_manager` | cron/on-demand | CRM, follow-ups, lead report | `customer_db.leads`, WEEEK |
| `email_agent` | on-demand | Outbound lead email drafts/sends | SMTP, `customer_db.leads` |
| `support_agent` | 24/7 launchd | Customer support bot and escalation | `support_tickets`, Telegram |
| `content_factory` | on-demand | HITL SMM pipeline | `content_items`, Telegram |
| `worker_dispatch` | 24/7 launchd | Drains `ops_db.tasks` for worker agents | task queue |
| `infra_monitor` | scheduled | Health checks and alerting | Docker, launchd, backups, heartbeats |

---

## Project And Content Flow

```mermaid
sequenceDiagram
    participant D as Denis
    participant O as Emilia
    participant PM as project_manager
    participant Q as ops_db.tasks
    participant W as worker_dispatch
    participant R as reports
    participant CF as content_factory

    D->>O: goal / brief
    O->>PM: new_project(goal)
    PM->>Q: create tasks + deps
    W->>Q: atomic claim
    W->>W: run role-specific handler
    W->>Q: complete/fail task
    W->>R: write report
    D->>CF: create content brief
    CF->>CF: copy -> visual brief -> review
    CF->>R: pending preview
    D->>CF: approve or reject
    CF->>R: published only if real Telegram delivery succeeds
```

The content factory is intentionally conservative:

| State | Meaning |
|---|---|
| `pending` | AI draft exists, waiting for human review |
| `approved` | Human accepted it, but no real external delivery has happened yet |
| `ready` | External channel is not configured; publish manually or retry later |
| `published` | Telegram API actually accepted the send |
| `rejected` | Human rejected it |

If `TELEGRAM_CHANNEL_ID` is missing, content becomes `ready` for manual
publication. If a configured delivery attempt fails, content remains `approved`
for retry. The system no longer labels either case as published.

---

## Safety And Quality Contracts

The repo contains deterministic guardrails in addition to prompts.

| Contract | Why it exists | Where |
|---|---|---|
| Unsupported product claims detector | Blocks claims about real-time GPS, exact accuracy, health/activity monitoring, geofences, ready app, guarantees | `agents/llm.py`, `agents/agent_contracts.py` |
| External action detector | Flags outputs that say "published/sent/implemented" without a tool result | `agents/agent_contracts.py`, `agents/audit_agent_outputs.py` |
| HITL publishing | Prevents automatic channel posts without approval | `content_factory.py` |
| Safe calendar sync | No auto-add without exact date/time/reason; OAuth failure becomes warning | `calendar_agent.py` |
| Obsidian path guard | LLM cannot write outside the configured vault | `knowledge_curator.py` |
| CRM fail-soft | Local lead persists even if WEEEK is not configured or API fails | `lead_manager.py` |
| Memory fallback | Sentence-transformer import is opt-in; fallback vector keeps agents alive | `memory.py` |

Run audits:

```bash
cd ~/ai-infra/agents
/opt/anaconda3/bin/python3 audit_agents.py
/opt/anaconda3/bin/python3 audit_agent_outputs.py --limit 80
```

---

## Interfaces

| Interface | URL / command | Purpose |
|---|---|---|
| Dashboard | `http://localhost:8099` | Founder action center: decisions, projects, results, system health |
| Dashboard docs | `http://localhost:8099/docs` | Rendered `docs/HOW_IT_WORKS.md` |
| Pixel office alias | `http://localhost:8099/office` | Redirects to the canonical visual office |
| Pixel office | `http://localhost:5070` | Visual agent office; not the source of operational truth |
| SMM Factory | `http://localhost:8180` | Primary workspace for copy, images, approval, scheduling, and publishing |
| Telegram | private bots | Founder command surface and support |
| MCP | `mcp/` | Read-only analytics/tools for Codex, Claude, Hermes |

---

## Setup

```bash
git clone https://github.com/Lenis45/agent-os
cd agent-os

# 1. Infrastructure
docker compose up -d

# 2. Python deps (anaconda recommended)
pip install -r requirements.txt

# 3. MCP server deps
cd mcp && python -m venv .venv && .venv/bin/pip install "mcp[cli]" psycopg2-binary python-dotenv

# 4. Copy and fill env
cp agents/.env.example agents/.env   # add Telegram/integration secrets; LLM API keys are optional

# 5. Init databases
cd agents && python ops_store.py

# 6. Run tests
python -m pytest tests/ -q

# 7. Load launchd jobs (macOS)
# See docs/RUNBOOK.md for launchctl commands
```

---

## Operations

```bash
cd ~/ai-infra

# One-command operational checks
make doctor
make security-check
make test
make audit
make llm-report
make model-eval
make release-check

# Dashboard state
curl -s http://localhost:8099/api/state

# Restart long-running agents
launchctl kickstart -k gui/$(id -u)/ai.orchestrator
launchctl kickstart -k gui/$(id -u)/ai.worker
launchctl kickstart -k gui/$(id -u)/ai.dashboard
launchctl kickstart -k gui/$(id -u)/knowledge.curator
launchctl kickstart -k gui/$(id -u)/amori.support

# Backup and restore-test without touching production
make backup
make restore-test
```

Launchd labels:

```text
Always on:
  ai.orchestrator, ai.worker, ai.dashboard, ai.office,
  knowledge.curator, amori.support

Scheduled:
  chief.of.staff, email.watchdog, ai.monitor, ai.digest,
  amori.backup, ai.restoretest, ai.tests

Cron:
  task_sync, calendar_agent, lead_manager
```

---

## Repository Map

```text
~/ai-infra/
├── agents/              Python agents, shared libs, tests, audit tools
├── dashboard/           Local web dashboard on :8099
├── mcp/                 MCP tools for external coding assistants
├── docs/                HOW_IT_WORKS, RUNBOOK, INFRA, principles
├── backups/             Local/off-site backup scripts and restore tests
├── office-fork/         Private Pixel Office submodule
├── FreeQwenApi/         Private optional Qwen proxy submodule (disabled by default)
├── docker-compose.yml   Postgres, Qdrant, Redis, Langfuse, n8n
└── AGENTS.md            Coding-agent rules for this repository
```

Important docs:

- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) — Russian operator guide.
- [docs/SMART_MODEL_ROUTING_2026-08-14.md](docs/SMART_MODEL_ROUTING_2026-08-14.md) — local/subscription routing, privacy and verification.
- [docs/SYSTEM_BASELINE_2026-08-12.md](docs/SYSTEM_BASELINE_2026-08-12.md) — verified system inventory, boundaries, and open items.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — incident procedures.
- [docs/INFRA.md](docs/INFRA.md) — infrastructure inventory.
- [agents/README.md](agents/README.md) — agent runtime internals.

The public repository works without private submodules. Their source is kept in
private owner repositories because the customized office is part of the commercial
operating environment; production agents do not depend on either submodule.

---

## Roadmap: What Would Improve The System Next

The measured, acceptance-criteria-driven plan is maintained in
[docs/AUTOMATION_AND_LLM_EFFICIENCY_PLAN_2026-08-13.md](docs/AUTOMATION_AND_LLM_EFFICIENCY_PLAN_2026-08-13.md).

| Priority | Improvement | Reason |
|---|---|---|
| P0 | Refresh Google Calendar OAuth token | Restores external calendar writes |
| P0 | Add one Action Inbox and consolidated morning plan | Turns overlapping reports into tracked decisions |
| P0 | Close the calendar and lead follow-up loops | Verifies actions in external systems instead of only reporting them |
| P0 | Keep at least 15-20 GiB free on the Data volume | Prevents local services and backups from failing |
| P1 | Connect production traces and prompt versions to Langfuse | Makes model/prompt changes measurable and reversible |
| P1 | Replace hash-vector memory with tested multilingual embeddings | Makes retrieval semantic instead of approximate |
| P1 | Isolate production Python dependencies in a locked venv | Removes global conda package conflicts |
| P1 | Restrict or stop the separate `amori-local-*` mobile dev stack when idle | Removes 14 LAN-visible development ports |
| P1 | Add real image generation provider or ComfyUI bridge | Turns visual briefs into assets |
| P2 | Add search API for `web_researcher` | Makes research current instead of model-memory based |
| P2 | Add PR/code-apply tool for `dev_worker` | Moves dev worker from proposal mode to supervised execution |
| P2 | Move secrets to a proper secret backend | Reduces local env-file operational risk |

---

## About The Builder

Denis Kolesnikov is building Amori as a founder/operator, not as a large
department. This repo is the operational leverage layer: a local AI team that
keeps routine work moving while preserving human control over public,
customer-facing, and irreversible actions.

The most important design choice is restraint: the system should be useful
without pretending that an agent has done work that no tool actually performed.
