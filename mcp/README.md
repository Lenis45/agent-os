# mcp/

A [FastMCP](https://github.com/jlowin/fastmcp) stdio server that exposes the AI team to external agents.
One server, three clients: **Claude Code**, **Codex**, **Hermes**.

## Why stdio

Stdio transport means the MCP server is a subprocess. It:
- Never opens a port
- Can't be reached from outside the machine
- Starts and stops with the AI client session

## The 11 tools

### Projects & tasks
| Tool | What it does |
|---|---|
| `new_project(goal)` | LLM decomposes goal → enqueues tasks for the team |
| `list_projects()` | Recent projects with status |
| `project_status(id)` | Tasks, results, progress for one project |
| `list_tasks(status?)` | Task queue — queued/running/done/failed |

### Content factory
| Tool | What it does |
|---|---|
| `create_content(brief, channel, kind)` | Runs the full pipeline: write → design brief → review → pending |
| `approve_content(id)` | Approve and publish a pending content item |
| `reject_content(id, reason)` | Reject with feedback |
| `list_content(status?)` | Content items by status |

### Ops
| Tool | What it does |
|---|---|
| `system_status()` | Full snapshot: agents, containers, DBs, queue depth, LLM spend |
| `recent_reports(n?)` | Latest N agent reports |
| `sql_read(db, query)` | Read-only SQL on `ops_db` or `customer_db` |

## Security on `sql_read`

Inputs from LLMs are untrusted. Guards applied:
```python
# Only SELECT or WITH
if not re.match(r"^\s*(SELECT|WITH)\b", query, re.I):
    raise ValueError("read-only: only SELECT/WITH allowed")

# Whitelist databases
if db not in {"ops_db", "customer_db"}:
    raise ValueError(f"unknown db: {db}")

# No DDL/DML hiding inside CTEs
for bad in (";", "DROP ", "DELETE ", "UPDATE ", "INSERT ", "TRUNCATE ", "ALTER "):
    if bad.upper() in query.upper():
        raise ValueError(f"disallowed keyword: {bad}")

# Auto-LIMIT 200, statement_timeout 8s
```

## Design: why subprocess for writes

Content creation and project management involve LLM calls (via litellm + Groq).
If the MCP server imported those, their `print()` statements would corrupt the JSON-RPC stream on stdout.

Solution: reads go directly via psycopg2; writes shell out to the existing Python CLIs:
```python
def _run(*args, timeout=200) -> str:
    r = subprocess.run(["/opt/anaconda3/bin/python3", *args],
                       cwd=AGENTS_DIR, capture_output=True, text=True, timeout=timeout)
    return r.stdout[-1500:]  # return last 1500 chars of output
```

## Connect from Claude Code

```bash
claude mcp add agent-os -s user -- ~/ai-infra/mcp/run.sh
```

Then in any Claude Code session:
```
use agent-os:system_status
use agent-os:new_project "write a product comparison between Amori v1 and competitors"
use agent-os:list_content pending
```

## Connect from Codex

```toml
# ~/.codex/config.toml
[mcp_servers.agent-os]
command = "/bin/bash"
args = ["-c", "~/ai-infra/mcp/run.sh"]
```

## Dependencies

```bash
cd mcp
python -m venv .venv
.venv/bin/pip install "mcp[cli]" psycopg2-binary python-dotenv
```

The venv is intentionally minimal — no litellm, no heavy ML deps, no print pollution.
