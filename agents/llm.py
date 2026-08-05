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
from types import SimpleNamespace
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import router
import cost_guard
from retry import net_retry


DEFAULT_GROQ_MODEL = os.getenv("DEFAULT_GROQ_MODEL", "openai/gpt-oss-120b")
DEFAULT_GROQ_LITELLM_MODEL = f"groq/{DEFAULT_GROQ_MODEL}"
DEPRECATED_GROQ_MODELS = {
    "llama-3.3-70b-versatile",
    "groq/llama-3.3-70b-versatile",
}


def normalize_groq_model(model: str, litellm: bool = False) -> str:
    """Return a supported Groq model, replacing models scheduled for shutdown."""
    value = (model or "").strip()
    if not value:
        return DEFAULT_GROQ_LITELLM_MODEL if litellm else DEFAULT_GROQ_MODEL
    if value.lower() in DEPRECATED_GROQ_MODELS:
        return DEFAULT_GROQ_LITELLM_MODEL if litellm or value.startswith("groq/") else DEFAULT_GROQ_MODEL
    return value

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


GROQ_FALLBACK = normalize_groq_model(os.getenv("FREE_FALLBACK_MODEL", DEFAULT_GROQ_LITELLM_MODEL), litellm=True)
# Реестр параметров сборки агента → чтобы пересобрать на Groq при пустом ответе.
_AGENT_BUILD = {}

# FreeQwenApi — локальный OpenAI-совместимый прокси к chat.qwen.ai (:3264).
# Модель задаётся спец-префиксом ``qwen-free/<tag>`` (например qwen-free/qwen3.7-max);
# выбирается в дашборде. Через litellm идёт как openai/<tag> + custom base_url.
QWEN_FREE_PREFIX = "qwen-free/"
FREEQWEN_API_BASE = os.getenv("FREEQWEN_API_BASE", "http://localhost:3264/api")
FREEQWEN_API_KEY = os.getenv("FREEQWEN_API_KEY", "dummy-key")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-3.6-flash")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini/gemini-3.6-flash")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


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


def fallback_models(primary_model=None):
    """Ordered, configured text fallbacks, excluding the model that just failed."""
    configured = os.getenv(
        "LLM_FALLBACK_MODELS",
        f"{GEMINI_TEXT_MODEL},{GROQ_FALLBACK}",
    )
    primary = str(primary_model or "").lower()
    result = []
    for value in configured.split(","):
        model = value.strip()
        if not model or model.lower() == primary or model in result:
            continue
        if model.startswith("gemini/") and not GEMINI_API_KEY:
            continue
        result.append(model)
    return result


def _fallback_text(prompt, agent_key, *, primary_model=None, agent_kwargs=None):
    """Try configured providers in order and return ``(text, used_model)``."""
    from praisonaiagents import Agent

    kwargs = dict(agent_kwargs or {})
    if not any(kwargs.get(key) for key in ("name", "role", "goal", "backstory", "instructions")):
        kwargs["instructions"] = "Отвечай по делу, точно и на языке пользователя."
    for model in fallback_models(primary_model):
        print(f"[llm] {agent_key}: fallback -> {model}")
        try:
            candidate = Agent(llm=_resolve_llm(model), **kwargs)
            result = candidate.start(prompt)
            if not _is_empty(result):
                return str(result), model
        except Exception as e:
            print(f"[llm] {agent_key} fallback {model} упал: {e}")
    return "", ""


def _messages_prompt(messages):
    labels = {"system": "SYSTEM", "assistant": "ASSISTANT", "user": "USER"}
    return "\n\n".join(
        f"{labels.get(str(item.get('role')), 'MESSAGE')}: {item.get('content', '')}"
        for item in messages
    )


def run(agent, prompt: str, agent_key: str = None, attempts: int = 2):
    """Выполнить agent.start(prompt) с ретраем. Если модель вернула пусто —
    пересобрать агента на Groq и повторить (важно: ollama/GPU-нода бывает флапает
    и отдаёт пустой ответ — раньше это давало «голый заголовок» без анализа).
    Usage пишется детерминированно."""
    build = _AGENT_BUILD.get(id(agent))
    model = build[0] if build else (router.get_model(agent_key) if agent_key else "unknown")

    @net_retry(attempts=attempts)
    def _go():
        return agent.start(prompt)

    try:
        result = _go()
    except Exception as e:
        result = ""
        print(f"[llm] {agent_key} вызов упал: {e}")

    if _is_empty(result) and build:
        result, fallback_model = _fallback_text(
            prompt,
            agent_key,
            primary_model=model,
            agent_kwargs=build[1],
        )
        if fallback_model:
            model = fallback_model

    if agent_key:
        _record(agent_key, model, prompt, result)
    return result


def groq_chat(client, agent_key: str, messages, model: str = DEFAULT_GROQ_MODEL, **kwargs):
    """Прямой вызов Groq SDK с ретраем и учётом usage (для orchestrator)."""
    model = normalize_groq_model(model, litellm=False)

    @net_retry(attempts=2)
    def _go():
        return client.chat.completions.create(model=model, messages=messages, **kwargs)

    try:
        resp = _go()
    except Exception as primary_error:
        text, fallback_model = _fallback_text(
            _messages_prompt(messages),
            agent_key,
            primary_model=f"groq/{model}",
        )
        if not text:
            raise primary_error
        _record(agent_key, fallback_model, _messages_prompt(messages), text, source="fallback")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        )
    try:
        u = resp.usage
        cost_guard.record_usage(agent_key, f"groq/{model}",
                                getattr(u, "prompt_tokens", 0) or 0,
                                getattr(u, "completion_tokens", 0) or 0, source="groq")
    except Exception:
        pass
    return resp


UNSUPPORTED_AMORI_PATTERNS = {
    "real-time location": re.compile(r"\b(real[- ]?time|реальн\w*\s+врем\w*)\b", re.I),
    "exact location/accuracy": re.compile(r"\b(точн\w*\s+(местополож|геолокац|координат|gps)|точност\w*)\b", re.I),
    "safe-zone alerts": re.compile(r"\b(уведомлен\w*|оповещен\w*|безопасн\w*\s+зон\w*|гео[- ]?зон\w*)\b", re.I),
    "health/activity monitoring": re.compile(r"\b(здоровь\w*|активност\w*|пульс\w*|сон\w*|мониторинг\w*)\b", re.I),
    "available app": re.compile(r"\b(приложени\w*\s+(уже\s+)?(доступн|работа\w*|скача\w*)|ios\s+и\s+android\s+доступн)\b", re.I),
    "absolute guarantee": re.compile(r"\b(гарантир\w*|никогда\s+не\s+потеря\w*|всегда\s+(зна\w*|под\s+контрол\w*))\b", re.I),
}


def unsupported_product_claims(text: str) -> list[str]:
    """Detect claims Amori agents must not make without verified product data."""
    s = str(text or "")
    return [label for label, pattern in UNSUPPORTED_AMORI_PATTERNS.items() if pattern.search(s)]


def ensure_safe_amori_output(text: str, agent_key: str = "agent") -> str:
    """Fail closed when generated Amori copy invents product capabilities."""
    claims = unsupported_product_claims(text)
    if claims:
        raise ValueError(f"{agent_key}: неподтверждённые claims: {', '.join(claims)}")
    return str(text or "")


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


def _gemini_vision_analyze(prompt: str, image_paths, timeout: int = 90) -> str:
    """Fallback image analysis through Gemini's generateContent API."""
    if not GEMINI_API_KEY:
        return ""
    import base64
    import urllib.request

    parts = [{"text": prompt}]
    for p in (image_paths if isinstance(image_paths, (list, tuple)) else [image_paths]):
        ext = (os.path.splitext(str(p))[1].lower().lstrip(".") or "png")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        parts.append({
            "inline_data": {
                "mime_type": mime,
                "data": base64.b64encode(open(p, "rb").read()).decode(),
            }
        })
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200},
    }).encode()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_VISION_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            break
        except Exception as e:
            last = e
    else:
        raise last if last else RuntimeError("gemini vision: unknown error")
    candidates = data.get("candidates") or []
    parts_out = (((candidates[0] if candidates else {}).get("content") or {}).get("parts") or [])
    return "\n".join(p.get("text", "") for p in parts_out).strip()


def _groq_vision_analyze(prompt: str, image_paths, timeout: int = 90) -> str:
    """Fallback image analysis through Groq vision models."""
    import base64
    from groq import Groq

    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        return ""
    content = [{"type": "text", "text": prompt}]
    for p in (image_paths if isinstance(image_paths, (list, tuple)) else [image_paths]):
        ref = str(p)
        if ref.startswith(("http://", "https://", "data:image/")):
            url = ref
        else:
            ext = (os.path.splitext(ref)[1].lower().lstrip(".") or "png")
            mime = "jpeg" if ext in ("jpg", "jpeg") else ext
            b64 = base64.b64encode(open(ref, "rb").read()).decode()
            url = f"data:image/{mime};base64,{b64}"
        content.append({"type": "image_url", "image_url": {"url": url}})
    client = Groq(api_key=key, timeout=timeout)
    resp = client.chat.completions.create(
        model=GROQ_VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        temperature=0.2,
        max_tokens=1200,
    )
    return (resp.choices[0].message.content or "").strip()


def vision_analyze(prompt: str, image_paths, agent_key: str = "orchestrator",
                   model: str = "qwen3-vl-plus", fallback_image_paths=None) -> str:
    """Analyze images through Qwen vision, with Gemini as a fallback."""
    import base64
    content = [{"type": "text", "text": prompt}]
    for p in (image_paths if isinstance(image_paths, (list, tuple)) else [image_paths]):
        ref = str(p)
        if ref.startswith(("http://", "https://", "data:image/")):
            url = ref
        else:
            b64 = base64.b64encode(open(ref, "rb").read()).decode()
            ext = (os.path.splitext(ref)[1].lower().lstrip(".") or "png")
            mime = "jpeg" if ext in ("jpg", "jpeg") else ext
            url = f"data:image/{mime};base64,{b64}"
        content.append({"type": "image_url",
                        "image_url": {"url": url}})
    try:
        result = _freeqwen_chat([{"role": "user", "content": content}], model=model)
    except Exception as e:
        print(f"[llm] vision_analyze упал: {e}")
        result = ""
    used_model = f"qwen-free/{model}"
    if _is_empty(result):
        fallback_paths = fallback_image_paths if fallback_image_paths is not None else image_paths
        try:
            result = _groq_vision_analyze(prompt, fallback_paths)
            if result:
                used_model = f"groq/{GROQ_VISION_MODEL}"
        except Exception as e:
            print(f"[llm] groq vision fallback упал: {e}")
    if _is_empty(result):
        fallback_paths = fallback_image_paths if fallback_image_paths is not None else image_paths
        try:
            result = _gemini_vision_analyze(prompt, fallback_paths)
            if result:
                used_model = f"gemini/{GEMINI_VISION_MODEL}"
        except Exception as e:
            print(f"[llm] gemini vision fallback упал: {e}")
    _record(agent_key, used_model, prompt, result, source="vision")
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
    # 2) Configured fallback chain — бот не должен молчать.
    if _is_empty(result) or _looks_garbled(result):
        result, fallback_model = _fallback_text(
            prompt,
            agent_key,
            primary_model=used_model,
            agent_kwargs={"instructions": system or "Отвечай по делу, по-русски."},
        )
        if fallback_model:
            used_model = fallback_model
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
    print("[llm] token estimate:", count_tokens(DEFAULT_GROQ_LITELLM_MODEL, "hello world " * 10))
