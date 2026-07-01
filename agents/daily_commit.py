#!/usr/bin/env python3
"""
daily_commit — «сетевой бэкап с changelog».

Раз в день зеркалит ИСХОДНИК системы (~/ai-infra, БЕЗ секретов/токенов/тяжёлого мусора)
в отдельный ПРИВАТНЫЙ git-репозиторий ~/ai-infra-backup → Lenis45/ai-infra-backup,
коммитит с человекочитаемым описанием сделанного за день (LLM-summary, Qwen→Groq фолбэк)
и пушит. По сути — ещё один бэкап, но онлайн и с комментариями «что сделали за день».

Почему зеркало, а не коммит в ~/ai-infra: репозиторий agent-os ПУБЛИЧНЫЙ — пушить туда
исходник инфры нельзя. Бэкап идёт в приватный репо и НЕ трогает agent-os.

Безопасность:
  - rsync с жёстким списком исключений (секреты, токены, сессии, .env, node_modules,
    venv, дампы, логи, third-party клоны FreeQwenApi/FreeGLMKimiAPI);
  - guard на секреты ПОСЛЕ стейджа: если что-то просочилось — коммит отменяется;
  - push без --force; при отказе уведомляем, историю не ломаем;
  - пустые коммиты пропускаются.

Запуск: `python3 daily_commit.py` (боевой) | `--dry-run` (показать changelog без commit/push).
"""
import os
import sys
import subprocess
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SRC = os.path.expanduser("~/ai-infra") + "/"
DEST = os.path.expanduser("~/ai-infra-backup")
REMOTE = "https://github.com/Lenis45/ai-infra-backup.git"

# Что НЕ копировать в бэкап (секреты, токены, тяжёлое, third-party).
RSYNC_EXCLUDES = [
    ".git", "*.env", ".env", ".env.*", "auth.json", "*.session",
    "credentials.json", "token.json", "*.pem", "*.key",
    "*.log", "*.log.*.gz", "__pycache__", "*.pyc", ".pytest_cache",
    ".venv", "venv", "node_modules", "dist", ".DS_Store",
    "backups/local", "*.sql.gz", "*.tar.gz",
    "FreeQwenApi", "FreeGLMKimiAPI", "session", "uploads",
]

# Backstop: имена, которые НИКОГДА не должны уехать в сеть.
SECRET_EXACT = {".env", "credentials.json", "token.json", "auth.json"}
SECRET_SUFFIX = (".session", ".pem", ".key", ".env")

DRY = "--dry-run" in sys.argv


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def ensure_dest():
    """Создать рабочую копию бэкапа и привязать приватный remote (идемпотентно)."""
    os.makedirs(DEST, exist_ok=True)
    if not os.path.isdir(os.path.join(DEST, ".git")):
        sh(["git", "init", "-b", "main"], DEST)
    if not sh(["git", "remote", "get-url", "origin"], DEST).stdout.strip():
        sh(["git", "remote", "add", "origin", REMOTE], DEST)


def mirror():
    args = ["rsync", "-a", "--delete"]
    for ex in RSYNC_EXCLUDES:
        args += ["--exclude", ex]
    args += [SRC, DEST + "/"]
    r = sh(args)
    if r.returncode != 0:
        raise RuntimeError("rsync упал: " + (r.stderr or "")[:200])


def is_secret(path: str) -> bool:
    base = os.path.basename(path)
    return base in SECRET_EXACT or base.endswith(SECRET_SUFFIX)


def make_changelog(stat: str, names: str) -> str:
    try:
        import llm
        prompt = (
            "Составь краткий changelog за день по изменениям — маркированный список из "
            "3-7 пунктов на русском, по сути, без воды. "
            f"Изменённые файлы:\n{names}\n\nСтатистика diff:\n{stat}"
        )
        out = llm.qwen_answer(
            prompt, system="Ты пишешь лаконичный changelog. Только список, без преамбулы.",
            agent_key="daily_commit", max_tokens=400,
        )
        out = (out or "").strip()
        if out and not llm._looks_garbled(out):
            return out
    except Exception as e:
        print(f"[daily_commit] LLM-summary недоступен: {e}")
    return "Изменения за день:\n" + stat.strip()


def main():
    try:
        ensure_dest()
        mirror()
    except Exception as e:
        print(f"[daily_commit] подготовка упала: {e}")
        _notify(f"🗂 Автобэкап: ошибка подготовки — {e}", warn=True)
        return

    sh(["git", "add", "-A"], DEST)

    # backstop-guard на секреты
    staged = sh(["git", "diff", "--cached", "--name-only"], DEST).stdout.splitlines()
    secrets = [f for f in staged if is_secret(f)]
    if secrets:
        sh(["git", "reset", "-q"], DEST)
        _finish({"ai-infra-backup": ("blocked", f"секреты в стейдже: {secrets[:3]}")})
        return

    if sh(["git", "diff", "--cached", "--quiet"], DEST).returncode == 0:
        _finish({"ai-infra-backup": ("skip", "нет изменений за день")})
        return

    stat = sh(["git", "diff", "--cached", "--stat"], DEST).stdout[-1800:]
    names = sh(["git", "diff", "--cached", "--name-status"], DEST).stdout[:2500]
    today = datetime.date.today().strftime("%d.%m.%Y")
    changelog = make_changelog(stat, names)
    msg = (f"chore(daily): автобэкап за {today}\n\n{changelog}\n\n"
           "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")

    if DRY:
        sh(["git", "reset", "-q"], DEST)
        print(f"\n===== DRY-RUN =====\n{msg}\n===================")
        print(f"(застейджено бы {len(staged)} файлов)")
        return

    c = sh(["git", "commit", "-m", msg], DEST)
    if c.returncode != 0:
        _finish({"ai-infra-backup": ("error", "commit: " + (c.stderr or c.stdout)[:160])})
        return
    p = sh(["git", "push", "-u", "origin", "main"], DEST)
    if p.returncode != 0:
        _finish({"ai-infra-backup": ("commit_no_push", (p.stderr or "")[:160])})
        return
    _finish({"ai-infra-backup": ("ok", changelog.replace("\n", " ")[:160])})


def _notify(text, warn=False):
    try:
        import notify
        notify.send(text, level="warn" if warn else "ok")
    except Exception:
        pass


def _finish(results: dict):
    bad = {"error", "blocked", "commit_no_push"}
    any_bad = any(st in bad for st, _ in results.values())
    did = any(st == "ok" for st, _ in results.values())
    report = "🗂 Ежедневный автобэкап\n" + "\n".join(
        f"{r}: {st} — {info}" for r, (st, info) in results.items())
    print(report)
    try:
        import ops_store
        ops_store.heartbeat("daily_commit", "warn" if any_bad else "ok",
                            {r: st for r, (st, _) in results.items()})
    except Exception:
        pass
    if did or any_bad:
        _notify(report, warn=any_bad)


if __name__ == "__main__":
    main()
