# dashboard/

Local control panel for the personal Amori AI infrastructure.

The dashboard is intentionally simple: one Python HTTP server, PostgreSQL
connection pools, static HTML/CSS/JS, and no frontend build step. It is an
operator console for Denis, not a public SaaS UI.

Verified: **2026-08-05**.

---

## What It Shows

`http://localhost:8099`

| Section | Real data source | Purpose |
|---|---|---|
| Today | Product-health rules over all sources below | Blocking issues, overdue actions, available workflows |
| Work | `ops_db.projects`, `ops_db.tasks`, `ops_db.content_items` | Real project progress, queue, legacy content failures |
| Results | `ops_db.reports` | Recent agent outputs and alerts |
| System | launchd, Docker, DB queries, `infra_runs`, `infra_heartbeats` | Agents, containers, providers, backups, storage, spend |
| Lead summary | `customer_db.leads` | Total and overdue follow-ups without exposing contacts |
| SMM availability | `http://localhost:8180/health` | Whether the commercial editorial workflow can be opened |
| Docs | `docs/HOW_IT_WORKS.md` | Russian operator explanation rendered in browser |

Secondary views:

- `http://localhost:8099/office` — redirect to the canonical Pixel Office.
- `http://localhost:8099/docs` — rendered operator documentation.
- `http://localhost:5070` — separate pixel office visualization.

---

## Data Flow

```mermaid
flowchart LR
    Browser["Browser<br/>localhost:8099"] --> Server["dashboard/server.py"]
    Server --> Launchd["launchd<br/>process status"]
    Server --> Docker["Docker<br/>container status"]
    Server --> Ops[("ops_db<br/>tasks, reports, content,<br/>usage, runs, heartbeats")]
    Server --> Customer[("customer_db<br/>leads/support summary")]
    Server --> Docs["docs/HOW_IT_WORKS.md"]
    Server --> SMM["SMM Factory<br/>localhost:8180"]
    Server --> Office["Pixel Office<br/>localhost:5070"]
    Server --> CLI["project_manager.py / content_factory.py<br/>guarded legacy actions"]
    CLI --> Ops
```

Read-heavy requests go directly through PostgreSQL pools. New publications are
created only in SMM Factory; the personal dashboard links there instead of
duplicating its editor, scheduler, approval queue, and audit trail.

---

## Current Runtime Facts

The control panel currently knows about these dashboard-visible agents:

| Agent | Type | Contour | How status is determined |
|---|---|---|---|
| `orchestrator` | longrun | personal | launchd label `ai.orchestrator` |
| `support_agent` | longrun | customer | launchd label `amori.support` |
| `knowledge_curator` | longrun | personal | launchd label `knowledge.curator` |
| `chief_of_staff` | scheduled | personal | launchd label `chief.of.staff` |
| `email_watchdog` | scheduled | personal | launchd label `email.watchdog` |
| `task_sync` | cron | personal | cron metadata |
| `calendar_agent` | cron | personal | cron metadata |
| `lead_manager` | cron | customer | cron metadata |
| `email_agent` | on-demand | customer | registry/on-demand metadata |

The required-agent denominator excludes `email_agent`: it is intentionally
on-demand and is shown separately. A healthy runtime therefore reports `8/8`
required agents plus one on-demand agent, not a misleading `8/9` warning.

The overall product status is calculated from required agents, core containers,
available LLM routes, SMM Factory availability, operational heartbeats, failed
content generation, stale project state, and overdue lead follow-ups. It never
derives the green state from container uptime alone.

---

## Content Status Contract

The dashboard must not imply that approval equals publication.

```mermaid
stateDiagram-v2
    [*] --> failed: empty required artifact
    [*] --> pending: text and visual validated
    pending --> approved: Denis approves
    pending --> rejected: Denis rejects
    approved --> published: Telegram API success
    approved --> ready: missing channel
    approved --> approved: delivery error / retry
```

| Status | Meaning |
|---|---|
| `pending` | AI draft is waiting for review |
| `approved` | Human accepted it; external delivery may still be absent |
| `ready` | Channel is not configured; publish manually or retry after setup |
| `published` | Telegram API accepted the send |
| `rejected` | Human rejected the draft |
| `failed` | Required text or visual is missing; approval and publish are blocked |

The content UI should display external delivery failures as operator work, not
as silent success.

---

## Implementation Notes

Older versions called `docker exec psql` many times per `/api/state` request,
which made the page slow and fragile under simultaneous polling. The current
server uses `psycopg2.pool.ThreadedConnectionPool` directly against the host
Postgres port.

```python
_pools = {
    "ops_db": ThreadedConnectionPool(...),
    "customer_db": ThreadedConnectionPool(...),
}
```

Important behavior:

- All SQL writes use parameterized `%s` placeholders.
- `_jsonable()` converts `Decimal`, `date`, and `datetime` values for JSON.
- `/api/state` is cached briefly to reduce polling pressure.
- `POSTGRES_PASSWORD` is read from `~/ai-infra/agents/.env`; no password is
  committed to the repository.

---

## POST Endpoints

| Endpoint | Action | Safety note |
|---|---|---|
| `POST /api/project/new` | Runs `project_manager.py` | Creates queued internal tasks |
| `POST /api/content/new` | Runs `content_factory.py` | Creates `pending` content |
| `POST /api/content/approve` | Runs content approval/publish path | `published` only after real send |
| `POST /api/content/reject` | Rejects content | Keeps audit trail |
| `POST /api/agent/model` | Saves `ops_db.agent_config` override | Model choices are allowlisted in UI |
| `POST /api/budget` | Updates monthly paid-API cap | Used by cost guard |

---

## Launchd

```xml
<!-- ~/Library/LaunchAgents/ai.dashboard.plist -->
<key>KeepAlive</key><true/>
<key>ProgramArguments</key>
<array>
  <string>/opt/anaconda3/bin/python3</string>
  <string>/Users/denis/ai-infra/dashboard/server.py</string>
</array>
<key>EnvironmentVariables</key>
<dict>
  <key>INFRA_DASH_BIND</key><string>0.0.0.0</string>
  <key>DASH_TOKEN</key><string>set-a-random-local-token</string>
</dict>
```

For localhost-only use, keep `INFRA_DASH_BIND=127.0.0.1`. For Tailscale access
to `/office`, `/docs`, and `/api/state`, bind to `0.0.0.0` or the Tailscale IP
and keep `DASH_TOKEN` set so mutating POST endpoints stay protected.

Restart:

```bash
launchctl kickstart -k gui/$(id -u)/ai.dashboard
```

Port: `8099`.
