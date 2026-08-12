#!/usr/bin/env python3
"""Refresh Google Calendar OAuth without replacing the old token on failure."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
CREDENTIALS_PATH = AGENTS / "credentials.json"
TOKEN_PATH = AGENTS / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _install_token(token_json: str, token_path: Path = TOKEN_PATH) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_path.with_suffix(token_path.suffix + ".tmp")
    backup = token_path.with_suffix(token_path.suffix + ".bak")
    temporary.write_text(token_json, encoding="utf-8")
    os.chmod(temporary, 0o600)
    if token_path.exists():
        shutil.copy2(token_path, backup)
        os.chmod(backup, 0o600)
    os.replace(temporary, token_path)


def main() -> int:
    if not CREDENTIALS_PATH.exists():
        print(f"Не найден {CREDENTIALS_PATH}. Нужен OAuth client типа Desktop app.", file=sys.stderr)
        return 2

    print("Откроется официальный экран Google. Войдите в нужный аккаунт и разрешите Calendar.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    credentials = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")

    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    service.calendarList().list(maxResults=1).execute()
    _install_token(credentials.to_json())
    print(f"Готово: доступ проверен, новый токен установлен в {TOKEN_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
