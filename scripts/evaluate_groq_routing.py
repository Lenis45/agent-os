#!/usr/bin/env python3
"""Small deterministic canary for comparing Groq routing candidates.

The cases are synthetic and contain no customer or founder data. This is a gate for a
larger real-world evaluation, not permission to change production routing by itself.
"""
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "agents" / ".env")

MODELS = ("openai/gpt-oss-20b", "openai/gpt-oss-120b")
SYSTEM = (
    "Ты компонент бизнес-автоматизации. Верни только валидный JSON без markdown. "
    "Не добавляй факты, которых нет во входных данных."
)
CASES = (
    {
        "name": "calendar_extract",
        "input": (
            "Извлеки событие: Перенеси встречу с Иваном на 20 августа 2026, 15:00, "
            "офис Surf. Поля: action,title,date,time,location."
        ),
        "expect": {"date": "2026-08-20", "time": "15:00", "location": "офис Surf"},
        "allowed": {"action": {"update", "reschedule", "перенести"}},
    },
    {
        "name": "email_triage",
        "input": (
            "Классифицируй письмо. Поля: category,reply_required,priority. "
            "Письмо: Просим подписать договор поддержки до пятницы и прислать ответ."
        ),
        "expect": {"reply_required": True, "priority": "high"},
        "required": ("category",),
    },
    {
        "name": "task_priority",
        "input": (
            "Выбери top_task_id. T1: обновить аватар, срок через 7 дней. "
            "T2: ответить партнёру, просрочено 2 дня. T3: архивировать заметки."
        ),
        "expect": {"top_task_id": "T2"},
    },
    {
        "name": "lead_followup",
        "input": (
            "Лид запросил демо, получил условия, но не ответил 5 дней. Не отправляй "
            "сообщение сам. Верни объект ровно с двумя полями: next_action (непустая "
            "строка) и contact_needed (только JSON boolean true или false)."
        ),
        "expect": {"contact_needed": True},
        "required": ("next_action",),
    },
)


def passes_case(actual, case) -> bool:
    if not isinstance(actual, dict):
        return False
    if not all(actual.get(key) == value for key, value in case["expect"].items()):
        return False
    if not all(str(actual.get(key) or "").strip() for key in case.get("required", ())):
        return False
    return all(
        str(actual.get(key) or "").lower() in {str(value).lower() for value in values}
        for key, values in case.get("allowed", {}).items()
    )


def main() -> int:
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        print("GROQ_API_KEY не настроен", file=sys.stderr)
        return 2
    client = Groq(api_key=key, timeout=45)
    results = {}
    for model in MODELS:
        passed = prompt_tokens = completion_tokens = 0
        latency_ms = 0
        failures = []
        for case in CASES:
            started = time.perf_counter()
            response = None
            last_error = None
            for _attempt in range(2):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": SYSTEM},
                            {"role": "user", "content": case["input"]},
                        ],
                        temperature=0,
                        max_tokens=300,
                        response_format={"type": "json_object"},
                    )
                    break
                except Exception as exc:
                    last_error = exc
            try:
                if response is None:
                    raise last_error or RuntimeError("empty response")
                latency_ms += round((time.perf_counter() - started) * 1000)
                usage = response.usage
                prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                actual = json.loads(response.choices[0].message.content or "{}")
                if passes_case(actual, case):
                    passed += 1
                else:
                    failures.append(case["name"])
            except Exception as exc:
                failures.append(f"{case['name']}: {type(exc).__name__}")
        results[model] = {
            "passed": passed,
            "total": len(CASES),
            "tokens": prompt_tokens + completion_tokens,
            "latency_ms": latency_ms,
            "failures": failures,
        }

    print("Groq routing canary (synthetic, no private data)")
    for model, result in results.items():
        print(
            f"- {model}: {result['passed']}/{result['total']}, "
            f"{result['tokens']} tokens, {result['latency_ms']} ms"
        )
        if result["failures"]:
            print("  failures: " + ", ".join(result["failures"]))
    candidate = results[MODELS[0]]
    baseline = results[MODELS[1]]
    ready = candidate["passed"] == len(CASES) and candidate["passed"] >= baseline["passed"]
    print("Decision: " + ("20B ready for expanded evaluation" if ready else "keep 120B routing"))
    return 0 if baseline["passed"] == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
