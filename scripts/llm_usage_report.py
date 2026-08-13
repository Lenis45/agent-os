#!/usr/bin/env python3
"""Print a privacy-safe LLM usage report from the local operations database."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

import cost_guard
import ops_store


def build_report(days: int) -> dict:
    usage = cost_guard.usage_summary(days)
    calls = int(usage["calls"] or 0)
    provider_calls = int(usage["provider_calls"] or 0)
    observed_prompt = int(usage["cache_observed_prompt_tokens"] or 0)
    cached_prompt = int(usage["cached_prompt_tokens"] or 0)
    return {
        **usage,
        "measurement_coverage_pct": round(provider_calls * 100 / calls, 1) if calls else 0.0,
        "cache_hit_rate_pct": (
            round(cached_prompt * 100 / observed_prompt, 1) if observed_prompt else None
        ),
    }


def format_number(value: int) -> str:
    return f"{int(value or 0):,}".replace(",", " ")


def format_text(report: dict) -> str:
    cache = report["cache_hit_rate_pct"]
    cache_text = f"{cache}%" if cache is not None else "нет данных провайдера"
    lines = [
        f"LLM usage: последние {report['days']} дней",
        f"Вызовы: {format_number(report['calls'])}",
        f"Токены: {format_number(report['total_tokens'])} "
        f"(вход {format_number(report['prompt_tokens'])}, "
        f"выход {format_number(report['completion_tokens'])})",
        f"Точное измерение провайдером: {report['measurement_coverage_pct']}% вызовов",
        f"Prompt cache hit rate: {cache_text}",
        f"Средняя измеренная задержка: {format_number(report['avg_latency_ms'])} мс",
        "",
        "Наибольшая нагрузка:",
    ]
    if report["top_agents"]:
        for item in report["top_agents"]:
            lines.append(
                f"- {item['agent']}: {format_number(item['tokens'])} токенов, "
                f"{format_number(item['calls'])} вызовов"
            )
    else:
        lines.append("- данных пока нет")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Отчёт по использованию LLM в Amori OS")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="вывести JSON")
    args = parser.parse_args()
    ops_store.init()
    report = build_report(max(1, min(args.days, 365)))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else format_text(report))


if __name__ == "__main__":
    main()
