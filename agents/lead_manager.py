import os
import json
import runtime_bootstrap

runtime_bootstrap.ensure_isolated_runtime()

import requests
import time
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from memory import init_db

import db
import notify
import llm
import agent_contracts
import ops_store
from applog import get_logger
from retry import safe

load_dotenv()
log = get_logger("lead_manager")
SURVEY_ROW_RE = re.compile(r"\[survey_row=(\d+)\]")

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

def _weeek_ready() -> bool:
    ok, reason = agent_contracts.require_env("WEEEK_TOKEN", "WEEEK_STAGE_NEW")
    if not ok:
        log.warning(f"WEEEK disabled: {reason}")
        return False
    return True

def _split_contact_name(name: str) -> tuple[str, str | None]:
    first, *last = (name or "Лид Amori").split()
    return first, " ".join(last) if last else None

def _weeek_request(method: str, url: str, **kwargs) -> requests.Response:
    timeout = kwargs.pop("timeout", 20)
    last_error = None
    for attempt in range(3):
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
    raise last_error

def create_weeek_contact(name: str, email: str = None, phone: str = None) -> str:
    if not _weeek_ready():
        return None
    first, last = _split_contact_name(name)
    body = {"firstName": first, "lastName": last}
    if email:
        body["emails"] = [email]
    if phone:
        body["phones"] = [phone]
    try:
        r = _weeek_request(
            "POST",
            "https://api.weeek.net/public/v1/crm/contacts",
            headers=WEEEK_HEADERS,
            json=body,
        )
        data = r.json() if r.ok else {}
    except Exception as e:
        log.warning(f"WEEEK contact failed: {e}")
        return None
    if data.get("success"):
        return data["contact"]["id"]
    return None

def update_weeek_contact(contact_id: str, name: str, email: str = None, phone: str = None) -> dict:
    if not contact_id:
        return {"ok": False, "action": "skipped", "reason": "missing_contact_id"}
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    first, last = _split_contact_name(name)
    body = {"firstName": first, "lastName": last}
    if email:
        body["emails"] = [email]
    if phone:
        body["phones"] = [phone]
    try:
        r = _weeek_request(
            "PUT",
            f"https://api.weeek.net/public/v1/crm/contacts/{contact_id}",
            headers=WEEEK_HEADERS,
            json=body,
        )
        data = r.json() if r.ok else {}
        return {
            "ok": bool(data.get("success")),
            "action": "updated" if data.get("success") else "update_failed",
            "status_code": r.status_code,
        }
    except Exception as e:
        log.warning(f"WEEEK contact update failed: {e}")
        return {"ok": False, "action": "update_error"}

def create_weeek_deal(
    title: str,
    contact_id: str,
    stage: str = "new",
    amount: float = None,
    description: str = None,
) -> str:
    if not _weeek_ready():
        return None
    status_id = STAGES.get(stage, STAGES["new"])
    if not status_id:
        log.warning(f"WEEEK deal skipped: stage {stage} not configured")
        return None
    body = {"title": title, "statusId": status_id}
    if amount:
        body["amount"] = amount
    if description:
        body["description"] = description
    try:
        r = _weeek_request(
            "POST",
            f"https://api.weeek.net/public/v1/crm/statuses/{status_id}/deals",
            headers=WEEEK_HEADERS,
            json=body,
        )
        data = r.json() if r.ok else {}
    except Exception as e:
        log.warning(f"WEEEK deal failed: {e}")
        return None
    if data.get("success"):
        deal_id = data["deal"]["id"]
        # Привязываем контакт к сделке
        if contact_id:
            try:
                _weeek_request(
                    "POST",
                    f"https://api.weeek.net/public/v1/crm/deals/{deal_id}/contacts",
                    headers=WEEEK_HEADERS,
                    json={"contactId": contact_id},
                )
            except Exception as e:
                log.warning(f"WEEEK link contact failed: {e}")
        return deal_id
    return None

def update_weeek_deal_details(deal_id: str, title: str = None, description: str = None, stage: str = None) -> dict:
    if not deal_id:
        return {"ok": False, "action": "skipped", "reason": "missing_deal_id"}
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    body = {}
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if stage:
        status_id = STAGES.get(stage)
        if status_id:
            body["statusId"] = status_id
    if not body:
        return {"ok": False, "action": "skipped", "reason": "empty_body"}
    try:
        r = _weeek_request(
            "PUT",
            f"https://api.weeek.net/public/v1/crm/deals/{deal_id}",
            headers=WEEEK_HEADERS,
            json=body,
        )
        data = r.json() if r.ok else {}
        return {
            "ok": bool(data.get("success")),
            "action": "updated" if data.get("success") else "update_failed",
            "status_code": r.status_code,
        }
    except Exception as e:
        log.warning(f"WEEEK deal update failed: {e}")
        return {"ok": False, "action": "update_error"}

def get_weeek_deal(deal_id: str) -> dict | None:
    if not deal_id:
        return None
    if not _weeek_ready():
        return None
    try:
        r = _weeek_request(
            "GET",
            f"https://api.weeek.net/public/v1/crm/deals/{deal_id}",
            headers=WEEEK_HEADERS,
        )
        data = r.json() if r.ok else {}
        return data.get("deal") if data.get("success") else None
    except Exception as e:
        log.warning(f"WEEEK deal fetch failed: {e}")
        return None

def create_weeek_deal_task(deal_id: str, title: str, description: str) -> dict:
    if not deal_id:
        return {"ok": False, "action": "skipped", "reason": "missing_deal_id"}
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    try:
        r = _weeek_request(
            "POST",
            f"https://api.weeek.net/public/v1/crm/deals/{deal_id}/tasks",
            headers=WEEEK_HEADERS,
            json={"title": title, "description": description},
        )
        data = r.json() if r.ok else {}
        return {
            "ok": bool(data.get("success")),
            "action": "created" if data.get("success") else "create_failed",
            "task_id": (data.get("task") or {}).get("id"),
            "status_code": r.status_code,
        }
    except Exception as e:
        log.warning(f"WEEEK deal task create failed: {e}")
        return {"ok": False, "action": "create_error"}

def update_weeek_deal_task(deal_id: str, task_id: int, title: str, description: str) -> dict:
    if not deal_id or not task_id:
        return {"ok": False, "action": "skipped", "reason": "missing_task"}
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    try:
        r = _weeek_request(
            "PUT",
            f"https://api.weeek.net/public/v1/crm/deals/{deal_id}/tasks/{task_id}",
            headers=WEEEK_HEADERS,
            json={"title": title, "description": description},
        )
        data = r.json() if r.ok else {}
        return {
            "ok": bool(data.get("success")),
            "action": "updated" if data.get("success") else "update_failed",
            "task_id": task_id,
            "status_code": r.status_code,
        }
    except Exception as e:
        log.warning(f"WEEEK deal task update failed: {e}")
        return {"ok": False, "action": "update_error", "task_id": task_id}

def delete_weeek_deal_task(deal_id: str, task_id: int) -> dict:
    if not deal_id or not task_id:
        return {"ok": False, "action": "skipped", "reason": "missing_task"}
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    try:
        r = _weeek_request(
            "DELETE",
            f"https://api.weeek.net/public/v1/crm/deals/{deal_id}/tasks/{task_id}",
            headers=WEEEK_HEADERS,
        )
        data = r.json() if r.ok and r.text else {}
        return {
            "ok": r.status_code in (200, 202, 204) or bool(data.get("success")),
            "action": "deleted",
            "task_id": task_id,
            "status_code": r.status_code,
        }
    except Exception as e:
        log.warning(f"WEEEK deal task delete failed: {e}")
        return {"ok": False, "action": "delete_error", "task_id": task_id}

def upsert_weeek_deal_task(deal_id: str, title: str, description: str, marker: str = "КАРТОЧКА ЛИДА AMORI") -> dict:
    deal = get_weeek_deal(deal_id)
    if deal is None:
        return {"ok": False, "action": "skipped", "reason": "deal_fetch_failed"}
    matches = []
    for task in deal.get("tasks") or []:
        task_description = task.get("description") or ""
        task_title = task.get("title") or ""
        if task.get("isDeleted"):
            continue
        if marker in task_description or task_title == title:
            matches.append(task)
    if matches:
        result = create_weeek_deal_task(deal_id, title, description)
        if result.get("ok"):
            removed = 0
            for old_task in matches:
                delete_result = delete_weeek_deal_task(deal_id, old_task.get("id"))
                if delete_result.get("ok"):
                    removed += 1
            result["action"] = "replaced"
            result["duplicates_removed"] = removed
            return result
        removed = 0
        for duplicate in matches[1:]:
            delete_result = delete_weeek_deal_task(deal_id, duplicate.get("id"))
            if delete_result.get("ok"):
                removed += 1
        result["action"] = "kept_existing"
        result["duplicates_removed"] = removed
        result["ok"] = True
        return result
    return create_weeek_deal_task(deal_id, title, description)

def update_deal_stage(deal_id: str, new_stage: str):
    if not _weeek_ready():
        return False
    status_id = STAGES.get(new_stage)
    if not status_id:
        return False
    try:
        result = update_weeek_deal_details(deal_id, stage=new_stage)
        return result.get("ok", False)
    except Exception as e:
        log.warning(f"WEEEK stage update failed: {e}")
        return False

def _weeek_delete(path: str, label: str) -> dict:
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    try:
        r = requests.delete(
            f"https://api.weeek.net/public/v1/{path.lstrip('/')}",
            headers=WEEEK_HEADERS,
            timeout=12,
        )
        if r.status_code in (200, 202, 204):
            return {"ok": True, "action": "deleted", "status_code": r.status_code}
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if data.get("success"):
            return {"ok": True, "action": "deleted", "status_code": r.status_code}
        return {
            "ok": False,
            "action": "delete_failed",
            "status_code": r.status_code,
            "label": label,
        }
    except Exception as e:
        log.warning(f"WEEEK {label} delete failed: {e}")
        return {"ok": False, "action": "delete_error", "label": label}

def mark_weeek_deal_test_removed(deal_id: str, title: str = None) -> dict:
    if not _weeek_ready():
        return {"ok": False, "action": "skipped", "reason": "weeek_not_configured"}
    status_id = STAGES.get("lost")
    if not status_id:
        return {"ok": False, "action": "skipped", "reason": "lost_stage_missing"}
    body = {"statusId": status_id}
    if title:
        body["title"] = f"[TEST REMOVED] {title}"[:180]
    try:
        r = requests.put(
            f"https://api.weeek.net/public/v1/crm/deals/{deal_id}",
            headers=WEEEK_HEADERS,
            json=body,
            timeout=12,
        )
        data = r.json() if r.ok else {}
        return {
            "ok": bool(data.get("success")),
            "action": "marked_lost" if data.get("success") else "mark_failed",
            "status_code": r.status_code,
        }
    except Exception as e:
        log.warning(f"WEEEK mark test deal failed: {e}")
        return {"ok": False, "action": "mark_error"}

def delete_weeek_deal(deal_id: str, title: str = None) -> dict:
    result = _weeek_delete(f"crm/deals/{deal_id}", "deal")
    if result.get("ok"):
        return result
    fallback = mark_weeek_deal_test_removed(deal_id, title)
    fallback["delete_attempt"] = result
    return fallback

def delete_weeek_contact(contact_id: str) -> dict:
    return _weeek_delete(f"crm/contacts/{contact_id}", "contact")

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
    if deal_id or contact_id:
        cur.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS weeek_deal_id VARCHAR(50)",
        )
        cur.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS weeek_contact_id VARCHAR(50)",
        )
    if deal_id:
        cur.execute(
            "UPDATE leads SET weeek_deal_id=%s WHERE id=%s",
            (deal_id, lead_id)
        )
    if contact_id:
        cur.execute(
            "UPDATE leads SET weeek_contact_id=%s WHERE id=%s",
            (contact_id, lead_id)
        )

    conn.commit()
    cur.close()
    conn.close()

    return {"id": lead_id, "weeek_deal_id": deal_id, "weeek_contact_id": contact_id}

def remove_test_lead(lead_id: int, reason: str = "[TEST REMOVED]") -> dict:
    """Remove a known test lead locally and best-effort clean linked WEEEK records."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, weeek_deal_id, weeek_contact_id
        FROM leads
        WHERE id=%s
    """, (lead_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return {"id": lead_id, "deleted": False, "reason": "not_found"}

    _, name, deal_id, contact_id = row
    weeek = {}
    if deal_id:
        weeek["deal"] = delete_weeek_deal(deal_id, f"{reason} {name or lead_id}")
    if contact_id:
        weeek["contact"] = delete_weeek_contact(contact_id)

    cur.execute("DELETE FROM leads WHERE id=%s", (lead_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"id": lead_id, "deleted": True, "weeek": weeek}

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

def _lead_contact_label(email: str = None, phone: str = None, telegram: str = None) -> str:
    if telegram:
        return telegram
    if phone:
        return phone
    if email:
        return email
    return ""

def _survey_row_from_notes(notes: str = None) -> str:
    match = SURVEY_ROW_RE.search(notes or "")
    return match.group(1) if match else ""

def format_lead_list_item(lead: tuple) -> str:
    lead_id, name, email, phone, telegram, source, pet_type, status, _stage, notes, *_ = lead
    contact = _lead_contact_label(email, phone, telegram)
    survey_row = _survey_row_from_notes(notes)
    generic = not name or name.startswith("Респондент анкеты #") or name.strip().lower() in {"телеграм", "telegram"}

    if generic and survey_row:
        title = f"Анкета #{survey_row}"
    else:
        title = name or "Лид без имени"

    if contact and contact not in title:
        title = f"{title} · {contact}"

    meta = " · ".join(item for item in [pet_type or "?", status or "?", source or ""] if item)
    return f"#{lead_id} {title}\n   {meta}"

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
    log.info("Lead follow-up запущен")
    due = get_followups_due()
    if not due:
        ops_store.heartbeat("lead_manager", "ok", {"mode": "followup", "due": 0})
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
    delivered = notify.send(msg)
    ops_store.heartbeat(
        "lead_manager",
        "ok" if delivered else "warn",
        {"mode": "followup", "due": len(due), "delivered": delivered},
    )
    log.info("Отчёт follow-up отправлен" if delivered else "Отчёт follow-up не доставлен")

def _ai_recommendation(stats: dict) -> str:
    """Краткая AI-рекомендация по лидам (2-3 действия). Не валит отчёт при сбое."""
    try:
        prompt = (
            "Ты — руководитель отдела продаж Amori (GPS-ошейники для питомцев). "
            "По срезу воронки дай 2-3 КОНКРЕТНЫХ следующих шага (по-русски, кратко, по пунктам). "
            f"Данные: {json.dumps(stats, ensure_ascii=False)}"
        )
        rec = llm.qwen_answer(prompt, system="Отвечай по делу, без воды, максимум 3 пункта.",
                              agent_key="lead_manager", max_tokens=400)
        return str(rec).strip()
    except Exception as e:
        log.warning(f"AI-рекомендация недоступна: {e}")
        return ""


def run_leads_report():
    """Ежедневный отчёт по лидам — расширенный: динамика, горячие, follow-up, зависшие,
    конверсия по источникам, b2b/b2c и AI-рекомендация."""
    log.info("Lead Manager запущен")
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM leads")
    total = cur.fetchone()[0]
    cur.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")
    by_status = dict(cur.fetchall())
    clients = by_status.get("client", 0)

    # Динамика неделя-к-неделе
    cur.execute("SELECT COUNT(*) FROM leads WHERE created_at >= NOW() - INTERVAL '7 days'")
    new_week = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM leads WHERE created_at >= NOW() - INTERVAL '14 days' "
                "AND created_at < NOW() - INTERVAL '7 days'")
    prev_week = cur.fetchone()[0]

    # b2c / b2b
    cur.execute("SELECT lead_type, COUNT(*) FROM leads GROUP BY lead_type")
    by_type = dict(cur.fetchall())

    # Источники с конверсией в клиента
    cur.execute("""SELECT source, COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE status='client') AS won
                   FROM leads GROUP BY source ORDER BY total DESC""")
    src_rows = cur.fetchall()

    # Свежие лиды (по именам)
    cur.execute("""SELECT name, source, status, pet_type,
                          EXTRACT(DAY FROM NOW()-created_at)::int
                   FROM leads ORDER BY created_at DESC LIMIT 5""")
    recent = cur.fetchall()

    # Follow-up сегодня
    cur.execute("""SELECT name, status, telegram_username, phone FROM leads
                   WHERE next_followup_at <= NOW() AND status NOT IN ('client','lost')
                   ORDER BY next_followup_at LIMIT 8""")
    followups = cur.fetchall()

    # Зависшие: без контакта >7 дней, ещё в работе
    cur.execute("""SELECT name, status, EXTRACT(DAY FROM NOW()-COALESCE(last_contact_at,created_at))::int AS days
                   FROM leads
                   WHERE status IN ('new','contacted','qualified','proposal')
                     AND COALESCE(last_contact_at, created_at) < NOW() - INTERVAL '7 days'
                   ORDER BY days DESC LIMIT 8""")
    stuck = cur.fetchall()

    cur.close()
    conn.close()

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    conversion = round(clients / total * 100, 1) if total > 0 else 0
    trend = "📈" if new_week > prev_week else ("📉" if new_week < prev_week else "➡️")

    msg = f"📊 Отчёт по лидам | {now_str}\n\n"
    msg += "━━━ ОБЩАЯ КАРТИНА ━━━\n"
    msg += f"Всего лидов: {total}\n"
    msg += f"Новых за неделю: {new_week} {trend} (было {prev_week})\n"
    msg += f"Клиентов: {clients} ({conversion}%)\n"
    if by_type:
        msg += "Тип: " + " · ".join(f"{k or '?'}: {v}" for k, v in by_type.items()) + "\n"

    msg += "\n━━━ ВОРОНКА ━━━\n"
    stage_names = {"new": "🆕 Новые", "contacted": "📞 Связались",
                   "qualified": "✅ Квалифицированы", "proposal": "📄 Предложение",
                   "client": "🏆 Клиенты", "lost": "❌ Отказы"}
    for stage, label in stage_names.items():
        count = by_status.get(stage, 0)
        if count > 0:
            msg += f"{label}: {count}\n"

    if recent:
        msg += "\n━━━ 🔥 СВЕЖИЕ ЛИДЫ ━━━\n"
        for name, source, status, pet, days in recent:
            ago = "сегодня" if days == 0 else f"{days}д назад"
            extra = f" · {pet}" if pet else ""
            msg += f"  • {name} ({status}, {source or '?'}{extra}) — {ago}\n"

    if followups:
        msg += "\n━━━ ⏰ FOLLOW-UP СЕГОДНЯ ━━━\n"
        for name, status, tg, phone in followups:
            contact = (f"@{tg}" if tg else None) or phone or "нет контакта"
            msg += f"  • {name} ({status}) — {contact}\n"

    if stuck:
        msg += "\n━━━ 😴 ЗАВИСШИЕ (>7 дней) ━━━\n"
        for name, status, days in stuck:
            msg += f"  • {name} ({status}) — {days}д без контакта\n"

    if src_rows:
        msg += "\n━━━ ИСТОЧНИКИ (конверсия) ━━━\n"
        for source, stotal, won in src_rows[:6]:
            conv = round(won / stotal * 100) if stotal else 0
            msg += f"  {source or 'не указан'}: {stotal} → {won} клиент(ов) ({conv}%)\n"

    rec = _ai_recommendation({
        "total": total, "clients": clients, "conversion_pct": conversion,
        "new_week": new_week, "prev_week": prev_week, "funnel": by_status,
        "followups_due": len(followups), "stuck": len(stuck),
    })
    if rec:
        msg += f"\n━━━ 💡 РЕКОМЕНДАЦИЯ ━━━\n{rec}\n"

    delivered = notify.send(msg)
    ops_store.heartbeat(
        "lead_manager",
        "ok" if delivered else "warn",
        {"mode": "report", "delivered": delivered},
    )
    log.info("Отчёт по лидам отправлен" if delivered else "Отчёт по лидам не доставлен")

if __name__ == "__main__":
    import sys
    if not db.wait_ready("agents") or not db.wait_ready("customer_db"):
        raise RuntimeError("Postgres is unavailable; scheduler will retry lead manager")
    init_db()
    if len(sys.argv) > 1:
        if sys.argv[1] == "report":
            run_leads_report()
        elif sys.argv[1] == "followup":
            run_followup_check()
    else:
        run_leads_report()
