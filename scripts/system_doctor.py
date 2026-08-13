#!/usr/bin/env python3
"""Read-only operational readiness check for the local Amori agent system."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
BACKUPS = ROOT / "backups" / "local"


@dataclass
class Check:
    level: str
    name: str
    detail: str


def command(args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def http_check(name: str, url: str, timeout: int = 8) -> Check:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "amori-doctor/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 400:
                return Check("PASS", name, f"HTTP {response.status}")
            return Check("FAIL", name, f"HTTP {response.status}")
    except Exception as exc:
        return Check("FAIL", name, str(exc)[:120])


def telegram_check(name: str, token: str) -> Check:
    if not token or "REPLACE" in token.upper():
        return Check("WARN", name, "token is not configured")
    last_error = "unknown error"
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            with urllib.request.urlopen(url, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and body.get("ok"):
                return Check("PASS", name, "getMe OK")
            last_error = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                return Check("FAIL", name, f"Telegram rejected credentials: HTTP {exc.code}")
            last_error = f"HTTP {exc.code}"
        except Exception as exc:
            last_error = str(exc).replace(token, "[redacted]")[:120]
        if attempt < 2:
            time.sleep(1)
    return Check("WARN", name, f"transient network failure after 3 attempts: {last_error}")


def docker_checks() -> list[Check]:
    required = {
        "ai_postgres": "database",
        "ai_redis": "queue/cache",
        "ai_qdrant": "vector store",
        "ai_langfuse": "observability",
        "ai_n8n": "automation",
    }
    code, output = command(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    if code != 0:
        return [Check("FAIL", "Docker", output or "docker ps failed")]
    running = {}
    for line in output.splitlines():
        name, _, status = line.partition("\t")
        running[name] = status
    checks = []
    for container, purpose in required.items():
        status = running.get(container)
        checks.append(
            Check("PASS" if status else "FAIL", f"Docker {container}", status or f"missing ({purpose})")
        )
    return checks


def launchd_checks() -> list[Check]:
    required = {
        "ai.orchestrator": "Emilia",
        "ai.worker": "worker queue",
        "amori.support": "support bot",
        "knowledge.curator": "knowledge curator",
        "ai.dashboard": "dashboard",
        "ai.office": "pixel office",
    }
    code, output = command(["launchctl", "list"])
    if code != 0:
        return [Check("FAIL", "launchd", output or "launchctl list failed")]
    rows = {}
    for line in output.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            rows[parts[2]] = (parts[0], parts[1])
    checks = []
    for label, purpose in required.items():
        pid, status = rows.get(label, ("-", "missing"))
        running = pid.isdigit()
        checks.append(Check("PASS" if running else "FAIL", f"launchd {label}", f"{purpose}; pid={pid}, status={status}"))
    return checks


def backup_check() -> Check:
    if not BACKUPS.exists():
        return Check("FAIL", "Backup", "local backup directory is missing")
    candidates = sorted(path for path in BACKUPS.iterdir() if path.is_dir())
    if not candidates:
        return Check("FAIL", "Backup", "no backups found")
    latest = candidates[-1]
    age_hours = (time.time() - latest.stat().st_mtime) / 3600
    required = {
        "pg_agents.sql.gz",
        "pg_ops_db.sql.gz",
        "pg_customer_db.sql.gz",
        "runtime_config.tar.gz",
        "components_code.tar.gz",
        "SHA256SUMS",
    }
    missing = sorted(name for name in required if not (latest / name).exists())
    if missing:
        return Check("FAIL", "Backup", f"{latest.name}: missing {', '.join(missing)}")
    level = "PASS" if age_hours <= 36 else "WARN"
    return Check(level, "Backup", f"{latest.name}; age={age_hours:.1f}h")


def disk_check() -> Check:
    usage = shutil.disk_usage(ROOT)
    free_gib = usage.free / (1024 ** 3)
    used_pct = usage.used / usage.total * 100
    level = "FAIL" if free_gib < 5 else "WARN" if free_gib < 15 else "PASS"
    return Check(level, "Disk", f"used={used_pct:.1f}%, free={free_gib:.1f} GiB")


def runtime_check() -> Check:
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        return Check("WARN", "Python runtime", "isolated .venv is absent")
    code, output = command(
        [str(python), "-c", "import praisonaiagents,mcp,requests,dotenv;print('imports OK')"],
        timeout=30,
    )
    return Check("PASS" if code == 0 else "FAIL", "Python runtime", output[:120])


def main() -> int:
    env = {**load_env(ROOT / ".env"), **load_env(AGENTS / ".env")}
    checks: list[Check] = []
    checks.extend(docker_checks())
    checks.extend(launchd_checks())
    checks.extend(
        [
            http_check("Dashboard", "http://127.0.0.1:8099/"),
            http_check("Pixel office", "http://127.0.0.1:5070/"),
            http_check("Langfuse", "http://127.0.0.1:3000/"),
            http_check("Qdrant", "http://127.0.0.1:6333/healthz"),
            http_check("n8n", "http://127.0.0.1:5678/healthz"),
            telegram_check("Telegram Emilia", env.get("TELEGRAM_BOT_TOKEN", "")),
            telegram_check("Telegram Support", env.get("SUPPORT_BOT_TOKEN", "")),
            runtime_check(),
            backup_check(),
            disk_check(),
        ]
    )

    print("Amori system doctor")
    print("=" * 72)
    for item in checks:
        print(f"{item.level:4}  {item.name:28} {item.detail}")
    totals = {level: sum(item.level == level for item in checks) for level in ("PASS", "WARN", "FAIL")}
    print("-" * 72)
    print(f"PASS={totals['PASS']} WARN={totals['WARN']} FAIL={totals['FAIL']}")
    return 1 if totals["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
