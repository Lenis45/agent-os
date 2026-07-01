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


def _freeqwen_chat(messages, model: str, max_tokens: int = 1500,
                   temperature: float = 0.3, timeout: int = 120):
    """Прямой вызов FreeQwenApi (OpenAI-совместимо). Возвращает текст ответа.
    Используется для мультимодальных запросов (картинки), которые неудобно
    гонять через praisonaiagents. model — «голый» тег Qwen (qwen3-vl-plus и т.п.)."""
    import urllib.request
    url = FREEQWEN_API_BASE.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model, "messages": messages, "stream": False,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {FREEQWEN_API_KEY}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def vision_analyze(prompt: str, image_paths, agent_key: str = "orchestrator",
                   model: str = "qwen3-vl-plus") -> str:
    """Анализ изображений через Qwen-vision (FreeQwenApi). image_paths — пути к файлам.
    Usage пишется в llm_usage как tier 3 (free). Пустой/ошибка → ''."""
    import base64
    content = [{"type": "text", "text": prompt}]
    for p in (image_paths if isinstance(image_paths, (list, tuple)) else [image_paths]):
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        ext = (os.path.splitext(str(p))[1].lower().lstrip(".") or "png")
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/{mime};base64,{b64}"}})
    try:
        result = _freeqwen_chat([{"role": "user", "content": content}], model=model)
    except Exception as e:
        print(f"[llm] vision_analyze упал: {e}")
        result = ""
    _record(agent_key, f"qwen-free/{model}", prompt, result, source="vision")
    return result


# ── OpenModel / DeepSeek V4 Flash — основной «мозг» (Anthropic Messages API) ──
OPENMODEL_API_BASE = os.getenv("OPENMODEL_API_BASE", "https://api.openmodel.ai")
OPENMODEL_API_KEY = os.getenv("OPENMODEL_API_KEY", "")
OPENMODEL_MODEL = os.getenv("OPENMODEL_MODEL", "deepseek-v4-flash")


def _openmodel_chat(prompt: str, system: str = "", model: str = None,
                    max_tokens: int = 2000, timeout: int = 120) -> str:
    """Вызов OpenModel (Anthropic /v1/messages) через requests (надёжный TLS) с
    ретраем на разовый SSL/сетевой блип. Возвращает финальный text (thinking-блоки
    отбрасываем). Пусто/ошибка после ретраев → '' (выше уйдём на Groq)."""
    import requests
    mdl = model or OPENMODEL_MODEL
    url = OPENMODEL_API_BASE.rstrip("/") + "/v1/messages"
    body = {"model": mdl, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    hdr = {"Content-Type": "application/json",
           "Authorization": f"Bearer {OPENMODEL_API_KEY}",
           "anthropic-version": "2023-06-01"}
    last = None
    for _ in range(3):
        try:
            r = requests.post(url, json=body, headers=hdr, timeout=timeout)
            r.raise_for_status()
            blocks = r.json().get("content") or []
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        except Exception as e:
            last = e
    raise last if last else RuntimeError("openmodel: unknown error")


def _looks_garbled(text) -> bool:
    """Детектор артефакта FreeQwenApi: иногда стрим склеивает две копии ответа
    «через символ» (напр. «ПродаКраткоежи»). Признак — аномально частые переходы
    строчная→ЗАГЛАВНАЯ и буква↔цифра внутри слов. Норма << порога; ложное
    срабатывание не страшно (просто уйдём на Groq)."""
    import re
    t = str(text or "")
    if len(t) < 40:
        return False
    flips = len(re.findall(r'[a-zа-яё][A-ZА-ЯЁ]', t))
    digadj = len(re.findall(r'[0-9][а-яёa-z]|[а-яё][0-9]', t))
    return (flips + digadj) / len(t) > 0.01


def qwen_answer(prompt: str, system: str = "", agent_key: str = "orchestrator",
                model: str = None, max_tokens: int = 1500) -> str:
    """Содержательный ответ «мозга». PRIMARY = OpenModel/DeepSeek V4 Flash (надёжный
    Anthropic-API), FALLBACK = Groq. (Имя историческое — раньше был Qwen; Qwen-прокси
    лёг на анти-боте. Алиас: brain_answer.) Usage пишется детерминированно."""
    result = ""
    used_model = ""
    # 1) OpenModel / DeepSeek (если есть ключ)
    if OPENMODEL_API_KEY:
        try:
            result = _openmodel_chat(prompt, system=system, max_tokens=max_tokens)
            used_model = f"openmodel/{OPENMODEL_MODEL}"
        except Exception as e:
            print(f"[llm] openmodel упал: {e}")
            result = ""
    # 2) Groq-фолбэк — бот не должен молчать
    if _is_empty(result) or _looks_garbled(result):
        try:
            from praisonaiagents import Agent
            fb = Agent(llm=GROQ_FALLBACK, instructions=system or "Отвечай по делу, по-русски.")
            result = fb.start(prompt)
            used_model = GROQ_FALLBACK
        except Exception as e:
            print(f"[llm] qwen_answer groq-fallback упал: {e}")
    _record(agent_key, used_model or "unknown", prompt, result, source="brain")
    return str(result)


# Понятный алиас (исторически функция называется qwen_answer)
brain_answer = qwen_answer


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
