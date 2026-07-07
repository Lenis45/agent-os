#!/usr/bin/env python3
"""Audit recent AI-team reports for unsafe claims and fake external actions."""
import argparse

import db
import agent_contracts


def _hits(text: str) -> list[str]:
    return agent_contracts.output_issues(text)


def audit(limit: int = 50) -> list[dict]:
    rows = db.query(
        """
        SELECT id, agent, kind, title, COALESCE(summary,''), COALESCE(body,''),
               to_char(ts,'YYYY-MM-DD HH24:MI')
        FROM reports
        ORDER BY ts DESC, id DESC
        LIMIT %s
        """,
        (limit,),
        dbname="ops_db",
    )
    findings = []
    for rid, agent, kind, title, summary, body, ts in rows:
        text = "\n".join([str(title or ""), str(summary or ""), str(body or "")])
        issues = _hits(text)
        if issues:
            findings.append({
                "id": rid,
                "ts": ts,
                "agent": agent,
                "kind": kind,
                "title": title,
                "issues": issues,
                "sample": text.replace("\n", " ")[:280],
            })
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    findings = audit(args.limit)
    if not findings:
        print(f"OK: последние {args.limit} отчётов без известных риск-паттернов")
        return
    print(f"Найдено рискованных отчётов: {len(findings)} / {args.limit}")
    for f in findings:
        print(f"- #{f['id']} {f['ts']} {f['agent']} [{', '.join(f['issues'])}] {f['title']}")
        print(f"  {f['sample']}")


if __name__ == "__main__":
    main()
