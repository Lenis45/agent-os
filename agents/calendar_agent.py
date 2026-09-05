import os
import re
import asyncio
import argparse
import time
import runtime_bootstrap
from pathlib import Path

runtime_bootstrap.ensure_isolated_runtime()

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from googleapiclient.discovery import build
from telethon import TelegramClient
from telethon.tl.types import Message
import imaplib
import email
from email.header import decode_header
from memory import remember, recall, is_known, init_db

import notify
import llm
import ops_store
from calendar_auth import CALENDAR_SCOPES, is_invalid_grant, load_calendar_credentials
from applog import get_logger

load_dotenv()
log = get_logger("calendar_agent")

_HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = list(CALENDAR_SCOPES)
CREDS_FILE = os.path.join(_HERE, 'credentials.json')
TOKEN_FILE = os.path.join(_HERE, 'token.json')
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "Europe/Moscow")

tg = TelegramClient(
    'chief_session',
    int(os.getenv("TELEGRAM_API_ID")),
    os.getenv("TELEGRAM_API_HASH")
)


def calendar_error_guidance(error) -> str:
    text = str(error or "").lower()
    if is_invalid_grant(error) or any(marker in text for marker in ("expired", "revoked", "token.json не найден")):
        return "Нужна повторная авторизация Google Calendar и новый token.json."
    if any(marker in text for marker in ("ssl", "eof", "timeout", "timed out", "connection", "network")):
        return "Проверь VPN/сеть до oauth2.googleapis.com и www.googleapis.com; повторная авторизация не нужна."
    return "Проверь доступ к Google Calendar; повторную авторизацию делай только при ошибке OAuth-токена."


def _calendar_oauth_failure_key(error) -> str | None:
    if not is_invalid_grant(error):
        return None
    try:
        token_version = Path(TOKEN_FILE).stat().st_mtime_ns
    except OSError:
        token_version = "missing"
    return f"invalid_grant:{token_version}"


def notify_calendar_failure(error, now_str: str) -> bool:
    """Send one invalid_grant alert per token version; other failures stay visible."""
    failure_key = _calendar_oauth_failure_key(error)
    if failure_key:
        try:
            previous = ops_store.get_automation_state("calendar_oauth_alert", {}) or {}
            if previous.get("failure_key") == failure_key and previous.get("delivered"):
                log.info("Повторный invalid_grant для того же token.json: уведомление пропущено")
                return False
        except Exception as state_error:
            log.warning("Calendar alert state unavailable: %s", state_error)

    delivered = notify.send(
        f"📅 Calendar Agent | {now_str}\n"
        f"Не смог подключиться к Google Calendar: {str(error)[:300]}\n"
        "Автоматическое добавление/удаление событий пропущено. " + calendar_error_guidance(error),
        "warn",
    )
    if failure_key and delivered:
        try:
            ops_store.set_automation_state(
                "calendar_oauth_alert",
                {"failure_key": failure_key, "delivered": True, "sent_at": datetime.now().isoformat()},
            )
        except Exception as state_error:
            log.warning("Calendar alert state was not saved: %s", state_error)
    return delivered

def get_calendar_service():
    creds = load_calendar_credentials(Path(TOKEN_FILE), scopes=tuple(SCOPES))
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)

def _execute_google(request, attempts: int = 3):
    """Execute a Google API request with small retries for transient network EOF/TLS errors."""
    last_error = None
    for attempt in range(attempts):
        try:
            return request.execute()
        except Exception as e:
            last_error = e
            transient = any(
                marker in str(e).lower()
                for marker in ("eof", "ssl", "timed out", "timeout", "connection reset", "temporarily")
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(1.2 * (attempt + 1))
    raise last_error

def local_now() -> datetime:
    return datetime.now(ZoneInfo(CALENDAR_TIMEZONE)).replace(tzinfo=None)


def get_upcoming_events(days=7):
    service = get_calendar_service()
    now_utc = datetime.now(timezone.utc)
    now = now_utc.isoformat().replace("+00:00", "Z")
    end = (now_utc + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    result = _execute_google(service.events().list(
        calendarId='primary',
        timeMin=now, timeMax=end,
        maxResults=50, singleEvents=True,
        orderBy='startTime'
    ))
    return result.get('items', [])

def add_event(title, start_dt, end_dt, description="", manually_added=False, source="calendar_agent", location=""):
    service = get_calendar_service()
    desc = description or ""
    if manually_added:
        desc = "[manually_added]\n" + desc
    if source:
        desc = f"[source:{source}]\n" + desc
    event = {
        'summary': title,
        'description': desc,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
    }
    if location:
        event["location"] = location
    return _execute_google(service.events().insert(calendarId='primary', body=event))

def delete_event(event_id):
    service = get_calendar_service()
    _execute_google(service.events().delete(calendarId='primary', eventId=event_id))

def update_event(event_id, **kwargs):
    service = get_calendar_service()
    event = _execute_google(service.events().get(calendarId='primary', eventId=event_id))
    event.update(kwargs)
    return _execute_google(service.events().update(calendarId='primary', eventId=event_id, body=event))

def is_manually_added(event):
    desc = event.get('description', '') or ''
    return '[manually_added]' in desc


def _event_start_raw(event: dict) -> str:
    return event.get('start', {}).get('dateTime', event.get('start', {}).get('date', '')) or ''


def _parse_event_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo:
            parsed = parsed.astimezone(ZoneInfo(CALENDAR_TIMEZONE))
        return parsed.replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None


def format_event_time(event: dict) -> str:
    raw = _event_start_raw(event)
    if not raw:
        return "время не указано"
    parsed = _parse_event_dt(raw)
    if not parsed:
        return raw[:16].replace("T", " ")
    if "T" not in raw:
        return parsed.strftime("%d.%m.%Y весь день")
    return parsed.strftime("%d.%m.%Y %H:%M")


def format_week_digest(events: list[dict], days: int = 7, now: datetime | None = None) -> str:
    """Human-readable week-ahead calendar block for Telegram digests."""
    now = now or local_now()
    end = now + timedelta(days=days)
    relevant = []
    for event in events:
        start = _parse_event_dt(_event_start_raw(event))
        if start is None or start < now.replace(hour=0, minute=0, second=0, microsecond=0) or start > end:
            continue
        relevant.append((start, event))
    relevant.sort(key=lambda item: item[0])

    if not relevant:
        return f"📆 НЕДЕЛЯ ВПЕРЁД ({now:%d.%m}–{end:%d.%m})\nСобытий в календаре нет."

    lines = [f"📆 НЕДЕЛЯ ВПЕРЁД ({now:%d.%m}–{end:%d.%m})"]
    weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
    current_day = None
    for start, event in relevant:
        day = f"{start:%d.%m}, {weekdays[start.weekday()]}"
        if day != current_day:
            current_day = day
            lines.append(f"\n{day}")
        title = event.get('summary') or 'Без названия'
        marker = " 🔒" if is_manually_added(event) else ""
        if "T" in _event_start_raw(event):
            lines.append(f"• {start:%H:%M} — {title}{marker}")
        else:
            lines.append(f"• весь день — {title}{marker}")
    return "\n".join(lines)


def format_today_digest(events: list[dict], now: datetime | None = None) -> str:
    """Compact list of today's meetings for the 08:00 Telegram message."""
    now = now or local_now()
    relevant = []
    for event in events:
        start = _parse_event_dt(_event_start_raw(event))
        if start is not None and start.date() == now.date():
            relevant.append((start, event))
    relevant.sort(key=lambda item: item[0])

    lines = [f"📅 ВСТРЕЧИ НА СЕГОДНЯ · {now:%d.%m.%Y}"]
    if not relevant:
        lines.append("В календаре встреч нет.")
        return "\n".join(lines)

    for start, event in relevant:
        title = event.get("summary") or "Без названия"
        location = (event.get("location") or "").strip()
        marker = " 🔒" if is_manually_added(event) else ""
        time_text = start.strftime("%H:%M") if "T" in _event_start_raw(event) else "весь день"
        line = f"• {time_text} — {title}{marker}"
        if location:
            line += f" · {location}"
        lines.append(line)
    return "\n".join(lines)


def morning_digest_delivered(now: datetime | None = None) -> bool:
    """Return True after today's scheduled report was delivered successfully."""
    now = now or local_now()
    try:
        state = ops_store.get_automation_state("calendar_morning_digest", {}) or {}
        return state.get("date") == now.strftime("%Y-%m-%d") and bool(state.get("delivered"))
    except Exception:
        return False


def remember_morning_digest(now: datetime | None = None) -> None:
    now = now or local_now()
    try:
        ops_store.set_automation_state(
            "calendar_morning_digest",
            {"date": now.strftime("%Y-%m-%d"), "delivered": True},
        )
    except Exception as exc:
        log.warning("Morning digest state was not saved: %s", exc)


def sorted_relevant_events(events: list[dict], days: int = 30, now: datetime | None = None) -> list[dict]:
    """Events sorted by start time with a stable 1-based ref for Telegram commands."""
    now = now or local_now()
    end = now + timedelta(days=days)
    relevant = []
    for event in events:
        start = _parse_event_dt(_event_start_raw(event))
        if start is None:
            continue
        if now.replace(hour=0, minute=0, second=0, microsecond=0) <= start <= end:
            relevant.append((start, event))
    relevant.sort(key=lambda item: item[0])
    return [event for _, event in relevant]


def format_event_list(events: list[dict], days: int = 30, now: datetime | None = None) -> str:
    """Detailed numbered event list for checking and editing through Emilia."""
    now = now or local_now()
    relevant = sorted_relevant_events(events, days=days, now=now)
    if not relevant:
        return f"📆 Календарь на {days} дней вперёд\nСобытий нет."

    lines = [f"📆 Календарь на {days} дней вперёд"]
    for index, event in enumerate(relevant, start=1):
        title = event.get("summary") or "Без названия"
        marker = " 🔒" if is_manually_added(event) else ""
        lines.append(f"{index}. {format_event_time(event)} — {title}{marker}")
    lines.append("\nЧтобы исправить: «перенеси событие 1 на завтра 12:00», «переименуй событие 1 в ...», «удали событие 1».")
    return "\n".join(lines)


def _select_event(events: list[dict], event_ref=None, title: str = "") -> dict | None:
    relevant = sorted_relevant_events(events, days=30)
    if event_ref:
        try:
            index = int(str(event_ref).strip())
            if 1 <= index <= len(relevant):
                return relevant[index - 1]
        except ValueError:
            pass
    title_norm = re.sub(r"\s+", " ", (title or "").lower()).strip()
    if title_norm:
        matches = []
        for event in relevant:
            event_title = re.sub(r"\s+", " ", (event.get("summary") or "").lower()).strip()
            if title_norm == event_title or title_norm in event_title or event_title in title_norm:
                matches.append(event)
        if len(matches) == 1:
            return matches[0]
    return None


def _looks_duplicate(existing_events: list[dict], title: str, start_dt: datetime) -> bool:
    title_norm = re.sub(r"\s+", " ", (title or "").lower()).strip()
    for event in existing_events:
        event_title = re.sub(r"\s+", " ", (event.get("summary") or "").lower()).strip()
        event_start = _parse_event_dt(_event_start_raw(event))
        if not event_start:
            continue
        if title_norm and (title_norm == event_title or title_norm in event_title or event_title in title_norm):
            if abs((event_start - start_dt).total_seconds()) < 30 * 60:
                return True
    return False


def parse_event_request(text: str, now: datetime | None = None) -> dict:
    """Parse a natural-language event request into a safe JSON calendar payload."""
    now = now or local_now()
    agent = llm.build_agent(
        "calendar_event_parser",
        name="CalendarEventParser",
        role="Парсер личного календаря",
        goal=f"""Преобразуй просьбу Дениса в строгое событие Google Calendar.
Сейчас: {now:%Y-%m-%d %H:%M}. Часовой пояс: {CALENDAR_TIMEZONE}.

Правила:
- Если дата или время неоднозначны — верни ok=false и уточняющий question.
- Если длительность не указана — ставь 60 минут.
- Не добавляй события без точной даты и времени.
- Название делай коротким и человеческим.
- Если в сообщении есть место ("в ОСерфе", "на Красном Октябре") — заполни location.

Верни ТОЛЬКО JSON:
{{
  "ok": true,
  "title": "Встреча за чаем",
  "date": "2026-07-19",
  "time_start": "10:00",
  "time_end": "11:00",
  "location": "ОСерф",
  "description": "Создано из сообщения Emilia: ...",
  "question": ""
}}""",
    )
    prompt = f"Сообщение Дениса: {text}\nВерни только JSON."
    result = llm.run(agent, prompt, "calendar_agent")
    data = llm.parse_json(result)
    if not isinstance(data, dict):
        return {"ok": False, "question": "Не смог разобрать дату и время. Напиши, пожалуйста: что, дата и время."}
    return data


def create_manual_event_from_text(text: str, source: str = "emilia") -> dict:
    """Create one explicit user-requested event and return a Telegram-ready result."""
    data = parse_event_request(text)
    if not data.get("ok"):
        return {
            "ok": False,
            "message": data.get("question") or "Не хватает точной даты/времени для календаря.",
        }

    title = str(data.get("title") or "").strip()
    date = str(data.get("date") or "").strip()
    time_start = str(data.get("time_start") or "").strip()
    time_end = str(data.get("time_end") or "").strip()
    if not title or not date or not time_start or not time_end:
        return {"ok": False, "message": "Не хватает названия, даты или времени. Напиши событие в формате: что, дата, время."}

    try:
        start_dt = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date} {time_end}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {"ok": False, "message": "Дата/время разобрались некорректно. Пример: 2026-07-19 10:00."}
    if end_dt <= start_dt:
        return {"ok": False, "message": "Время окончания должно быть позже начала."}

    try:
        existing = get_upcoming_events(days=30)
    except Exception as e:
        return {
            "ok": False,
            "message": (
                "Не смог подключиться к Google Calendar, событие не добавлено. "
                f"Причина: {str(e)[:220]}. {calendar_error_guidance(e)}"
            ),
        }
    if _looks_duplicate(existing, title, start_dt):
        return {"ok": True, "duplicate": True, "message": f"Похоже, это уже есть в календаре: {title} — {start_dt:%d.%m.%Y %H:%M}"}

    description = str(data.get("description") or "").strip()
    location = str(data.get("location") or "").strip()
    try:
        event = add_event(title, start_dt, end_dt, description, manually_added=True, source=source, location=location)
    except Exception as e:
        return {
            "ok": False,
            "message": (
                "Не смог записать событие в Google Calendar. "
                f"Причина: {str(e)[:220]}. {calendar_error_guidance(e)}"
            ),
        }
    remember(
        f"calendar_manual_{title}_{date}_{time_start}",
        "calendar_event",
        "telegram",
        "calendar_agent",
        {"date": date, "time_start": time_start, "title": title, "source": source, "location": location},
    )
    return {
        "ok": True,
        "event": event,
        "message": f"✅ Добавил в календарь: {title} — {start_dt:%d.%m.%Y %H:%M}",
    }


def parse_calendar_change_request(text: str, events: list[dict], now: datetime | None = None) -> dict:
    """Parse an edit/delete calendar request against a numbered event list."""
    now = now or local_now()
    event_list = format_event_list(events, days=30, now=now)
    agent = llm.build_agent(
        "calendar_change_parser",
        name="CalendarChangeParser",
        role="Парсер правок календаря",
        goal=f"""Преобразуй просьбу Дениса в безопасную правку существующего события.
Сейчас: {now:%Y-%m-%d %H:%M}. Часовой пояс: {CALENDAR_TIMEZONE}.

ТЕКУЩИЕ СОБЫТИЯ:
{event_list}

Правила:
- Для удаления верни action=delete.
- Для переноса/изменения времени верни action=update и новые date/time_start/time_end.
- Для переименования верни new_title.
- Если Денис указал место встречи — заполни location.
- Если Денис ссылается на номер события, положи его в event_ref.
- Если непонятно какое событие менять — ok=false и question.
- Если длительность не указана при переносе — сохрани старую длительность, не выдумывай time_end.

Верни ТОЛЬКО JSON:
{{
  "ok": true,
  "action": "update",
  "event_ref": 1,
  "title_query": "",
  "new_title": "",
  "date": "2026-07-20",
  "time_start": "12:00",
  "time_end": "",
  "location": "",
  "description_append": "Изменено через Emilia",
  "question": ""
}}""",
    )
    result = llm.run(agent, f"Просьба Дениса: {text}\nВерни только JSON.", "calendar_agent")
    data = llm.parse_json(result)
    if not isinstance(data, dict):
        return {"ok": False, "question": "Не смог понять, какое событие и как нужно исправить."}
    return data


def plan_calendar_change_from_text(text: str) -> dict:
    """Build a concrete, reviewable calendar change plan from natural language."""
    try:
        events = get_upcoming_events(days=30)
    except Exception as e:
        return {
            "ok": False,
            "message": f"Не смог подключиться к Google Calendar: {str(e)[:220]}. {calendar_error_guidance(e)}",
        }

    data = parse_calendar_change_request(text, events)
    if not data.get("ok"):
        return {"ok": False, "message": data.get("question") or "Не понял, какое событие исправить."}

    event = _select_event(events, data.get("event_ref"), data.get("title_query") or data.get("title"))
    if not event:
        return {
            "ok": False,
            "message": "Не нашёл однозначное событие. Сначала напиши /calendar, затем используй номер: например «перенеси событие 1 на завтра 12:00».",
        }

    action = str(data.get("action") or "").lower()
    old_title = event.get("summary") or "Без названия"
    old_start = _parse_event_dt(_event_start_raw(event))
    old_end = _parse_event_dt(event.get("end", {}).get("dateTime", event.get("end", {}).get("date", "")))
    if not old_start:
        return {"ok": False, "message": "У выбранного события нет понятного времени начала, не буду менять вслепую."}
    if not old_end:
        old_end = old_start + timedelta(hours=1)
    old_duration = old_end - old_start

    if action == "delete":
        return {
            "ok": True,
            "action": "delete",
            "event_id": event["id"],
            "old_title": old_title,
            "old_start": old_start.isoformat(),
            "message": f"Удалить из календаря: {old_title} — {old_start:%d.%m.%Y %H:%M}",
        }

    if action != "update":
        return {"ok": False, "message": "Пока умею только перенести/переименовать/удалить событие."}

    new_title = str(data.get("new_title") or "").strip() or old_title
    date = str(data.get("date") or "").strip()
    time_start = str(data.get("time_start") or "").strip()
    time_end = str(data.get("time_end") or "").strip()

    new_start = old_start
    new_end = old_end
    if date or time_start:
        if not date:
            date = old_start.strftime("%Y-%m-%d")
        if not time_start:
            time_start = old_start.strftime("%H:%M")
        try:
            new_start = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
            if time_end:
                new_end = datetime.strptime(f"{date} {time_end}", "%Y-%m-%d %H:%M")
            else:
                new_end = new_start + old_duration
        except ValueError:
            return {"ok": False, "message": "Новое время разобралось некорректно. Пример: 2026-07-20 12:00."}
    if new_end <= new_start:
        return {"ok": False, "message": "Новое время окончания должно быть позже начала."}

    description = event.get("description") or ""
    append = str(data.get("description_append") or "").strip()
    if append:
        description = (description + "\n" if description else "") + f"[emilia_edit] {append}"
    location = str(data.get("location") or "").strip() or event.get("location") or ""
    return {
        "ok": True,
        "action": "update",
        "event_id": event["id"],
        "old_title": old_title,
        "old_start": old_start.isoformat(),
        "new_title": new_title,
        "new_start": new_start.isoformat(),
        "new_end": new_end.isoformat(),
        "location": location,
        "description": description,
        "message": (
            f"Изменить событие:\n"
            f"Было: {old_title} — {old_start:%d.%m.%Y %H:%M}\n"
            f"Станет: {new_title} — {new_start:%d.%m.%Y %H:%M}"
        ),
    }


def apply_calendar_change_plan(plan: dict) -> dict:
    """Apply a previously reviewed calendar change plan without reparsing user text."""
    if not isinstance(plan, dict) or not plan.get("ok"):
        return {"ok": False, "message": "Нет корректного плана изменения календаря."}
    action = plan.get("action")
    event_id = plan.get("event_id")
    if not event_id:
        return {"ok": False, "message": "В плане нет event_id, не буду менять календарь вслепую."}

    if action == "delete":
        delete_event(event_id)
        return {"ok": True, "message": f"🗑 Удалил из календаря: {plan.get('old_title')} — {format_event_time({'start': {'dateTime': plan.get('old_start', '')}})}"}

    if action != "update":
        return {"ok": False, "message": "Пока умею применять только update/delete."}

    updated = {
        "summary": plan.get("new_title") or plan.get("old_title") or "Без названия",
        "description": plan.get("description") or "",
        "start": {"dateTime": plan.get("new_start"), "timeZone": CALENDAR_TIMEZONE},
        "end": {"dateTime": plan.get("new_end"), "timeZone": CALENDAR_TIMEZONE},
    }
    if plan.get("location"):
        updated["location"] = plan.get("location")
    update_event(event_id, **updated)
    return {"ok": True, "message": f"✅ Обновил календарь:\n{plan.get('message')}"}


def apply_calendar_change_from_text(text: str, dry_run: bool = False) -> dict:
    """Update/delete an existing event from natural language. Mutations should be HITL-confirmed upstream."""
    plan = plan_calendar_change_from_text(text)
    if dry_run or not plan.get("ok"):
        return plan
    return apply_calendar_change_plan(plan)

def get_emails_for_events():
    emails_text = []
    accounts = [
        ("imap.gmail.com", os.getenv("GMAIL1_EMAIL"), os.getenv("GMAIL1_PASSWORD")),
        ("imap.gmail.com", os.getenv("GMAIL2_EMAIL"), os.getenv("GMAIL2_PASSWORD")),
        ("imap.yandex.ru", os.getenv("YANDEX_EMAIL"), os.getenv("YANDEX_PASSWORD")),
    ]
    for host, addr, pwd in accounts:
        if not addr or not pwd:
            continue
        try:
            mail = imaplib.IMAP4_SSL(host)
            mail.login(addr, pwd)
            mail.select("INBOX")
            since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
            _, data = mail.search(None, f'(SINCE "{since}")')
            for uid in data[0].split()[-20:]:
                _, msg_data = mail.fetch(uid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                subject = decode_header(msg.get("Subject", ""))[0][0]
                if isinstance(subject, bytes):
                    subject = subject.decode('utf-8', errors='ignore')
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode('utf-8', errors='ignore')[:500]
                            break
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')[:500]
                emails_text.append(f"Тема: {subject}\n{body}")
            mail.logout()
        except Exception as e:
            log.warning(f"Email error {addr}: {e}")
    return emails_text

async def collect_tg_messages(hours=48):
    await tg.start(phone=os.getenv("TELEGRAM_PHONE"))
    since = datetime.now() - timedelta(hours=hours)
    messages = []
    skip = {"Telegram", "BotFather"}
    async for dialog in tg.iter_dialogs():
        if dialog.is_channel or dialog.name in skip:
            continue
        async for msg in tg.iter_messages(dialog, limit=100):
            if not isinstance(msg, Message) or not msg.text:
                continue
            if msg.date.replace(tzinfo=None) < since:
                break
            text = msg.text.lower()
            if any(w in text for w in ['встреча', 'созвон', 'звонок', 'митинг', 'встретимся',
                                        'во сколько', 'в котором', 'завтра', 'послезавтра',
                                        'в понедельник', 'во вторник', 'в среду', 'в четверг',
                                        'в пятницу', 'conference', 'demo', 'call', 'meet']):
                sender = getattr(msg.sender, 'first_name', '?') if msg.sender else '?'
                messages.append({
                    "chat": dialog.name,
                    "sender": sender,
                    "text": msg.text[:500],
                    "time": msg.date.strftime("%d.%m %H:%M"),
                    "is_me": msg.out
                })
    return messages

async def run():
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    now = datetime.now()
    log.info(f"[{now_str}] Calendar Agent запущен...")

    # Получаем текущие события
    try:
        upcoming = get_upcoming_events(days=7)
    except Exception as e:
        log.error(f"Calendar unavailable: {e}")
        try:
            ops_store.heartbeat("calendar_agent", "warn", {
                "message": "Google Calendar недоступен. " + calendar_error_guidance(e),
                "error": str(e)[:200],
            })
        except Exception:
            pass
        notify_calendar_failure(e, now_str)
        return
    log.info(f"Событий в календаре: {len(upcoming)}")

    if morning_digest_delivered(now):
        log.info("Утренний календарный дайджест уже доставлен сегодня: повтор пропущен")
        try:
            ops_store.heartbeat("calendar_agent", "ok", {
                "message": "Повторный запуск пропущен: дайджест уже доставлен",
                "events": len(upcoming),
            })
        except Exception:
            pass
        return

    existing_titles = []
    for e in upcoming:
        title = e.get('summary', '')
        start = e.get('start', {}).get('dateTime', e.get('start', {}).get('date', ''))
        existing_titles.append(f"{title} | {start[:16]}")

    # Собираем данные
    tg_messages = await collect_tg_messages(hours=48)
    email_texts = get_emails_for_events()

    log.info(f"TG сообщений с упоминанием встреч: {len(tg_messages)}")
    log.info(f"Писем проверено: {len(email_texts)}")

    # Формируем контекст для анализа
    tg_text = "\n".join([f"[{m['time']}] {m['chat']}/{m['sender']}: {m['text']}" for m in tg_messages])
    email_text = "\n---\n".join(email_texts[:10])

    calendar_text = "\n".join(existing_titles) if existing_titles else "Календарь пуст"

    agent = llm.build_agent(
        "calendar_agent",
        name="CalendarManager",
        role="Персональный менеджер календаря",
        goal=f"""Ты управляешь календарём Дениса Колесникова — основателя стартапа Amori.
Сейчас: {now_str}

СУЩЕСТВУЮЩИЕ СОБЫТИЯ В КАЛЕНДАРЕ:
{calendar_text}

ПРАВИЛА:
1. Анализируй переписки и письма на предмет встреч и событий
2. Если встреча УЖЕ ЕСТЬ в календаре — не добавляй повторно
3. Созвоны и встречи которые Денис назначил сам → добавляй автоматически (action: add)
4. Внешние мероприятия → оценивай загрузку и релевантность (action: recommend_add или action: skip)
5. Если встреча отменилась в переписке → action: delete
6. НЕ ТРОГАЙ события с пометкой [manually_added]
7. Смотри на загрузку: если день уже забит → учитывай при рекомендациях

Верни ТОЛЬКО JSON:
{{
  "actions": [
    {{
      "action": "add",
      "title": "Созвон с Андреем",
      "date": "2026-06-01",
      "time_start": "15:00",
      "time_end": "16:00",
      "description": "Обсуждение роадмапа приложения. Участники: Андрей",
      "source": "telegram",
      "reason": "Денис написал 'созвонимся в пятницу в 15'"
    }},
    {{
      "action": "recommend_add",
      "title": "Demo Day Физтех",
      "date": "2026-06-05",
      "time_start": "14:00",
      "time_end": "17:00",
      "description": "Демо-день акселератора Физтех.Идея",
      "reason": "Релевантно для Amori, день свободен. Рекомендую посетить."
    }},
    {{
      "action": "skip",
      "title": "Какое-то нерелевантное событие",
      "reason": "Не связано с бизнесом или уже занято время"
    }}
  ],
  "summary": "Краткий итог что сделано"
}}""",
    )

    prompt = f"""Проанализируй переписки и письма, найди встречи и события.

TELEGRAM (последние 48ч, только сообщения с упоминанием встреч):
{tg_text[:3000] if tg_text else 'Упоминаний встреч не найдено'}

EMAIL (последние 3 дня):
{email_text[:2000] if email_text else 'Писем не найдено'}

Верни только JSON."""

    result = await asyncio.to_thread(lambda: llm.run(agent, prompt, "calendar_agent"))
    data = llm.parse_json(result)
    if not isinstance(data, dict):
        try:
            ops_store.heartbeat("calendar_agent", "warn", {
                "message": "LLM не вернул корректный план календаря",
            })
        except Exception:
            pass
        notify.send(f"📅 Calendar Agent {now_str}\nОшибка парсинга ответа.")
        return

    actions = data.get("actions", [])
    summary = data.get("summary", "")

    added = []
    recommended = []
    skipped = []
    deleted = []

    for action in actions:
        act = action.get("action")
        title = action.get("title", "")
        reason = action.get("reason", "")
        description = action.get("description", "")

        if act == "add":
            try:
                date = action.get("date")
                t_start = action.get("time_start")
                t_end = action.get("time_end")
                if not date or not t_start or not t_end:
                    recommended.append(f"❓ {title}\n   Не добавлено автоматически: нет точной даты/времени.")
                    continue
                start_dt = datetime.strptime(f"{date} {t_start}", "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(f"{date} {t_end}", "%Y-%m-%d %H:%M")
                if end_dt <= start_dt:
                    recommended.append(f"❓ {title}\n   Не добавлено автоматически: некорректный интервал времени.")
                    continue
                if not reason:
                    recommended.append(f"❓ {title}\n   Не добавлено автоматически: нет объяснения источника.")
                    continue

                # Проверяем не добавляли ли уже
                mem_key = f"calendar_{title}_{date}"
                if not is_known(mem_key):
                    add_event(title, start_dt, end_dt, description)
                    remember(mem_key, "calendar_event", "telegram", "calendar_agent",
                            {"date": date, "title": title})
                    added.append(f"✅ {title} — {date} {t_start}")
            except Exception as e:
                log.info(f"Add error: {e}")

        elif act == "recommend_add":
            recommended.append(f"❓ {title}\n   {reason}")

        elif act == "skip":
            skipped.append(f"⏭ {title}: {reason}")

        elif act == "delete":
            # Ищем в существующих событиях
            for e in upcoming:
                if title.lower() in e.get('summary', '').lower():
                    if not is_manually_added(e):
                        delete_event(e['id'])
                        deleted.append(f"🗑 {title}")

    # Формируем отчёт
    report = f"📅 Calendar Manager | {now_str}\n\n"

    if added:
        report += "✅ ДОБАВЛЕНО АВТОМАТИЧЕСКИ:\n" + "\n".join(added) + "\n\n"
    if deleted:
        report += "🗑 УДАЛЕНО (отменилось):\n" + "\n".join(deleted) + "\n\n"
    if recommended:
        report += "❓ РЕКОМЕНДУЮ ДОБАВИТЬ:\n" + "\n".join(recommended) + "\n\n"
    if not added and not deleted and not recommended:
        report += "📭 Новых событий для добавления не найдено\n\n"

    # Дайджест ближайших событий
    try:
        upcoming = get_upcoming_events(days=7)
    except Exception:
        pass
    report += format_today_digest(upcoming, now=now) + "\n\n"
    report += format_week_digest(upcoming, days=7, now=now) + "\n"

    delivered = notify.send(report)
    if delivered:
        remember_morning_digest(now)
    try:
        ops_store.heartbeat("calendar_agent", "ok", {
            "message": "Утренний календарь доставлен" if delivered else "Не удалось доставить календарь",
            "events": len(upcoming),
            "added": len(added),
            "delivered": delivered,
        })
    except Exception:
        pass
    log.info("Отчёт отправлен" if delivered else "Отчёт не доставлен")

if __name__ == "__main__":
    import db
    if not db.wait_ready("agents"):
        raise RuntimeError("Postgres is unavailable; scheduler will retry calendar agent")
    init_db()
    parser = argparse.ArgumentParser(description="Amori Calendar Manager")
    parser.add_argument("--add", help="Добавить событие из естественного текста")
    parser.add_argument("--digest", action="store_true", help="Отправить недельный дайджест календаря")
    args = parser.parse_args()
    if args.add:
        result = create_manual_event_from_text(args.add, source="cli")
        notify.send(f"📅 Calendar Manager\n{result['message']}", "ok" if result.get("ok") else "warn")
    elif args.digest:
        now_cli = local_now()
        events = get_upcoming_events(days=7)
        notify.send(
            f"📅 Calendar Manager | {now_cli:%d.%m.%Y %H:%M}\n\n"
            f"{format_today_digest(events, now=now_cli)}\n\n"
            f"{format_week_digest(events, now=now_cli)}"
        )
    else:
        asyncio.run(run())
