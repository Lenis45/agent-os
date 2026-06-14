#!/usr/bin/env python3
"""
freeqwen_watchdog — самовосстановление FreeQwenApi (Qwen-бэкенд Hermes).

FreeQwenApi гонит chat.qwen.ai через headless puppeteer, который на длинном прогоне
зависает (`ProtocolError: Runtime.callFunctionOn timed out`). PROTOCOL_TIMEOUT уже 5 мин —
помогает только рестарт процесса. Этот watchdog (launchd, каждые ~5 мин) детектит
зависание ДЁШЕВО (по логам/health, без трат запросов к Qwen) и рестартит через
`launchctl kickstart`. Исходники FreeQwenApi не трогаем (third-party с .git).

Детект зависания:
  1) GET /api/health не отвечает/не ok → Node-процесс завис → рестарт.
  2) последняя ошибка браузера НОВЕЕ последнего «Ответ получен успешно» и свежая
     (< STALE_MIN мин) → headless-браузер завис → рестарт.
Кулдаун COOLDOWN_MIN между рестартами, чтобы не зациклиться. Heartbeat «freeqwen» в ops_db.
"""
import os
import re
import sys
import time
import json
import subprocess
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ops_store  # noqa: E402
import notify     # noqa: E402

LABEL = "com.denis.freeqwenapi"
LOG = os.path.expanduser("~/ai-infra/FreeQwenApi/logs/combined.log")
HEALTH_URL = "http://127.0.0.1:3264/api/health"
COOLDOWN_FILE = "/tmp/freeqwen_watchdog_cooldown"
COOLDOWN_MIN = 10
STALE_MIN = 12
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SUCCESS = "Ответ получен успешно"
ERRORS = ("ProtocolError", "Runtime.callFunctionOn timed out", "Ошибка при отправке")


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=6) as r:
            return bool(json.load(r).get("ok"))
    except Exception:
        return False


def _last_ts(lines, needles):
    for ln in reversed(lines):
        if any(n in ln for n in needles):
            m = TS_RE.match(ln)
            if m:
                try:
                    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
    return None


def _browser_hung() -> bool:
    try:
        with open(LOG, "r", errors="ignore") as f:
            lines = f.readlines()[-400:]
    except Exception:
        return False
    last_err = _last_ts(lines, ERRORS)
    if not last_err:
        return False
    last_ok = _last_ts(lines, [SUCCESS])
    fresh = (datetime.now() - last_err) < timedelta(minutes=STALE_MIN)
    newer_than_ok = (last_ok is None) or (last_err > last_ok)
    return fresh and newer_than_ok


def _proc_age_sec():
    """Возраст процесса FreeQwenApi (сек) по launchd PID, или None."""
    try:
        out = subprocess.run(["launchctl", "list", LABEL],
                             capture_output=True, text=True).stdout
        m = re.search(r'"PID"\s*=\s*(\d+)', out)
        if not m:
            return None
        et = subprocess.run(["ps", "-o", "etimes=", "-p", m.group(1)],
                            capture_output=True, text=True).stdout.strip()
        return int(et) if et.isdigit() else None
    except Exception:
        return None


def _in_cooldown() -> bool:
    try:
        return (time.time() - os.path.getmtime(COOLDOWN_FILE)) < COOLDOWN_MIN * 60
    except Exception:
        return False


def _restart(reason: str):
    uid = os.getuid()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True)
    try:
        with open(COOLDOWN_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass
    print(f"[freeqwen_watchdog] RESTART: {reason}")
    try:
        notify.send(f"🔄 FreeQwenApi перезапущен (Hermes/Qwen завис): {reason}", level="warn")
    except Exception:
        pass


def main():
    age = _proc_age_sec()
    if age is not None and age < 90:
        # процесс ещё прогревается после рестарта — старая ошибка в логе не считается
        try:
            ops_store.heartbeat("freeqwen", "ok", {"action": "warming", "age": age})
        except Exception:
            pass
        print(f"[freeqwen_watchdog] warming (age={age}s)")
        return
    health = _health_ok()
    hung = (not health) or _browser_hung()
    if hung and not _in_cooldown():
        _restart("health down" if not health else "browser hung (ProtocolError)")
        status = "restarted"
    elif hung:
        status = "hung_cooldown"
    else:
        status = "ok"
    try:
        ops_store.heartbeat("freeqwen", "ok" if status == "ok" else "warn",
                            {"action": status, "health": health})
    except Exception:
        pass
    print(f"[freeqwen_watchdog] {status} (health={health})")


if __name__ == "__main__":
    main()
