import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv
from memory import init_db

import db
import notify
import llm
from applog import get_logger

load_dotenv()
init_db()
log = get_logger("email_agent")

def get_db():
    """Клиентский контур (152-ФЗ): лиды — в отдельной БД customer_db."""
    return db.connect("customer_db")

GMAIL = os.getenv("GMAIL1_EMAIL")
GMAIL_PASSWORD = os.getenv("GMAIL1_PASSWORD")

def send_email(to: str, subject: str, body: str, from_name: str = "Denis | Amori") -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{GMAIL}>"
        msg["To"] = to

        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL, GMAIL_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.error(f"Email error: {e}")
        return False

def generate_email(lead_data: dict, email_type: str = "intro") -> dict:
    agent = llm.build_agent(
        "email_agent",
        name="EmailWriter",
        role="Копирайтер для стартапа Amori",
        goal="""Пишешь персонализированные письма от Дениса Колесникова — CEO Amori (умные GPS ошейники для домашних животных).

Стиль: дружелюбный, живой, не корпоративный. Короткие абзацы. Без воды.
Письмо должно ощущаться как личное, не шаблонное.
Верни JSON: {"subject": "тема письма", "body": "текст письма"}""",
    )

    name = lead_data.get("name", "")
    pet = lead_data.get("pet_type", "питомца")
    source = lead_data.get("source", "")
    notes = lead_data.get("notes", "")

    prompts = {
        "intro": f"""Напиши первое знакомственное письмо лиду.
Имя: {name}
Питомец: {pet}
Откуда пришёл: {source}
Заметки: {notes}

Цель: познакомиться, рассказать про Amori кратко (GPS ошейник для {pet}),
спросить что важно при выборе ошейника. Без продажи в лоб.
Верни только JSON.""",

        "followup": f"""Напиши follow-up письмо — мы писали раньше но не получили ответа.
Имя: {name}
Питомец: {pet}
Заметки: {notes}

Цель: мягко напомнить о себе, предложить ответить на вопросы.
Короткое, 3-4 предложения. Верни только JSON.""",

        "proposal": f"""Напиши письмо с предложением.
Имя: {name}
Питомец: {pet}
Заметки: {notes}

Цель: предложить попробовать Amori, указать ключевые преимущества для {pet},
добавить призыв к действию. Верни только JSON."""
    }

    result = llm.run(agent, prompts.get(email_type, prompts["intro"]), "email_agent")
    parsed = llm.parse_json(result)
    if isinstance(parsed, dict) and parsed.get("body"):
        return parsed
    return {"subject": f"Знакомство — Amori для вашего {pet}", "body": str(result)}

def send_to_lead(lead_id: int, email_type: str = "intro") -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT name, email, pet_type, source, notes, status FROM leads WHERE id=%s",
        (lead_id,)
    )
    lead = cur.fetchone()

    if not lead or not lead[1]:
        cur.close()
        conn.close()
        return False

    name, email, pet_type, source, notes, status = lead
    lead_data = {"name": name, "email": email, "pet_type": pet_type,
                 "source": source, "notes": notes}

    email_content = generate_email(lead_data, email_type)
    subject = email_content.get("subject", "")
    body = email_content.get("body", "")

    success = send_email(email, subject, body)

    if success:
        cur.execute("""
            UPDATE leads SET
                last_contact_at=NOW(),
                contact_count=contact_count+1,
                next_followup_at=NOW() + INTERVAL '3 days',
                status=CASE WHEN status='new' THEN 'contacted' ELSE status END
            WHERE id=%s
        """, (lead_id,))
        conn.commit()

        # Обновляем статус в WEEEK если есть deal_id
        cur.execute("SELECT weeek_deal_id FROM leads WHERE id=%s", (lead_id,))
        row = cur.fetchone()
        if row and row[0]:
            import requests
            headers = {
                "Authorization": f"Bearer {os.getenv('WEEEK_TOKEN')}",
                "Content-Type": "application/json"
            }
            requests.put(
                f"https://api.weeek.net/public/v1/crm/deals/{row[0]}",
                headers=headers,
                json={"statusId": os.getenv("WEEEK_STAGE_CONTACTED")}
            )

        notify.send(
            f"📧 Письмо отправлено\n"
            f"👤 {name} ({email})\n"
            f"📌 Тип: {email_type}\n"
            f"📋 Тема: {subject}"
        )

    cur.close()
    conn.close()
    return success

def send_bulk(email_type: str = "intro", status_filter: str = "new", limit: int = 10):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM leads WHERE status=%s AND email IS NOT NULL ORDER BY created_at LIMIT %s",
        (status_filter, limit)
    )
    lead_ids = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    sent = 0
    for lid in lead_ids:
        if send_to_lead(lid, email_type):
            sent += 1

    notify.send(f"📧 Рассылка завершена\nОтправлено: {sent}/{len(lead_ids)}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        lead_id = int(sys.argv[2])
        email_type = sys.argv[3] if len(sys.argv) > 3 else "intro"
        result = send_to_lead(lead_id, email_type)
        print("✅ Отправлено" if result else "❌ Ошибка")
    elif len(sys.argv) >= 2 and sys.argv[1] == "bulk":
        send_bulk()
    else:
        print("Использование:")
        print("  python3 email_agent.py send <lead_id> [intro|followup|proposal]")
        print("  python3 email_agent.py bulk")
