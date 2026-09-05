#!/usr/bin/env python3
"""Remove only legacy calendar_agent.py entries from the current user crontab."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "agents" / "private_backups"


def without_calendar_entries(crontab: str) -> tuple[str, list[str]]:
    kept = []
    removed = []
    for line in crontab.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "calendar_agent.py" in line:
            removed.append(line)
        else:
            kept.append(line)
    output = "\n".join(kept)
    if crontab.endswith("\n") or output:
        output += "\n"
    return output, removed


def current_crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode and "no crontab" not in result.stderr.lower():
        raise RuntimeError(result.stderr.strip() or "crontab -l failed")
    return result.stdout


def install_crontab(content: str) -> None:
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def backup_crontab(content: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_DIR / f"crontab-before-calendar-dedup-{timestamp}.txt"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    before = current_crontab()
    after, removed = without_calendar_entries(before)
    print(f"Legacy calendar cron entries: {len(removed)}")
    if not removed:
        print("No change needed")
        return 0
    if not args.execute:
        print("DRY-RUN: rerun with --execute to remove only those entries")
        return 0

    backup = backup_crontab(before)
    install_crontab(after)
    verified, leftovers = without_calendar_entries(current_crontab())
    del verified
    if leftovers:
        raise RuntimeError("Calendar cron entry is still present after install")
    print(f"Removed {len(removed)} entry; backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
