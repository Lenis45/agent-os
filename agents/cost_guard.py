"""
cost_guard — учёт расходов LLM + месячный предохранитель (v3.0).

Закрывает два пробела:
  1. Наблюдаемость: record_usage() пишет КАЖДЫЙ вызов (free тоже — для видимости нагрузки).
  2. Бюджет: guard_model() не даёт уйти за месячный потолок на ПЛАТНЫЙ API —
     при превышении даунгрейдит на free tier (Groq) согласно budget_config.

Сейчас платный API не сконфигурирован (router отдаёт только groq/ollama),
поэтому guard «спит» — но система готова к подключению Anthropic/OpenAI с первого дня.
"""
import os
import ops_store

# Курс для оценки (₽ за $). Приблизительно; уточняется в budget_config при желании.
USD_RUB = float(os.getenv("USD_RUB", "90"))

# Цена за 1M токенов в USD: (prompt, completion). Матчинг по подстроке model.
PRICING_USD_PER_M = {
    "claude-opus":   (15.0, 75.0),
    "claude-3-opus": (15.0, 75.0),
    "opus":          (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "sonnet":        (3.0, 15.0),
    "claude-haiku":  (0.80, 4.0),
    "haiku":         (0.80, 4.0),
    "gpt-5":         (2.50, 10.0),
    "gpt-4o":        (2.50, 10.0),
    "gpt-4":         (10.0, 30.0),
    "o1":            (15.0, 60.0),
}

# Тиры по модели
TIER1_MARKERS = ("pro-web", "plus-web", "manual")          # ручные подписки
PAID_MARKERS = ("claude", "gpt-", "openai", "anthropic", "o1")  # платный API


def model_tier(model: str) -> int:
    m = (model or "").lower()
    if any(x in m for x in TIER1_MARKERS):
        return 1
    # free/local ДО PAID_MARKERS: qwen идёт через FreeQwenApi как openai/qwen…,
    # а "openai" есть в PAID_MARKERS — без этой строки бесплатный Qwen считался бы платным.
    if (m.startswith("groq/") or "ollama" in m or "local" in m
            or "gemini" in m or "qwen" in m):
        return 3
    if any(x in m for x in PAID_MARKERS):
        return 2
    return 3


def estimate_cost_rub(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = (model or "").lower()
    for key, (pin, pout) in PRICING_USD_PER_M.items():
        if key in m:
            usd = (prompt_tokens / 1_000_000) * pin + (completion_tokens / 1_000_000) * pout
            return round(usd * USD_RUB, 4)
    return 0.0  # free / local / unknown → 0


def record_usage(agent: str, model: str, prompt_tokens: int = 0,
                 completion_tokens: int = 0, source: str = "agent", meta: dict = None) -> float:
    """Записать вызов в llm_usage. Возвращает рассчитанную стоимость в ₽."""
    import json
    tier = model_tier(model)
    cost = estimate_cost_rub(model, prompt_tokens, completion_tokens)
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO llm_usage(agent, model, tier, prompt_tokens, completion_tokens, cost_rub, source, meta) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (agent, model, tier, prompt_tokens, completion_tokens, cost, source, json.dumps(meta or {})),
        )
        conn.commit()
    finally:
        conn.close()
    return cost


def month_spend_rub(paid_only: bool = True) -> float:
    """Сумма расходов за текущий календарный месяц."""
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        q = ("SELECT COALESCE(SUM(cost_rub),0) FROM llm_usage "
             "WHERE date_trunc('month', ts) = date_trunc('month', now())")
        if paid_only:
            q += " AND tier = 2"
        cur.execute(q)
        return float(cur.fetchone()[0])
    finally:
        conn.close()


def remaining_paid_rub() -> float:
    cap = float(ops_store.get_budget("monthly_paid_cap_rub", "2500"))
    return max(0.0, cap - month_spend_rub(paid_only=True))


def allow_paid() -> bool:
    """Можно ли ещё ходить в платный API в этом месяце."""
    return remaining_paid_rub() > 0


def guard_model(model: str, agent: str = "?") -> str:
    """
    Предохранитель: если модель платная (tier 2), а бюджет исчерпан —
    вернуть free-замену согласно over_budget_action. Иначе вернуть как есть.
    """
    if model_tier(model) != 2:
        return model
    if allow_paid():
        return model
    action = ops_store.get_budget("over_budget_action", "downgrade")
    if action == "block":
        raise RuntimeError(f"[cost_guard] месячный лимит платного API исчерпан — вызов '{agent}' заблокирован")
    # downgrade | queue_tier1 → отдаём free tier
    fallback = os.getenv("FREE_FALLBACK_MODEL", "groq/llama-3.3-70b-versatile")
    print(f"[cost_guard] бюджет исчерпан, '{agent}': {model} → {fallback}")
    return fallback


if __name__ == "__main__":
    ops_store.init()
    # smoke
    c = record_usage("smoke_test", "groq/llama-3.3-70b-versatile", 1000, 500, source="selftest")
    print(f"[cost_guard] free вызов записан, cost={c}₽")
    c2 = estimate_cost_rub("claude-sonnet", 10000, 4000)
    print(f"[cost_guard] оценка claude-sonnet 10k/4k = {c2}₽")
    print(f"[cost_guard] platno потрачено в этом месяце = {month_spend_rub()}₽, осталось = {remaining_paid_rub()}₽")
    print(f"[cost_guard] guard(claude-sonnet) = {guard_model('claude-sonnet','smoke')}")
