#!/usr/bin/env python3
"""Safely archive all current tasks from one exact WEEEK project."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
BACKUP_DIR = AGENTS / "private_backups"
API_BASE = "https://api.weeek.net/public/v1"
CONFIRM_PHRASE = "2026-09-05-weeek-reset"


@dataclass(frozen=True)
class Inventory:
    project_id: int | str
    project_title: str
    current: list[dict]
    deleted: list[dict]
    crm_contacts: int
    crm_organizations: int

    @property
    def completed_count(self) -> int:
        return sum(bool(task.get("isCompleted")) for task in self.current)

    @property
    def open_count(self) -> int:
        return len(self.current) - self.completed_count


class WeeekClient:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method,
            f"{API_BASE}/{path.lstrip('/')}",
            headers=self.headers,
            timeout=15,
            **kwargs,
        )
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError(f"WEEEK {method} {path} failed: HTTP {response.status_code}")
        return response

    def paged(self, path: str, key: str, *, params: dict | None = None) -> list[dict]:
        per_page = 100
        offset = 0
        items = []
        seen_ids = set()
        for _page in range(100):
            page_params = {**(params or {}), "perPage": per_page, "offset": offset}
            response = self.request("GET", path, params=page_params)
            page = response.json().get(key, [])
            for item in page:
                item_id = str(item.get("id", ""))
                if not item_id:
                    raise RuntimeError(f"WEEEK {path} returned an item without id")
                if item_id in seen_ids:
                    raise RuntimeError(f"WEEEK {path} returned duplicate id {item_id}")
                seen_ids.add(item_id)
                items.append(item)
            if len(page) < per_page:
                return items
            offset += per_page
        raise RuntimeError(f"WEEEK {path} pagination exceeded 100 pages")

    def exact_project(self, title: str) -> dict:
        projects = self.paged("tm/projects", "projects")
        matches = [project for project in projects if project.get("title") == title]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one WEEEK project named {title!r}, found {len(matches)}")
        return matches[0]

    def inventory(self, project_title: str) -> Inventory:
        project = self.exact_project(project_title)
        project_id = project.get("id")
        all_tasks = self.paged("tm/tasks", "tasks", params={"projectId": project_id, "all": 1})
        current = [task for task in all_tasks if not task.get("isDeleted")]
        deleted = [task for task in all_tasks if task.get("isDeleted")]
        contacts = self.paged("crm/contacts", "contacts")
        organizations = self.paged("crm/organizations", "organizations")
        return Inventory(
            project_id=project_id,
            project_title=project_title,
            current=current,
            deleted=deleted,
            crm_contacts=len(contacts),
            crm_organizations=len(organizations),
        )

    def complete_task(self, task_id: int | str) -> None:
        self.request("POST", f"tm/tasks/{task_id}/complete")

    def delete_task(self, task_id: int | str) -> None:
        self.request("DELETE", f"tm/tasks/{task_id}")


def save_manifest(inventory: Inventory, backup_dir: Path = BACKUP_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"weeek-tasks-before-reset-{timestamp}.json"
    payload: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": {"id": inventory.project_id, "title": inventory.project_title},
        "counts": {
            "current": len(inventory.current),
            "completed": inventory.completed_count,
            "open": inventory.open_count,
            "already_deleted": len(inventory.deleted),
            "crm_contacts": inventory.crm_contacts,
            "crm_organizations": inventory.crm_organizations,
        },
        "tasks": inventory.current,
    }
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return target


def print_inventory(inventory: Inventory, label: str) -> None:
    print(
        f"{label}: project={inventory.project_title!r} "
        f"current={len(inventory.current)} completed={inventory.completed_count} "
        f"open={inventory.open_count} already_deleted={len(inventory.deleted)} "
        f"crm_contacts={inventory.crm_contacts} crm_organizations={inventory.crm_organizations}"
    )


def reset_task_sync_baseline() -> None:
    sys.path.insert(0, str(AGENTS))
    import ops_store

    ops_store.init()
    started_at = datetime.now(timezone.utc).isoformat()
    ops_store.set_automation_state(
        "task_sync_baseline",
        {"started_at": started_at, "reason": "WEEEK task reset"},
    )
    ops_store.set_automation_state(
        "task_sync_digest",
        {"fingerprint": None, "reset_at": started_at},
    )


def execute_cleanup(client: WeeekClient, before: Inventory) -> Inventory:
    for task in before.current:
        if not task.get("isCompleted"):
            client.complete_task(task["id"])
    for task in before.current:
        client.delete_task(task["id"])

    after = client.inventory(before.project_title)
    if after.current:
        raise RuntimeError(f"WEEEK cleanup incomplete: {len(after.current)} current tasks remain")
    if after.crm_contacts != before.crm_contacts or after.crm_organizations != before.crm_organizations:
        raise RuntimeError("WEEEK CRM counts changed unexpectedly; inspect the workspace immediately")
    return after


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--project", default="Amori")
    parser.add_argument("--confirm-cleanup", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(AGENTS / ".env")
    token = (os.getenv("WEEEK_TOKEN") or "").strip()
    if not token:
        print("WEEEK_TOKEN is not configured", file=sys.stderr)
        return 2
    if args.execute and args.confirm_cleanup != CONFIRM_PHRASE:
        print(f"Execution requires --confirm-cleanup={CONFIRM_PHRASE}", file=sys.stderr)
        return 2

    client = WeeekClient(token)
    before = client.inventory(args.project)
    print_inventory(before, "BEFORE")
    if args.dry_run:
        print("DRY-RUN: no WEEEK data changed and no manifest written")
        return 0

    manifest = save_manifest(before)
    print(f"Manifest: {manifest} (mode 0600)")
    after = execute_cleanup(client, before)
    reset_task_sync_baseline()
    print_inventory(after, "AFTER")
    print(
        f"DONE: completed={before.open_count} moved_to_trash={len(before.current)} "
        "CRM unchanged; task_sync baseline reset"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
