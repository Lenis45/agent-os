"""Read-only bridge from Amori Telegram agents to Hypothesis Hub."""

import json
import os
import urllib.error
import urllib.request

import llm


def fetch_summary() -> dict:
    """Fetch a bounded portfolio snapshot; never writes to Hypothesis Hub."""
    base_url = os.getenv("HYPOTHESIS_HUB_API_URL", "http://127.0.0.1:3001").rstrip("/")
    headers = {"Accept": "application/json"}
    token = os.getenv("HYPOTHESIS_HUB_TOKEN")
    if token:
        headers["x-amori-token"] = token
    request = urllib.request.Request(f"{base_url}/api/amori/summary", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError("нет доступа к Hypothesis Hub: проверь HYPOTHESIS_HUB_TOKEN") from error
        raise RuntimeError(f"Hypothesis Hub вернул HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Hypothesis Hub недоступен: {error.reason if hasattr(error, 'reason') else error}") from error
    if not isinstance(payload, dict) or "totals" not in payload or "hypotheses" not in payload:
        raise RuntimeError("Hypothesis Hub вернул неполный снимок данных")
    return payload


def analyze(question: str = "") -> str:
    """Ask Amori's configured model to analyse live portfolio data, not invent it."""
    snapshot = fetch_summary()
    system = """Ты — продуктовый аналитик Amori. Анализируешь только данные из Hypothesis Hub.
Отвечай на русском, коротко и без markdown-таблиц. Структура ответа:
1. Главный вывод.
2. Обоснование конкретными цифрами/названиями гипотез.
3. Риски или пробелы в данных.
4. До трёх следующих действий.
Не выдумывай метрики, результаты экспериментов и причинно-следственные связи. Если данных мало — прямо скажи это."""
    prompt = (
        f"Вопрос Дениса: {question or 'Дай краткий анализ текущего портфеля гипотез и приоритетов.'}\n\n"
        f"Снимок Hypothesis Hub:\n{json.dumps(snapshot, ensure_ascii=False, default=str)}"
    )
    return str(llm.qwen_answer(prompt, system=system, agent_key="analyst_agent"))
