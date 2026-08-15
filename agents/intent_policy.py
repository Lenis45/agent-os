"""Server-side compatibility checks for non-deterministic intent decisions."""

from __future__ import annotations

import re


KNOWN_TOOLS = {
    "check_agents", "translate", "check_tasks", "check_calendar", "calendar_week",
    "add_calendar_event", "change_calendar_event", "save_note", "update_team",
    "answer", "add_lead", "leads_report", "send_email_lead", "send_bulk_emails",
    "update_lead", "get_leads", "new_project", "make_content", "hypotheses",
}

CALENDAR_NOUNS = ("календар", "встреч", "созвон", "мероприят", "событ", "звонок")


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def _calendar_compatible(tool: str, text: str) -> bool:
    if not _contains_any(text, CALENDAR_NOUNS):
        return False
    if tool == "add_calendar_event":
        return _contains_any(text, ("добав", "постав", "заплан", "создай", "внеси", "занеси"))
    if tool == "change_calendar_event":
        return _contains_any(text, ("перенеси", "измени", "исправ", "переимен", "удали", "отмени"))
    return _contains_any(text, ("покажи", "проверь", "список", "что в", "какие"))


def validate_tool_decision(message: str, decision: dict) -> dict:
    """Fail closed when a model selects an incompatible or unknown action."""
    text = (message or "").strip()
    lowered = text.lower()
    tool = str((decision or {}).get("tool", "answer"))
    params = (decision or {}).get("params")
    params = params if isinstance(params, dict) else {}

    if tool not in KNOWN_TOOLS:
        tool = "answer"
    elif tool in {"add_calendar_event", "change_calendar_event", "calendar_week", "check_calendar"}:
        compatible_name = "calendar_week" if tool == "check_calendar" else tool
        if not _calendar_compatible(compatible_name, lowered):
            tool = "answer"
    elif tool in {"send_email_lead", "send_bulk_emails"}:
        if not _contains_any(lowered, ("отправ", "разошли", "рассыл")) or "пись" not in lowered:
            tool = "answer"
    elif tool == "new_project":
        if not _contains_any(lowered, ("запусти проект", "создай проект", "поручи команде")):
            tool = "answer"
    elif tool == "update_team":
        if not _contains_any(lowered, ("добав", "удал", "обнов")) or "команд" not in lowered:
            tool = "answer"
    elif tool == "save_note" and not _contains_any(lowered, ("сохрани", "запиши", "замет")):
        tool = "answer"

    if tool == "answer":
        params = {"question": text}
    elif tool in {"add_calendar_event", "change_calendar_event"}:
        params["text"] = text
    return {
        "tool": tool,
        "params": params,
        "confirmation_text": str((decision or {}).get("confirmation_text", "")),
    }
