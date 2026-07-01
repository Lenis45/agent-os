#!/usr/bin/env python3
"""
freeglmkimi_watchdog — самовосстановление FreeGLMKimiAPI (GLM/Kimi-бэкенд Hermes).

В отличие от FreeQwenApi, этот прокси отдаёт ответы через прямой fetch к Z.ai/Kimi
(браузер нужен только на этапе авторизации), поэтому режима «headless завис на ответе»
нет — достаточно дешёвого health-чека. Если процесс упал/не отвечает на :9766/health —
рестартим через `launchctl kickstart`. Кулдаун, чтобы не зациклиться. Heartbeat
«freeglmkimi» в ops_db. Исходники FreeGLMKimiAPI не трогаем (third-party с .git).
"""
import os
import re
import sys
import time
import json
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ops_store  # noqa: E402
import notify     # noqa: E402

LABEL = "com.denis.freeglmkimi"
HEALTH_URL = "http://127.0.0.1:9766/health"
COOLDOWN_FILE = "/tmp/freeglmkimi_watchdog_cooldown"
COOLDOWN_MIN = 10


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=6) as r:
            return bool(json.load(r).get("ok"))
    except Exception:
        return False


def _proc_age_sec():
    """Возраст процесса FreeGLMKimiAPI (сек) по launchd PID, или None."""
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
    print(f"[freeglmkimi_watchdog] RESTART: {reason}")
    try:
        notify.send(f"🔄 FreeGLMKimiAPI перезапущен (Hermes/GLM-Kimi): {reason}", level="warn")
    except Exception:
        pass


def main():
    age = _proc_age_sec()
    if age is not None and age < 90:
        # процесс ещё прогревается после рестарта
        try:
            ops_store.heartbeat("freeglmkimi", "ok", {"action": "warming", "age": age})
        except Exception:
            pass
        print(f"[freeglmkimi_watchdog] warming (age={age}s)")
        return
    health = _health_ok()
    if not health and not _in_cooldown():
        _restart("health down")
        status = "restarted"
    elif not health:
        status = "down_cooldown"
    else:
        status = "ok"
    try:
        ops_store.heartbeat("freeglmkimi", "ok" if status == "ok" else "warn",
                            {"action": status, "health": health})
    except Exception:
        pass
    print(f"[freeglmkimi_watchdog] {status} (health={health})")


if __name__ == "__main__":
    main()
