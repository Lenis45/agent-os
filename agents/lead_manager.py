import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from memory import init_db

import db
import notify
import llm
from applog import get_logger
from retry import safe

load_dotenv()
init_db()
log = get_logger("lead_manager")

def get_db():
    """Клиентский контур (152-ФЗ): лиды — в отдельной БД customer_db."""
    return db.connect("customer_db")

WEEEK_HEADERS = {
    "Authorization": f"Bearer {os.getenv('WEEEK_TOKEN')}",
    "Content-Type": "application/json"
}

STAGES = {
    "new":       os.getenv("WEEEK_STAGE_NEW"),
    "contacted": os.getenv("WEEEK_STAGE_CONTACTED"),
    "qualified": os.getenv("WEEEK_STAGE_QUALIFIED"),
    "proposal":  os.getenv("WEEEK_STAGE_PROPOSAL"),
    "client":    os.getenv("WEEEK_STAGE_CLIENT"),
    "lost":      os.getenv("WEEEK_STAGE_LOST"),
}

# ===== WEEEK CRM =====

def create_weeek_contact(name: str, email: str = None, phone: str = None) -> str:
    first, *last = name.split()
    body = {"firstName": first, "lastName": " ".join(last) if last else None}
    if email:
        body["emails"] = [email]
    if phone:
        body["phones"] = [phone]
    r = requests.post(
        "https://api.weeek.net/public/v1/crm/contacts",
        headers=WEEEK_HEADERS, json=body
    )
    data = r.json()
    if data.get("success"):
        return data["contact"]["id"]
    return None

def create_weeek_deal(title: str, contact_id: str, stage: str = "new", amount: float = None) -> str:
    status_id = STAGES.get(stage, STAGES["new"])
    body = {"title": title, "statusId": status_id}
    if amount:
        body["amount"] = amount
    r = requests.post(
        f"https://api.weeek.net/public/v1/crm/statuses/{status_id}/deals",
        headers=WEEEK_HEADERS, json=body
    )
    data = r.json()
    if data.get("success"):
        deal_id = data["deal"]["id"]
        # Привязываем контакт к сделке
        if contact_id:
            requests.post(
                f"https://api.weeek.net/public/v1/crm/deals/{deal_id}/contacts",
                headers=WEEEK_HEADERS,
                json={"contactId": contact_id}
            )
        return deal_id
    return None

def update_deal_stage(deal_id: str, new_stage: str):
    status_id = STAGES.get(new_stage)
    if not status_id:
        return False
    r = requests.put(
        f"https://api.weeek.net/public/v1/crm/deals/{deal_id}",
        headers=WEEEK_HEADERS,
        json={"statusId": status_id}
    )
    return r.json().get("success", False)

# ===== PostgreSQL =====

def add_lead(name: str, email: str = None, phone: str = None,
             telegram: str = None, source: str = None,
             pet_type: str = None, notes: str = None,
             lead_type: str = "b2c") -> dict:
    conn = get_db()
    cur = conn.cursor()

    # Создаём контакт в WEEEK
    contact_id = create_weeek_contact(name, email, phone)
    deal_id = None
    if contact_id:
        deal_id = create_weeek_deal(
            f"Лид: {name}" + (f" — {pet_type}" if pet_type else ""),
            contact_id, "new"
        )

    cur.execute("""
        INSERT INTO leads (name, email, phone, telegram_username, source,
                          pet_type, notes, lead_type, status,
                          last_contact_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'new',NOW())
        RETURNING id
    """, (name, email, phone, telegram, source, pet_type, notes, lead_type))

    lead_id = cur.fetchone()[0]

    # Сохраняем WEEEK IDs
    if deal_id:
        cur.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS weeek_deal_id VARCHAR(50)",
        )
        cur.execute(
            "UPDATE leads SET weeek_deal_id=%s WHERE id=%s",
            (deal_id, lead_id)
        )

    conn.commit()
    cur.close()
    conn.close()

    return {"id": lead_id, "weeek_deal_id": deal_id, "weeek_contact_id": contact_id}

def get_leads(status: str = None, limit: int = 20) -> list:
    conn = get_db()
    cur = conn.cursor()
    if status:
        cur.execute("""
            SELECT id, name, email, phone, telegram_username, source,
                   pet_type, status, stage, notes, last_contact_at,
                   next_followup_at, created_at
            FROM leads WHERE status=%s ORDER BY created_at DESC LIMIT %s
        """, (status, limit))
    else:
        cur.execute("""
            SELECT id, name, email, phone, telegram_username, source,
                   pet_type, status, stage, notes, last_contact_at,
                   next_followup_at, created_at
            FROM leads ORDER BY created_at DESC LIMIT %s
        """, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_followups_due() -> list:
    """Лиды которым нужен follow-up сегодня"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, email, phone, telegram_username, status, notes
        FROM leads
        WHERE next_followup_at <= NOW()
        AND status NOT IN ('won', 'lost')
        ORDER BY next_followup_at
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def parse_lead_from_text(text: str) -> dict:
    """Парсим лида из произвольного текста через LLM"""
    agent = llm.build_agent(
        "lead_manager",
        name="LeadParser",
        role="Парсер данных о лидах",
        goal="""Извлеки данные о потенциальном клиенте из текста.
Верни ТОЛЬКО JSON:
{
  "name": "Имя Фамилия",
  "email": "email или null",
  "phone": "телефон или null",
  "telegram": "@username или null",
  "source": "instagram/telegram/vk/referral/event/website/cold",
  "pet_type": "собака/кошка/etc или null",
  "notes": "дополнительная информация",
  "lead_type": "b2c или b2b"
}""",
    )
    result = llm.run(agent, f"Текст: {text}\nВерни только JSON.", "lead_manager")
    parsed = llm.parse_json(result)
    return parsed if isinstance(parsed, dict) else {"name": str(text)[:50], "notes": str(text)}

def run_followup_check():
    """Проверяем кому нужен follow-up"""
    due = get_followups_due()
    if not due:
        return

    now_str = datetime.now().strftime("%d.%m.%Y")
    msg = f"📋 Follow-up напоминания | {now_str}\n\n"

    for lead in due:
        lid, name, email, phone, tg, status, notes = lead
        msg += f"👤 {name}\n"
        if email: msg += f"  📧 {email}\n"
        if phone: msg += f"  📞 {phone}\n"
        if tg: msg += f"  💬 {tg}\n"
        msg += f"  Статус: {status}\n"
        if notes: msg += f"  Заметка: {notes[:100]}\n"
        msg += "\n"

    msg += f"Всего: {len(due)} лидов требуют внимания"
    notify.send(msg)

def run_leads_report():
    """Ежедневный отчёт по лидам"""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")
    by_status = dict(cur.fetchall())

    cur.execute("SELECT source, COUNT(*) FROM leads GROUP BY source ORDER BY count DESC")
    by_source = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM leads WHERE created_at >= NOW() - INTERVAL '7 days'")
    new_week = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM leads WHERE status='client'")
    clients = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM leads")
    total = cur.fetchone()[0]

    cur.close()
    conn.close()

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    conversion = round(clients / total * 100, 1) if total > 0 else 0

    msg = f"📊 Отчёт по лидам | {now_str}\n\n"
    msg += f"━━━ ОБЩАЯ КАРТИНА ━━━\n"
    msg += f"Всего лидов: {total}\n"
    msg += f"Новых за неделю: {new_week}\n"
    msg += f"Клиентов: {clients} ({conversion}%)\n\n"

    msg += f"━━━ ВОРОНКА ━━━\n"
    stage_names = {
        "new": "🆕 Новые",
        "contacted": "📞 Связались",
        "qualified": "✅ Квалифицированы",
        "proposal": "📄 Предложение",
        "client": "🏆 Клиенты",
        "lost": "❌ Отказы"
    }
    for stage, label in stage_names.items():
        count = by_status.get(stage, 0)
        if count > 0:
            msg += f"{label}: {count}\n"

    if by_source:
        msg += f"\n━━━ ИСТОЧНИКИ ━━━\n"
        for source, count in by_source[:5]:
            msg += f"  {source or 'не указан'}: {count}\n"

    notify.send(msg)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "report":
            run_leads_report()
        elif sys.argv[1] == "followup":
            run_followup_check()
    else:
        run_leads_report()
