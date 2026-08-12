#!/usr/bin/env python3
"""Per-agent operational audit for the personal Amori AI-team."""
import os
import json
from datetime import datetime, timezone

import db
import agent_contracts


AGENTS = {
    "orchestrator": {"log": "orchestrator.log", "role": "CEO assistant/router", "kind": "longrun"},
    "chief_of_staff": {"log": "chief.log", "role": "Telegram digest", "kind": "scheduled"},
    "email_watchdog": {"log": "email.log", "role": "incoming email digest", "kind": "scheduled"},
    "calendar_agent": {"log": "calendar.log", "role": "calendar sync", "kind": "scheduled"},
    "knowledge_curator": {"log": "curator.log", "role": "Obsidian/memory curator", "kind": "longrun"},
    "support_agent": {"log": "support.log", "role": "customer support bot", "kind": "longrun"},
    "lead_manager": {"log": "leads.log", "role": "CRM/leads", "kind": "scheduled"},
    "email_agent": {"log": "email_agent.log", "role": "outbound emails", "kind": "on-demand"},
    "task_sync": {"log": "tasksync.log", "role": "WEEEK/Taiga reporting", "kind": "scheduled"},
    "infra_monitor": {"log": "monitor.log", "role": "infra health", "kind": "scheduled"},
    "worker_dispatch": {"log": "worker.log", "role": "AI-team queue dispatcher", "kind": "longrun"},
    "project_manager": {"log": None, "role": "project decomposition", "kind": "on-demand"},
    "content_writer": {"log": None, "role": "content worker", "kind": "queue-worker"},
    "content_designer": {"log": None, "role": "visual brief worker", "kind": "queue-worker"},
    "content_reviewer": {"log": None, "role": "content QA worker", "kind": "queue-worker"},
    "web_researcher": {"log": None, "role": "research worker", "kind": "queue-worker"},
    "dev_worker": {"log": None, "role": "dev worker", "kind": "queue-worker"},
    "content_factory": {"log": None, "role": "HITL content pipeline", "kind": "on-demand"},
}

REPORT_TRUST_CUTOFF = os.getenv("REPORT_TRUST_CUTOFF", "2026-08-05T00:00:00+03:00")
ERROR_MARKERS = ("Traceback", "CRITICAL", "invalid_grant", "AUTHENTICATIONFAILED", "Connection refused")
START_MARKERS = (
    "запущен", "запущен...", "Support Agent запущен", "worker dispatcher запущен",
    "[notify] sent", "[infra_monitor] всё ок", "✓ PASS",
)


def _rows(query, params=None):
    return db.query(query, params or (), dbname="ops_db")


def _recent_reports():
    rows = _rows(
        """
        SELECT agent, title, COALESCE(summary,''), COALESCE(body,''), ts
        FROM reports
        WHERE ts >= %s::timestamptz
        ORDER BY ts DESC, id DESC
        LIMIT 120
        """,
        (REPORT_TRUST_CUTOFF,),
    )
    by_agent = {}
    for agent, title, summary, body, ts in rows:
        text = "\n".join([str(title or ""), str(summary or ""), str(body or "")])
        by_agent.setdefault(agent, []).append({
            "title": title,
            "ts": ts,
            "issues": agent_contracts.output_issues(text),
            "sample": text.replace("\n", " ")[:220],
        })
    return by_agent


def _tasks():
    rows = _rows("SELECT assignee, status, count(*) FROM tasks GROUP BY assignee, status")
    out = {}
    for assignee, status, count in rows:
        out.setdefault(assignee or "unassigned", {})[status] = count
    return out


def _usage():
    rows = _rows("SELECT agent, count(*), max(ts) FROM llm_usage GROUP BY agent")
    return {agent: {"calls": count, "last": ts} for agent, count, ts in rows}


def _heartbeats():
    rows = _rows("SELECT component, status, last_seen FROM infra_heartbeats")
    return {component: {"status": status, "last_seen": last_seen} for component, status, last_seen in rows}


def _log_findings(log_name):
    if not log_name:
        return []
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_name)
    if not os.path.exists(path):
        return [f"log missing: {log_name}"]
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        tail = f.readlines()[-2000:]
    for idx in range(len(tail) - 1, -1, -1):
        if any(marker in tail[idx] for marker in START_MARKERS):
            tail = tail[idx:]
            break
    findings = []
    for marker in ERROR_MARKERS:
        if any(marker in line for line in tail):
            findings.append(marker)
    return findings


def audit():
    reports = _recent_reports()
    tasks = _tasks()
    usage = _usage()
    heartbeats = _heartbeats()
    rows = []
    for key, meta in AGENTS.items():
        rep = reports.get(key, [])
        report_issues = [issue for r in rep[:5] for issue in r["issues"]]
        log_issues = _log_findings(meta.get("log"))
        hb = heartbeats.get(key) or heartbeats.get(key.replace("_agent", "")) or {}
        status = "ok"
        if hb.get("status") in {"warn", "fail", "critical"}:
            status = "needs_attention"
        elif report_issues or any(x in log_issues for x in ("Traceback", "CRITICAL", "invalid_grant")):
            status = "needs_attention"
        elif log_issues and not (meta["kind"] == "on-demand" and log_issues == [f"log missing: {meta.get('log')}"]):
            status = "warn"
        rows.append({
            "agent": key,
            "role": meta["role"],
            "kind": meta["kind"],
            "status": status,
            "heartbeat": hb,
            "llm": usage.get(key, {}),
            "tasks": tasks.get(key, {}),
            "recent_reports": len(rep),
            "report_issues": sorted(set(report_issues)),
            "log_findings": sorted(set(log_issues)),
        })
    return rows


def print_markdown(rows):
    print(f"# Amori Agent Audit\n\nGenerated: {datetime.now(timezone.utc).isoformat()}\n")
    for r in rows:
        print(f"## {r['agent']} — {r['status']}")
        print(f"- Role: {r['role']} ({r['kind']})")
        if r["heartbeat"]:
            print(f"- Heartbeat: {r['heartbeat'].get('status')} at {r['heartbeat'].get('last_seen')}")
        if r["llm"]:
            print(f"- LLM: {r['llm'].get('calls')} calls, last {r['llm'].get('last')}")
        if r["tasks"]:
            print(f"- Tasks: {json.dumps(r['tasks'], ensure_ascii=False)}")
        if r["report_issues"]:
            print(f"- Report issues: {', '.join(r['report_issues'])}")
        if r["log_findings"]:
            print(f"- Log findings: {', '.join(r['log_findings'])}")
        if not r["heartbeat"] and not r["llm"] and not r["tasks"] and not r["recent_reports"]:
            print("- Activity: no recent DB activity; check schedule/on-demand usage.")
        print()


if __name__ == "__main__":
    print_markdown(audit())
