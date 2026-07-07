"""Shared safety contracts for Amori agents.

Small deterministic checks live here so agent prompts are not the only guardrail.
"""
import os
import re

import llm


EXTERNAL_ACTION_PATTERNS = {
    "published": re.compile(r"\b(опубликовал[аи]?|опубликован[оаы]?|размещен[оаы]?|запостил[аи]?)\b", re.I),
    "sent": re.compile(r"\b(отправил[аи]?|письмо\s+отправлен[оа]?|рассылка\s+запущен[а]?)\b", re.I),
    "implemented": re.compile(r"\b(внедрил[аи]?|исправил[аи]?\s+в\s+коде|изменения\s+применены|протестировал[аи]?)\b", re.I),
}


def output_issues(text: str) -> list[str]:
    issues = [f"unsupported_product_claim:{x}" for x in llm.unsupported_product_claims(text)]
    s = str(text or "")
    for label, pattern in EXTERNAL_ACTION_PATTERNS.items():
        if pattern.search(s):
            issues.append(f"unverified_external_action:{label}")
    return issues


def ensure_safe_marketing_text(text: str, agent_key: str) -> str:
    """Reject generated public-facing copy with unsupported Amori claims."""
    return llm.ensure_safe_amori_output(text, agent_key)


def safe_product_fallback(context: str = "") -> str:
    base = (
        "Мы готовим Amori для владельцев, которым важно спокойствие за питомца. "
        "Параметры продукта, приложения, цены и сроков команда ещё уточняет, поэтому "
        "перед публикацией не будем обещать неподтверждённые функции."
    )
    if context:
        return f"{base}\n\nКонтекст для команды: {context[:500]}"
    return base


def require_env(*names: str) -> tuple[bool, str]:
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        return False, "не настроены переменные окружения: " + ", ".join(missing)
    return True, ""


def is_real_publish_result(ok: bool, info: str) -> bool:
    """True only when an integration actually sent content to an external channel."""
    if not ok:
        return False
    s = str(info or "").lower()
    return any(x in s for x in ("отправлено в telegram", "telegram api ok", "message_id"))
