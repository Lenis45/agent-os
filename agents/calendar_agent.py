import os
import re
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from telethon import TelegramClient
from telethon.tl.types import Message
import imaplib
import email
from email.header import decode_header
from memory import remember, recall, is_known, init_db

import notify
import llm
from applog import get_logger

load_dotenv()
init_db()
log = get_logger("calendar_agent")

_HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDS_FILE = os.path.join(_HERE, 'credentials.json')
TOKEN_FILE = os.path.join(_HERE, 'token.json')

tg = TelegramClient(
    'chief_session',
    int(os.getenv("TELEGRAM_API_ID")),
    os.getenv("TELEGRAM_API_HASH")
)

def get_calendar_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)

def get_upcoming_events(days=7):
    service = get_calendar_service()
    now = datetime.utcnow().isoformat() + 'Z'
    end = (datetime.utcnow() + timedelta(days=days)).isoformat() + 'Z'
    result = service.events().list(
        calendarId='primary',
        timeMin=now, timeMax=end,
        maxResults=50, singleEvents=True,
        orderBy='startTime'
    ).execute()
    return result.get('items', [])

def add_event(title, start_dt, end_dt, description="", manually_added=False):
    service = get_calendar_service()
    desc = description
    if manually_added:
        desc = "[manually_added]\n" + description
    event = {
        'summary': title,
        'description': desc,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Europe/Riga'},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Europe/Riga'},
    }
    return service.events().insert(calendarId='primary', body=event).execute()

def delete_event(event_id):
    service = get_calendar_service()
    service.events().delete(calendarId='primary', eventId=event_id).execute()

def update_event(event_id, **kwargs):
    service = get_calendar_service()
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    event.update(kwargs)
    return service.events().update(calendarId='primary', eventId=event_id, body=event).execute()

def is_manually_added(event):
    desc = event.get('description', '') or ''
    return '[manually_added]' in desc

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
    upcoming = get_upcoming_events(days=7)
    log.info(f"Событий в календаре: {len(upcoming)}")

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

    result = llm.run(agent, prompt, "calendar_agent")
    data = llm.parse_json(result)
    if not isinstance(data, dict):
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
                date = action.get("date", now.strftime("%Y-%m-%d"))
                t_start = action.get("time_start", "10:00")
                t_end = action.get("time_end", "11:00")
                start_dt = datetime.strptime(f"{date} {t_start}", "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(f"{date} {t_end}", "%Y-%m-%d %H:%M")

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
    if upcoming:
        report += "📆 БЛИЖАЙШИЕ СОБЫТИЯ:\n"
        for e in upcoming[:5]:
            title_e = e.get('summary', 'Без названия')
            start_e = e.get('start', {}).get('dateTime', e.get('start', {}).get('date', ''))
            manually = " 🔒" if is_manually_added(e) else ""
            report += f"  • {title_e} — {start_e[:16]}{manually}\n"

    notify.send(report)
    log.info("Отчёт отправлен")

if __name__ == "__main__":
    asyncio.run(run())
