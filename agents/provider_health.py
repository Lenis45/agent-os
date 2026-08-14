#!/usr/bin/env python3
"""
provider_health — ежедневный отчёт «на чём работает система».

Раз в сутки пробивает каждый LLM-провайдер/систему, на которых может работать инфра,
и шлёт в Telegram: кто 🟢 ok / 🔴 не работает / ⚪ не настроен, ЧТО СДЕЛАТЬ для починки,
и список «что ещё можно подключить». Пишет heartbeats в ops_db (видно в «проверь агентов»).

Запуск: `python3 provider_health.py` (разово) / launchd раз в сутки.
"""
import os
import sys
import socket
import json
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

import notify      # noqa: E402
import ops_store   # noqa: E402


def _request(method, url, headers, timeout, body=None, attempts=3):
    import requests
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, url, headers=headers, json=body, timeout=timeout)
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
        time.sleep(1)
    if last_error:
        raise last_error
    raise RuntimeError(f"{method} {url}: retry loop ended without response")


def _post(url, headers, body, timeout):
    return _request("POST", url, headers, timeout, body=body)


def _get(url, headers, timeout):
    return _request("GET", url, headers, timeout)


def _tcp_probe(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False, "непонятный OLLAMA_API_BASE"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "tcp ok"
    except socket.timeout:
        return False, "timeout"
    except OSError as e:
        return False, str(e)[:80]


# ── проверки. каждая → (icon, status_text, fix_action) ──

def check_deepseek():
    key = os.getenv("OPENMODEL_API_KEY")
    base = os.getenv("OPENMODEL_API_BASE", "https://api.openmodel.ai").rstrip("/")
    mdl = os.getenv("OPENMODEL_MODEL", "deepseek-v4-flash")
    enabled = os.getenv("OPENMODEL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return ("⏸", "отключён: закончился кредит", "включи OPENMODEL_ENABLED=1 после пополнения OpenModel")
    if not key:
        return ("⚪", "не настроен", "добавь OPENMODEL_API_KEY в agents/.env")
    try:
        r = _post(base + "/v1/messages",
                  {"Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"},
                  {"model": mdl, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}, 25)
        if r.status_code == 200 and r.json().get("content"):
            return ("🟢", "ok", "")
        return ("🔴", f"HTTP {r.status_code}", "проверь ключ/лимит (10 RPM); промо-кредит мог кончиться")
    except Exception as e:
        return ("🔴", str(e)[:45], "сеть/SSL до api.openmodel.ai")


def check_groq():
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return ("⚪", "не настроен", "добавь GROQ_API_KEY в agents/.env")
    try:
        r = _get("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"}, 10)
        return ("🟢", "ok", "") if r.status_code == 200 else ("🔴", f"HTTP {r.status_code}", "проверь ключ/лимит Groq")
    except Exception as e:
        return ("🔴", str(e)[:45], "сеть до api.groq.com")


def check_qwen():
    try:
        r = _post("http://127.0.0.1:3264/api/chat/completions", {"Content-Type": "application/json"},
                  {"model": "qwen3.7-max", "messages": [{"role": "user", "content": "hi"}],
                   "stream": False, "max_tokens": 5}, 35)
        if r.status_code == 200 and r.json().get("choices"):
            return ("🟢", "ok", "")
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = r.text[:60]
        anti = "anti-bot" in msg.lower()
        return ("🔴", "анти-бот" if anti else f"HTTP {r.status_code}",
                "ре-авторизация: cd ~/ai-infra/FreeQwenApi && node scripts/auth.js · стабильно — офиц. DashScope API")
    except Exception as e:
        return ("🔴", str(e)[:40], "проверь сервис :3264 (launchctl list | grep freeqwen) + ре-авторизацию")


def check_glm_kimi():
    """Два под-провайдера на :9766 (OpenAI-совместимо /v1/chat/completions)."""
    out = {}
    for mdl, name in [("glm-5", "GLM (Z.ai)"), ("kimi-k2.5", "Kimi")]:
        try:
            r = _post("http://127.0.0.1:9766/v1/chat/completions",
                      {"Content-Type": "application/json", "Authorization": "Bearer x"},
                      {"model": mdl, "messages": [{"role": "user", "content": "hi"}],
                       "stream": False, "max_tokens": 5}, 35)
            if r.status_code == 200 and r.json().get("choices"):
                out[name] = ("🟢", "ok", "")
                continue
            t = r.text.lower()
            if "account" in t and ("no " in t or "configured" in t):
                fix = ("GLM: npm run auth:browser -- ./auth.json" if mdl.startswith("glm")
                       else "Kimi: токен с kimi.com → admin API /admin/accounts")
                out[name] = ("⚪", "нет аккаунта", fix)
            else:
                out[name] = ("🔴", f"HTTP {r.status_code}",
                             "провайдер режет запрос (прокси устарел) · стабильно — офиц. API")
        except Exception as e:
            out[name] = ("🔴", str(e)[:40], "проверь сервис :9766 (launchctl list | grep freeglmkimi)")
    return out


def check_gemini():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return ("⚪", "не настроен", "добавь GEMINI_API_KEY (есть бесплатный тир)")
    model = os.getenv("GEMINI_TEXT_MODEL", "gemini/gemini-3.6-flash").removeprefix("gemini/")
    try:
        r = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            {"Content-Type": "application/json"},
            {
                "contents": [{"role": "user", "parts": [{"text": "Reply OK"}]}],
                "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
            },
            20,
        )
        if r.status_code == 200 and r.json().get("candidates"):
            return ("🟢", f"ok ({model})", "")
        return ("🔴", f"HTTP {r.status_code} ({model})", "обнови GEMINI_TEXT_MODEL или проверь лимит")
    except Exception as e:
        return ("🔴", str(e)[:45], "сеть до generativelanguage.googleapis.com")


def check_ollama():
    base = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434").rstrip("/")
    required = [
        item.strip()
        for item in os.getenv(
            "OLLAMA_REQUIRED_MODELS",
            "qwen3:1.7b,qwen3-vl:2b",
        ).split(",")
        if item.strip()
    ]
    tcp_ok, tcp_status = _tcp_probe(base, timeout=float(os.getenv("OLLAMA_CHECK_TIMEOUT", "3")))
    if not tcp_ok:
        return (
            "⚪",
            f"порт недоступен ({tcp_status})",
            (
                f"запусти Ollama на Mac: `brew services restart ollama`; "
                f"затем: curl --max-time 5 {base}/api/tags"
            ),
        )
    try:
        r = _get(base + "/api/tags", {}, float(os.getenv("OLLAMA_CHECK_TIMEOUT", "5")))
        if r.status_code != 200:
            return ("🔴", f"HTTP {r.status_code}", f"проверь Ollama API: {base}/api/tags")
        data = r.json() if hasattr(r, "json") else json.loads(r.text or "{}")
        installed = {
            str(item.get("name") or "").strip()
            for item in data.get("models", [])
            if isinstance(item, dict) and item.get("name")
        }
        if required:
            missing = [model for model in required if model not in installed]
            if missing:
                pulls = " && ".join(f"ollama pull {model}" for model in missing)
                return (
                    "⚠️",
                    f"API ok, но нет моделей: {', '.join(missing)}",
                    f"выполни локально: {pulls}",
                )
        if not installed:
            return ("⚠️", "API ok, но список моделей пуст", "установи модель: ollama pull qwen3:1.7b")
        return ("🟢", "ok (Mac, обязательные модели есть)", "")
    except Exception as e:
        return ("🔴", str(e)[:45], f"TCP есть, но HTTP API не отвечает: {base}/api/tags")


def brain_summary(deepseek, groq, gemini, ollama=None):
    """Describe the first usable provider in the configured production chain."""
    if ollama and ollama[0] == "🟢":
        return True, "✅ Локальный мозг работает; сложные задачи маршрутизируются в Codex/Claude по подписке."
    for label, state in (
        ("DeepSeek", deepseek),
        ("Gemini", gemini),
        ("Groq", groq),
    ):
        if state[0] == "🟢":
            return True, f"✅ Мозг работает через {label}; остальные провайдеры остаются резервом."
    return False, "🔴 ВНИМАНИЕ: DeepSeek, Gemini и Groq недоступны — генерация ответов остановлена."


def main():
    import datetime
    today = datetime.date.today().strftime("%d.%m.%Y")

    external_enabled = os.getenv("ALLOW_EXTERNAL_LLM_FALLBACK", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
    disabled = ("⏸", "отключён политикой local-first", "включать только при осознанной API-оплате")
    ds = check_deepseek() if external_enabled else disabled
    gr = check_groq() if external_enabled else disabled
    gm = check_gemini() if external_enabled else disabled
    ol = check_ollama()

    L = [f"🩺 Здоровье LLM-провайдеров | {today}", ""]
    L.append("━━━ ОСНОВНОЙ КОНТУР ━━━")
    L.append(f"{ol[0]} Ollama/Qwen (Mac) — {ol[1]}" + (f"\n   ↳ {ol[2]}" if ol[2] else ""))
    L.append("🧭 amori-ai — простые запросы локально; код → Codex; архитектура/анализ → Claude")

    L.append("\n━━━ ВНЕШНИЕ API (ОПЦИОНАЛЬНО) ━━━")
    L.append(f"{ds[0]} DeepSeek/OpenModel — {ds[1]}")
    L.append(f"{gm[0]} Gemini — {gm[1]}")
    L.append(f"{gr[0]} Groq — {gr[1]}")

    brain_ok, summary = brain_summary(ds, gr, gm, ol)
    L.append("")
    L.append(summary)
    L.append("ℹ️ Codex и Claude используются через их CLI/OAuth; отдельные API-ключи не нужны.")

    report = "\n".join(L)
    print(report)

    # heartbeats (видно в check_agents/дашборде)
    hb = {"llm_deepseek": ds, "llm_groq": gr, "llm_gemini": gm, "llm_ollama": ol}
    for comp, s in hb.items():
        try:
            status = "ok" if s[0] == "🟢" else "disabled" if s[0] == "⏸" else "warn"
            ops_store.heartbeat(comp, status, {"status": s[1]})
        except Exception:
            pass
    for comp in ("llm_qwen", "llm_glm", "llm_kimi"):
        try:
            ops_store.heartbeat(comp, "disabled", {"status": "optional proxy disabled intentionally"})
        except Exception:
            pass

    try:
        notify.send(report, level="ok" if brain_ok else "warn")
    except Exception as e:
        print(f"[provider_health] notify упал: {e}")


if __name__ == "__main__":
    main()
