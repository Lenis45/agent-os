#!/usr/bin/env python3
"""Read-only diagnostics for the Google Calendar agent."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
TOKEN_PATH = AGENTS / "token.json"
CREDENTIALS_PATH = AGENTS / "credentials.json"
CALENDAR_LOG = AGENTS / "calendar.log"
REQUIRED_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    detail: str


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        return completed.returncode, completed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)


def check_token(token_path: Path = TOKEN_PATH) -> list[Check]:
    if not token_path.exists():
        return [Check("FAIL", "OAuth token", "token.json отсутствует")]

    checks = []
    mode = stat.S_IMODE(token_path.stat().st_mode)
    checks.append(
        Check(
            "PASS" if mode == 0o600 else "FAIL",
            "Token permissions",
            f"mode={mode:04o}; требуется 0600",
        )
    )
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        checks.append(Check("FAIL", "OAuth token", f"не читается: {error}"))
        return checks

    checks.append(
        Check(
            "PASS" if payload.get("refresh_token") else "FAIL",
            "Refresh token",
            "present" if payload.get("refresh_token") else "missing",
        )
    )
    scopes = payload.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    if REQUIRED_SCOPE in scopes:
        checks.append(Check("PASS", "OAuth scope", "calendar.events"))
    elif "https://www.googleapis.com/auth/calendar" in scopes:
        checks.append(Check("WARN", "OAuth scope", "legacy full calendar scope; reauthorize"))
    else:
        checks.append(Check("FAIL", "OAuth scope", "calendar.events is missing"))
    return checks


def check_schedule(crontab_text: str | None = None, launchd_loaded: bool | None = None) -> Check:
    if crontab_text is None:
        code, crontab_text = _run(["crontab", "-l"])
        if code != 0 and "no crontab" not in crontab_text.lower():
            return Check("WARN", "Calendar schedule", f"crontab unavailable: {crontab_text[:100]}")
    cron_entries = [
        line.strip()
        for line in (crontab_text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "calendar_agent.py" in line
    ]

    if launchd_loaded is None:
        code, _ = _run(["launchctl", "print", f"gui/{os.getuid()}/ai.calendar-digest"])
        launchd_loaded = code == 0

    if launchd_loaded and cron_entries:
        return Check("FAIL", "Calendar schedule", "duplicate: launchd + crontab")
    if launchd_loaded:
        return Check("PASS", "Calendar schedule", "launchd only (08:00)")
    if cron_entries:
        return Check("WARN", "Calendar schedule", "legacy crontab only")
    return Check("FAIL", "Calendar schedule", "no active schedule")


def check_recent_invalid_grant(
    log_path: Path = CALENDAR_LOG,
    token_path: Path = TOKEN_PATH,
) -> Check:
    if not log_path.exists():
        return Check("WARN", "OAuth failures", "calendar.log отсутствует")
    try:
        with log_path.open("rb") as handle:
            handle.seek(max(0, log_path.stat().st_size - 256_000))
            text = handle.read().decode("utf-8", errors="ignore").lower()
    except OSError as error:
        return Check("WARN", "OAuth failures", f"log unreadable: {error}")
    historical_count = text.count("invalid_grant")
    token_updated = datetime.fromtimestamp(token_path.stat().st_mtime) if token_path.exists() else None
    current_count = 0
    for line in text.splitlines():
        if "invalid_grant" not in line:
            continue
        match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not match:
            continue
        if not token_updated:
            current_count += 1
            continue
        logged_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        if logged_at >= token_updated:
            current_count += 1
    level = "WARN" if current_count else "PASS"
    return Check(
        level,
        "OAuth failures",
        f"invalid_grant after current token: {current_count}; historical tail: {historical_count}",
    )


def main() -> int:
    checks = [
        Check(
            "PASS" if CREDENTIALS_PATH.exists() else "FAIL",
            "OAuth client",
            "credentials.json present" if CREDENTIALS_PATH.exists() else "credentials.json missing",
        ),
        *check_token(),
        check_schedule(),
        check_recent_invalid_grant(),
    ]
    print("Calendar doctor")
    print("=" * 72)
    for check in checks:
        print(f"{check.level:4}  {check.name:22} {check.detail}")
    totals = {level: sum(check.level == level for check in checks) for level in ("PASS", "WARN", "FAIL")}
    print("-" * 72)
    print(f"PASS={totals['PASS']} WARN={totals['WARN']} FAIL={totals['FAIL']}")
    return 1 if totals["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
