from datetime import datetime

import calendar_agent
import orchestrator


def test_week_digest_mentions_empty_calendar():
    out = calendar_agent.format_week_digest([], now=datetime(2026, 7, 18, 8, 30))

    assert "НЕДЕЛЯ ВПЕРЁД" in out
    assert "Событий в календаре нет" in out


def test_week_digest_groups_events_by_day():
    events = [
        {
            "summary": "Встреча за чаем",
            "description": "[manually_added]",
            "start": {"dateTime": "2026-07-19T10:00:00+03:00"},
        }
    ]

    out = calendar_agent.format_week_digest(events, now=datetime(2026, 7, 18, 8, 30))

    assert "19.07" in out
    assert "10:00 — Встреча за чаем" in out
    assert "🔒" in out


def test_event_list_is_numbered_for_telegram_edits():
    events = [
        {
            "summary": "Встреча за чаем",
            "description": "[manually_added]",
            "start": {"dateTime": "2026-07-19T10:00:00+03:00"},
        }
    ]

    out = calendar_agent.format_event_list(events, now=datetime(2026, 7, 18, 8, 30))

    assert "1. 19.07.2026 10:00 — Встреча за чаем" in out
    assert "перенеси событие 1" in out


def test_orchestrator_routes_plain_text_calendar_add_without_llm():
    decision = orchestrator.orchestrate("добавь встречу за чаем завтра в 10 утра", [])

    assert decision["tool"] == "add_calendar_event"
    assert "встречу за чаем" in decision["params"]["text"]


def test_orchestrator_routes_calendar_change_with_confirmation():
    decision = orchestrator.orchestrate("перенеси событие 1 на завтра в 12:00", [])

    assert decision["tool"] == "change_calendar_event"
    assert decision["params"]["text"].startswith("перенеси")


def test_calendar_change_preview_updates_numbered_event(monkeypatch):
    events = [
        {
            "id": "evt_1",
            "summary": "Встреча за чаем",
            "description": "[manually_added]",
            "start": {"dateTime": "2026-07-19T10:00:00+03:00"},
            "end": {"dateTime": "2026-07-19T11:00:00+03:00"},
        }
    ]
    monkeypatch.setattr(calendar_agent, "get_upcoming_events", lambda days=30: events)
    monkeypatch.setattr(
        calendar_agent,
        "parse_calendar_change_request",
        lambda text, current: {
            "ok": True,
            "action": "update",
            "event_ref": 1,
            "date": "2026-07-20",
            "time_start": "12:00",
        },
    )

    result = calendar_agent.apply_calendar_change_from_text("перенеси событие 1", dry_run=True)

    assert result["ok"] is True
    assert "Было: Встреча за чаем — 19.07.2026 10:00" in result["message"]
    assert "Станет: Встреча за чаем — 20.07.2026 12:00" in result["message"]


def test_calendar_change_plan_applies_without_reparsing(monkeypatch):
    calls = {}

    def fake_update(event_id, **kwargs):
        calls["event_id"] = event_id
        calls["kwargs"] = kwargs

    monkeypatch.setattr(calendar_agent, "update_event", fake_update)
    monkeypatch.setattr(
        calendar_agent,
        "parse_calendar_change_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not reparse confirmed plan")),
    )
    plan = {
        "ok": True,
        "action": "update",
        "event_id": "evt_1",
        "old_title": "Встреча за чаем",
        "old_start": "2026-07-19T10:00:00",
        "new_title": "Встреча за чаем",
        "new_start": "2026-07-20T15:00:00",
        "new_end": "2026-07-20T16:00:00",
        "location": "ОСерф",
        "description": "[manually_added]",
        "message": "Изменить событие",
    }

    result = calendar_agent.apply_calendar_change_plan(plan)

    assert result["ok"] is True
    assert calls["event_id"] == "evt_1"
    assert calls["kwargs"]["start"]["dateTime"] == "2026-07-20T15:00:00"
    assert calls["kwargs"]["location"] == "ОСерф"
