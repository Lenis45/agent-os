#!/usr/bin/env python3
"""Refresh Google Calendar OAuth without replacing the old token on failure."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
sys.path.insert(0, str(AGENTS))

from calendar_auth import CALENDAR_SCOPES, atomic_write_token


CREDENTIALS_PATH = AGENTS / "credentials.json"
TOKEN_PATH = AGENTS / "token.json"
SCOPES = list(CALENDAR_SCOPES)


def _install_token(token_json: str, token_path: Path = TOKEN_PATH) -> None:
    atomic_write_token(token_json, token_path)


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print(f"Не найден {CREDENTIALS_PATH}. Нужен OAuth client типа Desktop app.", file=sys.stderr)
        return 2

    print("Откроется официальный экран Google. Войдите в нужный аккаунт и разрешите Calendar.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    credentials = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")

    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    service.events().list(
        calendarId="primary",
        timeMin=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        maxResults=1,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    _install_token(credentials.to_json())
    print(f"Готово: доступ проверен, новый токен установлен в {TOKEN_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
