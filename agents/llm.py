"""
llm — единая обёртка работы с моделями для агентов (v3.0 hardening).

Закрывает пробел: раньше ни один агент не учитывал LLM-расходы. Теперь любой вызов
через praisonaiagents (litellm под капотом) автоматически пишется в ops_db.llm_usage
через success-callback litellm + cost_guard. Плюс:
  - build_agent(agent_key, ...) — Agent с моделью из router (роутинг + бюджет-гард),
  - run(agent, prompt, agent_key) — выполнение с ретраем,
  - groq_chat(...) — обёртка для прямых вызовов Groq SDK (orchestrator) с учётом usage,
  - parse_json(text) — устойчивый разбор JSON из ответа модели.
"""
import os
import re
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import router
import cost_guard
from retry import net_retry

def count_tokens(model: str, text: str) -> int:
    """Быстрая оценка числа токенов (эвристика ~len/4). Без сети — litellm.token_counter
    тянет токенайзер по сети и виснет на таймауте, поэтому не используем его."""
    if not text:
        return 0
    return max(1, len(str(text)) // 4)


def _record(agent_key: str, model: str, prompt: str, result, source: str = "agent"):
    """Записать вызов в ops_db.llm_usage (учёт не должен ломать основной поток)."""
    try:
        cost_guard.record_usage(
            agent_key, model,
            count_tokens(model, prompt), count_tokens(model, str(result)),
            source=source,
        )
    except Exception:
        pass


GROQ_FALLBACK = os.getenv("FREE_FALLBACK_MODEL", "groq/llama-3.3-70b-versatile")
# Реестр параметров сборки агента → чтобы пересобрать на Groq при пустом ответе.
_AGENT_BUILD = {}

# FreeQwenApi — локальный OpenAI-совместимый прокси к chat.qwen.ai (:3264).
# Модель задаётся спец-префиксом ``qwen-free/<tag>`` (например qwen-free/qwen3.7-max);
# выбирается в дашборде. Через litellm идёт как openai/<tag> + custom base_url.
QWEN_FREE_PREFIX = "qwen-free/"
FREEQWEN_API_BASE = os.getenv("FREEQWEN_API_BASE", "http://localhost:3264/api")
FREEQWEN_API_KEY = os.getenv("FREEQWEN_API_KEY", "dummy-key")


def _resolve_llm(model):
    """Превратить строку модели в аргумент llm для praisonaiagents.Agent.
    qwen-free/<tag> → dict на локальный FreeQwenApi (litellm openai-совместимо,
    POST {base}/chat/completions). Остальное — как есть (строка модели)."""
    if isinstance(model, str) and model.startswith(QWEN_FREE_PREFIX):
        tag = model[len(QWEN_FREE_PREFIX):]
        return {
            "model": "openai/" + tag,
            "base_url": FREEQWEN_API_BASE,
            "api_key": FREEQWEN_API_KEY,
        }
    return model


def build_agent(agent_key: str, **agent_kwargs):
    """praisonaiagents.Agent с моделью из router (+ бюджет-гард).

    Для qwen-free/<tag> собираем llm-dict на FreeQwenApi, но в _AGENT_BUILD храним
    ИСХОДНУЮ строку модели — empty→groq fallback в run() проверяет строку
    (``"groq" not in model.lower()``), а не dict."""
    from praisonaiagents import Agent
    model = agent_kwargs.pop("llm", None) or router.get_model(agent_key)
    a = Agent(llm=_resolve_llm(model), **agent_kwargs)
    _AGENT_BUILD[id(a)] = (model, dict(agent_kwargs))  # исходная строка → fallback-пересборка
    return a


def _is_empty(r) -> bool:
    return not r or not str(r).strip()


def run(agent, prompt: str, agent_key: str = None, attempts: int = 2):
    """Выполнить agent.start(prompt) с ретраем. Если модель вернула пусто —
    пересобрать агента на Groq и повторить (важно: ollama/GPU-нода бывает флапает
    и отдаёт пустой ответ — раньше это давало «голый заголовок» без анализа).
    Usage пишется детерминированно."""
    model = router.get_model(agent_key) if agent_key else "unknown"

    @net_retry(attempts=attempts)
    def _go():
        return agent.start(prompt)

    try:
        result = _go()
    except Exception as e:
        result = ""
        print(f"[llm] {agent_key} вызов упал: {e}")

    # Fallback: пустой ответ + знаем как пересобрать + текущая модель не groq
    if _is_empty(result) and id(agent) in _AGENT_BUILD and "groq" not in model.lower():
        from praisonaiagents import Agent
        _, kwargs = _AGENT_BUILD[id(agent)]
        print(f"[llm] {agent_key}: пустой ответ от {model} → fallback {GROQ_FALLBACK}")
        try:
            fb = Agent(llm=GROQ_FALLBACK, **kwargs)
            result = fb.start(prompt)
            model = GROQ_FALLBACK
        except Exception as e:
            print(f"[llm] {agent_key} groq-fallback упал: {e}")

    if agent_key:
        _record(agent_key, model, prompt, result)
    return result


def groq_chat(client, agent_key: str, messages, model: str = "llama-3.3-70b-versatile", **kwargs):
    """Прямой вызов Groq SDK с ретраем и учётом usage (для orchestrator)."""

    @net_retry(attempts=2)
    def _go():
        return client.chat.completions.create(model=model, messages=messages, **kwargs)

    resp = _go()
    try:
        u = resp.usage
        cost_guard.record_usage(agent_key, f"groq/{model}",
                                getattr(u, "prompt_tokens", 0) or 0,
                                getattr(u, "completion_tokens", 0) or 0, source="groq")
    except Exception:
        pass
    return resp


def parse_json(text: str, default=None):
    """Достать JSON-объект/массив из ответа модели. None/default при неудаче."""
    if not text:
        return default
    s = str(text).strip()
    # срезаем markdown-ограждение ```json ... ```
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return default
    return default


if __name__ == "__main__":
    print("[llm] router model for chief_of_staff:", router.get_model("chief_of_staff"))
    print("[llm] parse_json test:", parse_json('```json\n{"ok": true}\n```'))
    print("[llm] token estimate:", count_tokens("groq/llama-3.3-70b-versatile", "hello world " * 10))
