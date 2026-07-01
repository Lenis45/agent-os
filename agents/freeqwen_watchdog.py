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
import urllib.error
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
# Анти-бот probe: /health бывает ok, а реальные completions Qwen блокирует анти-ботом.
# Шлём крошечный реальный запрос не чаще раза в PROBE_INTERVAL_MIN (бережём лимит),
# при анти-боте — уведомляем о ре-авторизации (не спамим, раз в ANTIBOT_NOTIFY_COOLDOWN_MIN).
CHAT_URL = "http://127.0.0.1:3264/api/chat/completions"
PROBE_INTERVAL_MIN = 30
PROBE_TS_FILE = "/tmp/freeqwen_probe_ts"
ANTIBOT_NOTIFY_COOLDOWN_MIN = 180
ANTIBOT_NOTIFY_FILE = "/tmp/freeqwen_antibot_notified"
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


def _rate_ok(path: str, minutes: int) -> bool:
    """True если с последней отметки в path прошло >= minutes (и обновляет отметку).
    False если ещё рано (отметку не трогаем)."""
    try:
        if (time.time() - os.path.getmtime(path)) < minutes * 60:
            return False
    except Exception:
        pass
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass
    return True


def _probe_antibot() -> str:
    """Реальный мини-запрос к Qwen (не чаще PROBE_INTERVAL_MIN).
    'ok' | 'antibot' | 'error' | 'skip' (ещё не время)."""
    if not _rate_ok(PROBE_TS_FILE, PROBE_INTERVAL_MIN):
        return "skip"
    body = json.dumps({
        "model": os.getenv("DEFAULT_MODEL", "qwen3.7-max"),
        "messages": [{"role": "user", "content": "1"}],
        "stream": False, "max_tokens": 4,
    }).encode()
    req = urllib.request.Request(CHAT_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer dummy"})
    try:
        with urllib.request.urlopen(req, timeout=50) as r:
            data = json.loads(r.read().decode())
        err = (data.get("error") or {}).get("message", "") if isinstance(data, dict) else ""
        if "anti-bot" in err.lower():
            return "antibot"
        return "ok" if data.get("choices") else ("error" if err else "ok")
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode()[:300]
        except Exception:
            txt = ""
        return "antibot" if "anti-bot" in txt.lower() else "error"
    except Exception:
        return "error"


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
        # /health ok — но проверим, что реальные ответы не блокирует анти-бот
        probe = _probe_antibot()
        if probe == "antibot":
            # Qwen выключен намеренно (мозг на DeepSeek) — фиксируем статус, НЕ уведомляем.
            status = "antibot"
        elif probe == "error" and not _in_cooldown():
            # реальный запрос упал не из-за анти-бота (краш/таймаут браузера:
            # TargetCloseError, ProtocolError) — это лечится рестартом
            _restart("probe error (browser crash)")
            status = "restarted"
        elif probe == "ok":
            # снова живой — сбросим флаг уведомления
            try:
                os.remove(ANTIBOT_NOTIFY_FILE)
            except OSError:
                pass
    try:
        ops_store.heartbeat("freeqwen", "ok" if status == "ok" else "warn",
                            {"action": status, "health": health})
    except Exception:
        pass
    print(f"[freeqwen_watchdog] {status} (health={health})")


if __name__ == "__main__":
    main()
