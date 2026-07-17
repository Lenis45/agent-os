#!/usr/bin/env python3
"""Register native Telegram slash-command menus for Amori bots."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from bot_commands import ORCHESTRATOR_COMMANDS, SECRETARY_COMMANDS, SUPPORT_COMMANDS, to_bot_commands


async def set_commands(name: str, token_env: str, commands) -> None:
    token = os.getenv(token_env)
    if not token:
        print(f"{name}: skipped, {token_env} is missing")
        return
    bot = Bot(token=token)
    me = await bot.get_me()
    await bot.set_my_commands(to_bot_commands(commands))
    print(f"{name}: @{me.username} commands set ({len(commands)})")


async def main() -> int:
    load_dotenv(AGENTS_DIR / ".env")
    await set_commands("orchestrator", "ORCHESTRATOR_BOT_TOKEN", ORCHESTRATOR_COMMANDS)
    await set_commands("secretary", "TELEGRAM_BOT_TOKEN", SECRETARY_COMMANDS)
    await set_commands("support", "SUPPORT_BOT_TOKEN", SUPPORT_COMMANDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
