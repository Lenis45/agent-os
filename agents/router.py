import os
import time

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

# Правила роутинга
# Приватные данные и тяжёлый анализ → локально
# Быстрые задачи и коммуникации → Groq
ROUTING = {
    "chief_of_staff":     "groq/llama-3.3-70b-versatile",  # читает TG — быстро нужно
    "email_watchdog":     "groq/llama-3.3-70b-versatile",  # почта — быстро
    "knowledge_curator":  "groq/llama-3.3-70b-versatile",  # сохранение заметок
    "context_translator": "groq/llama-3.3-70b-versatile",  # перевод задач — скорость важна
    "task_sync":          "ollama/gpt-oss:20b",            # анализ задач — приватно
    "research_agent":     "ollama/gpt-oss:20b",            # тяжёлый анализ — локально
    "code_agent":         "ollama/qwen2.5-coder:7b",       # код — локально
    "content_agent":      "ollama/gpt-oss:20b",            # контент — локально
    "analyst_agent":      "ollama/gpt-oss:20b",            # данные — приватно
}

def get_model(agent_name: str) -> str:
    # Override из UI (agent_config) имеет приоритет над дефолтом ROUTING
    model = _model_overrides().get(agent_name) or ROUTING.get(agent_name, "groq/llama-3.3-70b-versatile")

    # Если системник недоступен — fallback на Groq
    if model.startswith("ollama"):
        if not _check_ollama():
            print(f"[Router] Ollama недоступен, fallback → Groq")
            return "groq/llama-3.3-70b-versatile"

    # Бюджет-гард: если модель платная (tier 2) и месячный лимит исчерпан —
    # cost_guard сам даунгрейднет на free tier. Для free/local моделей это no-op,
    # поэтому текущее поведение не меняется (платных моделей в ROUTING пока нет).
    if _GUARD:
        try:
            model = cost_guard.guard_model(model, agent_name)
        except Exception as e:
            print(f"[Router] guard error ({e}); отдаю модель без гарда")

    return model

def _check_ollama() -> bool:
    try:
        import urllib.request
        urllib.request.urlopen(
            os.getenv("OLLAMA_API_BASE", "http://100.77.9.84:11434"),
            timeout=3
        )
        return True
    except:
        return False

if __name__ == "__main__":
    print("Роутинг моделей:")
    for agent, model in ROUTING.items():
        print(f"  {agent:25} → {model}")
    print(f"\nOllama доступен: {_check_ollama()}")
