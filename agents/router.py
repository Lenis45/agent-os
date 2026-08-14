import os
import time
import urllib.request
import json

# cost_guard — опционален. Импорт защищён: если что-то не так с ops_db,
# роутинг обязан продолжать работать (это критический путь всех агентов).
try:
    import cost_guard
    _GUARD = True
except Exception as _e:  # pragma: no cover
    _GUARD = False
    print(f"[Router] cost_guard недоступен ({_e}); бюджет-гард отключён")

# Override модели на агента из UI (ops_db.agent_config). Кэш 30с, fail-safe:
# любая ошибка чтения → используем дефолтный ROUTING (роутинг не должен падать).
_override = {"models": {}, "ts": 0.0}
_ollama_cache = {"models": set(), "ok": False, "ts": 0.0, "error": ""}
DEPRECATED_MODEL_REPLACEMENTS = {
    "groq/llama-3.3-70b-versatile": "groq/openai/gpt-oss-120b",
    "groq/qwen/qwen3-32b": "groq/openai/gpt-oss-120b",
    "groq/meta-llama/llama-4-scout-17b-16e-instruct": "groq/qwen/qwen3.6-27b",
}

def _model_overrides() -> dict:
    now = time.time()
    if now - _override["ts"] > 30:
        try:
            import ops_store
            _override["models"] = ops_store.get_agent_models()
        except Exception:
            pass  # держим прошлый кэш
        _override["ts"] = now
    return _override["models"]

DEFAULT_GROQ_LITELLM_MODEL = os.getenv("FREE_FALLBACK_MODEL", "groq/openai/gpt-oss-120b")
if DEFAULT_GROQ_LITELLM_MODEL == "groq/llama-3.3-70b-versatile":
    DEFAULT_GROQ_LITELLM_MODEL = "groq/openai/gpt-oss-120b"
LOCAL_FAST_MODEL = os.getenv("LOCAL_FAST_MODEL", "ollama/qwen3:1.7b")
LOCAL_WORK_MODEL = os.getenv("LOCAL_WORK_MODEL", "ollama/qwen3:1.7b")
ALLOW_EXTERNAL_FALLBACK = os.getenv("ALLOW_EXTERNAL_LLM_FALLBACK", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

# Правила роутинга
# Регулярные фоновые задачи выполняются локально. Codex/Claude подключаются
# отдельным subscription-router только для явных сложных пользовательских запросов.
ROUTING = {
    "chief_of_staff":     LOCAL_WORK_MODEL,
    "email_watchdog":     LOCAL_WORK_MODEL,
    "knowledge_curator":  LOCAL_FAST_MODEL,
    "context_translator": LOCAL_FAST_MODEL,
    "task_sync":          LOCAL_WORK_MODEL,
    "research_agent":     LOCAL_WORK_MODEL,
    "code_agent":         LOCAL_WORK_MODEL,
    "content_agent":      LOCAL_WORK_MODEL,
    "analyst_agent":      LOCAL_WORK_MODEL,
}

def get_model(agent_name: str) -> str:
    # Override из UI (agent_config) имеет приоритет над дефолтом ROUTING
    model = _model_overrides().get(agent_name) or ROUTING.get(agent_name, DEFAULT_GROQ_LITELLM_MODEL)
    model = DEPRECATED_MODEL_REPLACEMENTS.get(model, model)

    # Не уходить во внешний API молча. Сначала пробуем малую локальную модель,
    # внешний free-tier доступен только через явный opt-in в .env.
    if model.startswith("ollama"):
        required = model.split("/", 1)[1] if "/" in model else ""
        if not _check_ollama(required_model=required):
            local_fallback = LOCAL_FAST_MODEL.split("/", 1)[1]
            ok, models, error = _ollama_models()
            if ok and local_fallback in models:
                print(f"[Router] {required} недоступна, fallback → {LOCAL_FAST_MODEL}")
                return LOCAL_FAST_MODEL
            if ALLOW_EXTERNAL_FALLBACK:
                print("[Router] Ollama недоступен, разрешённый fallback → Groq")
                return DEFAULT_GROQ_LITELLM_MODEL
            print(f"[Router] Ollama недоступен ({error}); внешний fallback запрещён")

    # Бюджет-гард: если модель платная (tier 2) и месячный лимит исчерпан —
    # cost_guard сам даунгрейднет на free tier. Для free/local моделей это no-op,
    # поэтому текущее поведение не меняется (платных моделей в ROUTING пока нет).
    if _GUARD:
        try:
            model = cost_guard.guard_model(model, agent_name)
        except Exception as e:
            print(f"[Router] guard error ({e}); отдаю модель без гарда")

    return model

def _ollama_models(force: bool = False) -> tuple[bool, set[str], str]:
    now = time.time()
    if not force and now - _ollama_cache["ts"] < 20:
        return bool(_ollama_cache["ok"]), set(_ollama_cache["models"]), str(_ollama_cache["error"])
    try:
        base = os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434").rstrip("/")
        request = urllib.request.Request(base + "/api/tags")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=float(os.getenv("OLLAMA_CHECK_TIMEOUT", "2.5"))) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        models = {
            str(item.get("name") or "").strip()
            for item in payload.get("models", [])
            if isinstance(item, dict) and item.get("name")
        }
        _ollama_cache.update({"models": models, "ok": True, "ts": now, "error": ""})
        return True, models, ""
    except Exception as e:
        _ollama_cache.update({"models": set(), "ok": False, "ts": now, "error": str(e)[:120]})
        return False, set(), str(e)[:120]


def _check_ollama(required_model: str | None = None) -> bool:
    ok, models, _error = _ollama_models()
    if not ok:
        return False
    if required_model:
        return required_model in models
    return True

if __name__ == "__main__":
    print("Роутинг моделей:")
    for agent, model in ROUTING.items():
        print(f"  {agent:25} → {model}")
    print(f"\nOllama доступен: {_check_ollama()}")
