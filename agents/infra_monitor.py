#!/usr/bin/env python3
"""
infra_monitor — комплексный монитор AI-инфры (v3.0). Новый агент автоматизации.

Заменяет health_check.py (который не был в расписании и содержал мёртвый код).
Покрывает максимум сценариев и алертит в Telegram ТОЛЬКО при проблемах
(принцип «alert only if down»). Пишет heartbeat + историю в ops_db.

Режимы:
  python3 infra_monitor.py            # проверка, алерт при проблемах
  python3 infra_monitor.py --digest   # еженедельная сводка (шлёт всегда)

Проверяет: контейнеры, доступность БД/сервисов, живость агентов (launchd),
свежесть бэкапа + off-site, свободное место, раздувание Docker, размеры логов,
устаревшие heartbeat-и.
"""
import os
import sys
import json
import time
import subprocess
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

import notify
try:
    import ops_store
    OPS = True
except Exception:
    OPS = False

DOCKER = os.getenv("DOCKER_BIN", "/usr/local/bin/docker")
INFRA = os.path.dirname(HERE)
BACKUP_STATUS = os.path.join(INFRA, "backups", "local", "status.json")

CONTAINERS = ["ai_postgres", "ai_qdrant", "ai_redis", "ai_langfuse", "ai_n8n"]
HTTP_CHECKS = {
    "Langfuse": "http://localhost:3000",
    "Qdrant": "http://localhost:6333/",
    "n8n": "http://localhost:5678/healthz",
}
# label -> (тип, макс возраст лога в часах для cron-агентов)
AGENTS = {
    "ai.orchestrator":  ("longrun", None),
    "amori.support":    ("longrun", None),
    "knowledge.curator":("longrun", None),
    "chief.of.staff":   ("sched", 14),   # 9:00 и 19:00 → лог не старше ~14ч днём
    "email.watchdog":   ("sched", 26),   # 8:00 ежедневно
    "amori.backup":     ("sched", 26),   # 4:00 ежедневно
}
DISK_MIN_GB = int(os.getenv("DISK_MIN_GB", "5"))
DISK_WARN_GB = int(os.getenv("DISK_WARN_GB", "10"))
LOG_WARN_MB = int(os.getenv("LOG_WARN_MB", "20"))
DOCKER_CACHE_WARN_GB = int(os.getenv("DOCKER_CACHE_WARN_GB", "15"))
BACKUP_MAX_AGE_H = int(os.getenv("BACKUP_MAX_AGE_H", "26"))

crit, warn, ok = [], [], []


def sh(args, timeout=15):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def container_running(name):
    out, rc = sh([DOCKER, "inspect", "--format", "{{.State.Status}}", name])
    return rc == 0 and out == "running"


def http_ok(url):
    try:
        urllib.request.urlopen(urllib.request.Request(url), timeout=6)
        return True
    except Exception:
        return False


def launchd_state(label):
    """(loaded, pid|None)"""
    out, rc = sh(["launchctl", "list", label])
    if rc != 0:
        return False, None
    pid = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith('"PID"') or s.startswith("PID"):
            digits = "".join(ch for ch in s if ch.isdigit())
            pid = int(digits) if digits else None
    return True, pid


def db_ok(dbname):
    try:
        import psycopg2
        conn = psycopg2.connect(
            host="127.0.0.1", port=5432, database=dbname, user="agent_user",
            password=os.getenv("POSTGRES_PASSWORD", ""),
        )
        cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); conn.close()
        return True
    except Exception:
        return False


def log_age_hours(label):
    """Возраст самого свежего лога агента (по имени) в часах, или None."""
    base = label.split(".")
    candidates = [
        "orchestrator.log" if "orchestrator" in label else None,
        "support.log" if "support" in label else None,
        "curator.log" if "curator" in label else None,
        "chief.log" if "chief" in label else None,
        "email.log" if "email" in label else None,
        os.path.join(INFRA, "backups", "backup.log") if "backup" in label else None,
    ]
    newest = None
    for c in candidates:
        if not c:
            continue
        p = c if os.path.isabs(c) else os.path.join(HERE, c)
        if os.path.exists(p):
            age = (time.time() - os.path.getmtime(p)) / 3600.0
            newest = age if newest is None else min(newest, age)
    return newest


def check_containers():
    for c in CONTAINERS:
        if container_running(c):
            ok.append(f"container {c}")
        else:
            crit.append(f"❌ контейнер {c} не запущен")


def check_services():
    for name, url in HTTP_CHECKS.items():
        (ok.append(f"http {name}") if http_ok(url) else crit.append(f"❌ {name} недоступен ({url})"))
    for db in ("agents", "ops_db"):
        (ok.append(f"db {db}") if db_ok(db) else crit.append(f"❌ БД {db} недоступна"))


def check_agents():
    for label, (kind, max_age) in AGENTS.items():
        loaded, pid = launchd_state(label)
        if not loaded:
            crit.append(f"❌ агент {label} не загружен в launchd")
            continue
        if kind == "longrun" and not pid:
            warn.append(f"⚠️ агент {label} загружен, но без PID (не выполняется)")
        if kind == "sched" and max_age:
            age = log_age_hours(label)
            if age is not None and age > max_age:
                warn.append(f"⚠️ агент {label} молчит {age:.0f}ч (ожидалось <{max_age}ч)")
        ok.append(f"agent {label}")


def check_backup():
    if not os.path.exists(BACKUP_STATUS):
        warn.append("⚠️ нет status.json бэкапа — ни разу не отработал?")
        return
    try:
        st = json.load(open(BACKUP_STATUS))
    except Exception:
        warn.append("⚠️ status.json бэкапа нечитаем"); return
    age_h = (time.time() - os.path.getmtime(BACKUP_STATUS)) / 3600.0
    if age_h > BACKUP_MAX_AGE_H:
        crit.append(f"❌ бэкап устарел: {age_h:.0f}ч назад (порог {BACKUP_MAX_AGE_H}ч)")
    if st.get("offsite", "none") == "none":
        warn.append("⚠️ бэкап без off-site — только внутренний SSD")
    if "PARTIAL" in st.get("status", "") or "FAIL" in st.get("status", ""):
        warn.append(f"⚠️ последний бэкап: {st.get('status')} ({st.get('warnings','')})")
    ok.append(f"backup {age_h:.0f}h offsite={st.get('offsite')}")


def check_disk():
    # macOS: df / = запечатанный системный том; реальные данные на Data-томе ($HOME)
    out, _ = sh(["df", "-g", os.path.expanduser("~")])
    try:
        free = int(out.splitlines()[1].split()[3])
        if free < DISK_MIN_GB:
            crit.append(f"❌ критически мало места: {free}GB (порог {DISK_MIN_GB}GB)")
        elif free < DISK_WARN_GB:
            warn.append(f"⚠️ места мало: {free}GB свободно (комфортно от {DISK_WARN_GB}GB)")
        else:
            ok.append(f"disk {free}GB")
    except Exception:
        pass
    # внешний диск для off-site
    ext = [v for v in os.listdir("/Volumes") if v != "Macintosh HD"]
    if not ext:
        warn.append("⚠️ внешний диск не подключён — off-site бэкап невозможен")


def check_docker_bloat():
    out, _ = sh([DOCKER, "system", "df", "--format", "{{.Type}} {{.Reclaimable}}"])
    for line in out.splitlines():
        if "Build Cache" in line or "Images" in line:
            # извлекаем число GB перед 'GB'
            import re
            m = re.search(r"([\d.]+)GB", line)
            if m and float(m.group(1)) > DOCKER_CACHE_WARN_GB:
                warn.append(f"⚠️ Docker {line.split()[0]} раздут: {m.group(1)}GB reclaimable (docker system prune)")


def check_logs():
    for f in os.listdir(HERE):
        if f.endswith(".log"):
            mb = os.path.getsize(os.path.join(HERE, f)) / 1048576.0
            if mb > LOG_WARN_MB:
                warn.append(f"⚠️ лог {f} = {mb:.0f}MB (ротация >10MB в backup.sh)")


def run_check():
    check_containers(); check_services(); check_agents()
    check_backup(); check_disk(); check_docker_bloat(); check_logs()

    now = datetime.now().strftime("%d.%m %H:%M")
    status = "ok" if not crit and not warn else ("fail" if crit else "warn")

    if crit or warn:
        msg = f"Infra Monitor | {now}\n"
        if crit:
            msg += "\nКРИТИЧНО:\n" + "\n".join(crit)
        if warn:
            msg += "\n\nВнимание:\n" + "\n".join(warn)
        msg += f"\n\nОК: {len(ok)} проверок"
        notify.send(msg, "crit" if crit else "warn")
        print(f"[infra_monitor] {status}: crit={len(crit)} warn={len(warn)} ok={len(ok)}")
    else:
        print(f"[infra_monitor] всё ок ({len(ok)} проверок), алерт не нужен")

    if OPS:
        try:
            ops_store.heartbeat("infra_monitor", status,
                                {"crit": len(crit), "warn": len(warn), "ok": len(ok)})
            ops_store.record_run("monitor", status,
                                 {"crit": crit, "warn": warn, "ok_count": len(ok)})
        except Exception as e:
            print(f"[infra_monitor] ops_db запись не удалась: {e}")
    return 0 if not crit else 1


def run_digest():
    """Еженедельная сводка — шлём всегда."""
    run_check()  # наполнит crit/warn/ok и запишет heartbeat
    lines = [f"📊 Еженедельная сводка инфры | {datetime.now().strftime('%d.%m %H:%M')}"]
    lines.append(f"Сервисы: {len(ok)} ок, {len(warn)} предупр., {len(crit)} критично")
    if OPS:
        try:
            import cost_guard, tier1_log
            spent = cost_guard.month_spend_rub(paid_only=True)
            remain = cost_guard.remaining_paid_rub()
            t1 = tier1_log.stats(7)
            lines.append(f"💸 Платный API за месяц: {spent:.0f}₽ (осталось {remain:.0f}₽)")
            lines.append(f"🧠 Tier-1 сессий за 7д: {t1['total']} (applied {t1['applied']})")
        except Exception:
            pass
    try:
        st = json.load(open(BACKUP_STATUS))
        lines.append(f"💾 Бэкап: {st.get('status')} · off-site={st.get('offsite')} · диск {st.get('free_gb')}GB")
    except Exception:
        lines.append("💾 Бэкап: статус неизвестен")
    notify.send("\n".join(lines), "info")
    print("[infra_monitor] дайджест отправлен")
    return 0


if __name__ == "__main__":
    if "--digest" in sys.argv:
        sys.exit(run_digest())
    sys.exit(run_check())
