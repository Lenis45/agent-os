"""Small Telegram text formatter for agent digests.

Telegram notifications are sent as plain text. LLMs often return Markdown anyway,
so this module removes raw Markdown artifacts and keeps the message readable.
"""
from __future__ import annotations

import re


def _table_row_to_bullet(line: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [re.sub(r"\s+", " ", c) for c in cells if c.strip()]
    if not cells:
        return ""
    if all(set(c) <= {"-", ":", " "} for c in cells):
        return ""
    if len(cells) == 1:
        return f"• {cells[0]}"
    return f"• {cells[0]} — {'; '.join(cells[1:])}"


def normalize_plain_text(text: str, max_chars: int | None = None) -> str:
    """Normalize LLM/Markdown-ish output into readable Telegram plain text."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""

    s = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", s, flags=re.I)
    s = re.sub(r"</?final>", "", s, flags=re.I)
    s = re.sub(r"```(?:\w+)?\n?", "", s)
    s = s.replace("```", "")
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"__(.*?)__", r"\1", s, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)

    out: list[str] = []
    previous_blank = False
    for raw in s.splitlines():
        line = raw.strip()

        if not line:
            if out and not previous_blank:
                out.append("")
            previous_blank = True
            continue

        if re.fullmatch(r"[-*_]{3,}", line):
            if out and not previous_blank:
                out.append("")
            out.append("────────")
            previous_blank = False
            continue

        if line.startswith("|") and line.endswith("|"):
            converted = _table_row_to_bullet(line)
            if converted:
                out.append(converted)
                previous_blank = False
            continue

        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\*([^\s].*?)\*$", r"\1", line)
        line = re.sub(r"^_([^\s].*?)_$", r"\1", line)
        line = re.sub(r"^[-*]\s+", "• ", line)
        line = re.sub(r"^(\d+)\.\s+", r"\1. ", line)
        line = re.sub(r"\s{2,}", " ", line)

        if line:
            out.append(line)
            previous_blank = False

    result = "\n".join(out).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    if max_chars and len(result) > max_chars:
        cut = result[:max_chars].rstrip()
        boundary = max(cut.rfind("\n"), cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if boundary > max_chars * 0.55:
            result = cut[:boundary + 1].rstrip()
        else:
            result = cut.rstrip(" ,;:") + "…"
    return result
