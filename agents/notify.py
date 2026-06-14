#!/usr/bin/env python3
"""
notify — единая точка отправки уведомлений (Telegram) для всей инфры.

Используется и python-агентами (import notify; notify.send(...)),
и bash-скриптами (python3 notify.py "текст" [--level warn|crit|info]).

Читает TELEGRAM_BOT_TOKEN + TELEGRAM_MY_ID из ~/ai-infra/agents/.env.
Не падает, если Telegram недоступен — возвращает False.
"""
import os
import sys
import time
import json
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

ICONS = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "crit": "🚨"}


def send(text: str, level: str = "info") -> bool:
    """Отправить в Telegram. Длинный текст бьётся на части (лимит 4096). True если всё ушло."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_MY_ID")
    if not token or not chat_id:
        print(f"[notify] нет TELEGRAM_BOT_TOKEN/TELEGRAM_MY_ID — пропуск ({level}): {text[:80]}")
        return False
    prefix = ICONS.get(level, "")
    body = f"{prefix} {text}".strip()
    chunks = [body[i:i + 4000] for i in range(0, len(body), 4000)] or [body]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in chunks:
        data = json.dumps({"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}).encode()
        sent = False
        # Ретрай: транзиентные TLS-сбои к api.telegram.org (SSL: UNEXPECTED_EOF) частые
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
                sent = True
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    print(f"[notify] не удалось отправить часть после 3 попыток: {e}")
        if not sent:
            ok = False
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 notify.py 'message' [--level info|ok|warn|crit]")
        sys.exit(2)
    msg = sys.argv[1]
    level = "info"
    if "--level" in sys.argv:
        try:
            level = sys.argv[sys.argv.index("--level") + 1]
        except Exception:
            pass
    ok = send(msg, level)
    sys.exit(0 if ok else 1)
