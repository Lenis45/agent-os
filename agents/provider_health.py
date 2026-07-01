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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

import notify      # noqa: E402
import ops_store   # noqa: E402


def _post(url, headers, body, timeout):
    import requests
    return requests.post(url, headers=headers, json=body, timeout=timeout)


def _get(url, headers, timeout):
    import requests
    return requests.get(url, headers=headers, timeout=timeout)


# ── проверки. каждая → (icon, status_text, fix_action) ──

def check_deepseek():
    key = os.getenv("OPENMODEL_API_KEY")
    base = os.getenv("OPENMODEL_API_BASE", "https://api.openmodel.ai").rstrip("/")
    mdl = os.getenv("OPENMODEL_MODEL", "deepseek-v4-flash")
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
    try:
        r = _get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", {}, 10)
        return ("🟢", "ok (ключ валиден)", "") if r.status_code == 200 else ("🔴", f"HTTP {r.status_code}", "проверь GEMINI_API_KEY")
    except Exception as e:
        return ("🔴", str(e)[:45], "сеть до generativelanguage.googleapis.com")


def check_ollama():
    base = os.getenv("OLLAMA_API_BASE", "http://100.77.9.84:11434")
    try:
        import urllib.request
        urllib.request.urlopen(base, timeout=3)
        return ("🟢", "ok (ПК включён)", "")
    except Exception:
        return ("⚪", "ПК выключен", f"включи ПК {base} + `ollama serve` (Gemma/Qwen локально, бесплатно)")


def main():
    import datetime
    today = datetime.date.today().strftime("%d.%m.%Y")

    ds = check_deepseek()
    gr = check_groq()
    gm = check_gemini()
    ol = check_ollama()

    L = [f"🩺 Здоровье LLM-провайдеров | {today}", ""]
    L.append("━━━ ОСНОВНЫЕ (мозг/воркеры) ━━━")
    L.append(f"{ds[0]} DeepSeek V4 Flash (OpenModel) — {ds[1]}   ← дефолт мозга" + (f"\n   ↳ {ds[2]}" if ds[2] else ""))
    L.append(f"{gr[0]} Groq (Llama 3.3 70B) — {gr[1]}   ← фолбэк + воркеры" + (f"\n   ↳ {gr[2]}" if gr[2] else ""))

    L.append("\n━━━ ОТКЛЮЧЕНЫ (опциональные, не используются) ━━━")
    L.append("⏸ Qwen / GLM / Kimi — веб-прокси выключены намеренно; мозг на DeepSeek. Чинить не нужно.")

    L.append("\n━━━ ЛОКАЛЬНЫЕ / ПРОЧЕЕ ━━━")
    L.append(f"{ol[0]} Ollama/Gemma (ПК) — {ol[1]}" + (f"\n   ↳ {ol[2]}" if ol[2] else ""))
    L.append(f"{gm[0]} Gemini — {gm[1]}" + (f"\n   ↳ {gm[2]}" if gm[2] else ""))

    brain_ok = ds[0] == "🟢" or gr[0] == "🟢"
    L.append("")
    L.append("✅ Мозг работает: DeepSeek + Groq-фолбэк." if brain_ok
             else "🔴 ВНИМАНИЕ: и DeepSeek, и Groq недоступны — мозг лежит!")
    L.append("ℹ️ DeepSeek — временная бесплатная акция OpenModel: следи за остатком кредита. Qwen выключен намеренно.")

    L.append("\n━━━ ЧТО ЕЩЁ МОЖНО ПОДКЛЮЧИТЬ (нужен ключ) ━━━")
    L.append("• Официальные API (стабильно): Anthropic Claude · OpenAI GPT · Google Gemini · DeepSeek official · OpenRouter (агрегатор 300+ моделей)")
    L.append("• Через офиц. API без прокси: Qwen (DashScope) · GLM (Z.ai API) · Kimi (Moonshot)")
    L.append("• Локально бесплатно: Ollama на ПК — любые open-модели (Gemma, Qwen, Llama, DeepSeek-distill)")

    report = "\n".join(L)
    print(report)

    # heartbeats (видно в check_agents/дашборде)
    hb = {"llm_deepseek": ds, "llm_groq": gr, "llm_gemini": gm, "llm_ollama": ol}
    for comp, s in hb.items():
        try:
            ops_store.heartbeat(comp, "ok" if s[0] == "🟢" else "warn", {"status": s[1]})
        except Exception:
            pass

    try:
        notify.send(report, level="ok" if brain_ok else "warn")
    except Exception as e:
        print(f"[provider_health] notify упал: {e}")


if __name__ == "__main__":
    main()
