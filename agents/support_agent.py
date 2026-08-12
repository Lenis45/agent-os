import os
import json
import asyncio
import re
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import urllib.request
import concurrent.futures
from memory import init_db, remember, recall

import db
import llm
import agent_contracts
import notify
from applog import get_logger
from bot_commands import command_menu_text, set_application_commands
from telegram_format import normalize_plain_text
from telegram_runtime import install_error_handler

load_dotenv()
init_db()
log = get_logger("support_agent")

def get_db():
    """Клиентский контур (152-ФЗ): тикеты/сообщения поддержки — в customer_db."""
    return db.connect("customer_db")

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

SUPPORT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN")
MAIN_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DENIS_ID = os.getenv("TELEGRAM_MY_ID")
MAX_TELEGRAM_ANSWER_CHARS = 650

# ===== БАЗА ЗНАНИЙ О ПРОДУКТЕ =====
PRODUCT_KNOWLEDGE = """
Ты агент поддержки Amori — стартапа который разрабатывает умные GPS ошейники для домашних животных.

О ПРОДУКТЕ:
- Amori — стартап, который разрабатывает умный GPS-ошейник для домашних животных.
- Продукт находится в разработке; публичные параметры, цена, сроки запуска и точные функции ещё уточняются.
- Можно говорить о задаче продукта: помочь владельцу спокойнее ориентироваться, где находится питомец.
- Нельзя обещать real-time, точность геолокации, уведомления, геозоны, мониторинг здоровья/активности или готовое приложение, пока это не подтверждено командой.

КОНТАКТЫ:
- Поддержка: этот чат
- Сайт: в разработке

ПРАВИЛА ОБЩЕНИЯ:
- Отвечай только на русском языке
- Будь дружелюбным, спокойным и кратким
- Пиши как в Telegram-чате: 2–5 коротких предложений, без markdown, без **жирного**, без заголовков
- Если вопрос большой — сначала дай короткое резюме, потом один понятный следующий шаг
- Если не знаешь точного ответа — честно скажи и предложи соединить с командой
- Не выдумывай цены, функции или сроки если не уверен
- Собирай обратную связь и пожелания — они важны для развития продукта

ЧАСТЫЕ ВОПРОСЫ:
Q: Когда будет доступен ошейник?
A: Мы активно разрабатываем продукт. Оставьте свои контакты и мы уведомим вас первыми о запуске.

Q: Сколько будет стоить?
A: Ценообразование ещё формируется. Подпишитесь на уведомления чтобы узнать первым о старте продаж и получить специальные условия.

Q: Как работает GPS?
A: Мы проектируем GPS-функциональность, чтобы владельцу было проще ориентироваться с местоположением питомца. Точные параметры работы и приложения команда ещё подтверждает.

Q: Для каких животных подходит?
A: В первую очередь для собак и кошек. Ошейник будет доступен в разных размерах.

Q: Как долго держит заряд?
A: Этот параметр ещё уточняется в процессе разработки.

Если вопрос сложный или клиент хочет поговорить с командой — используй эскалацию.
"""

# ===== БАЗА ДАННЫХ ПОДДЕРЖКИ =====

def init_support_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id SERIAL PRIMARY KEY,
            customer_id VARCHAR(50),
            customer_name VARCHAR(200),
            customer_username VARCHAR(100),
            status VARCHAR(20) DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER REFERENCES support_tickets(id),
            role VARCHAR(20),
            content TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_faq (
            id SERIAL PRIMARY KEY,
            question TEXT,
            answer TEXT,
            use_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_or_create_ticket(customer_id: str, name: str, username: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM support_tickets WHERE customer_id=%s AND status='open' ORDER BY created_at DESC LIMIT 1",
        (customer_id,)
    )
    row = cur.fetchone()
    if row:
        tid = row[0]
    else:
        cur.execute(
            "INSERT INTO support_tickets (customer_id, customer_name, customer_username) VALUES (%s,%s,%s) RETURNING id",
            (customer_id, name, username)
        )
        tid = cur.fetchone()[0]
        conn.commit()
    cur.close()
    conn.close()
    return tid

def save_support_message(ticket_id: int, role: str, content: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO support_messages (ticket_id, role, content) VALUES (%s,%s,%s)",
        (ticket_id, role, content)
    )
    cur.execute("UPDATE support_tickets SET updated_at=NOW() WHERE id=%s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()

def get_ticket_history(ticket_id: int, limit: int = 10) -> list:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT role, content FROM support_messages
        WHERE ticket_id=%s ORDER BY created_at DESC LIMIT %s
    """, (ticket_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def get_ticket_info(ticket_id: int) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT customer_id, customer_name, customer_username, status FROM support_tickets WHERE id=%s",
        (ticket_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"customer_id": row[0], "name": row[1], "username": row[2], "status": row[3]}
    return {}

def get_latest_ticket_for_customer(customer_id: str) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, status, updated_at
        FROM support_tickets
        WHERE customer_id=%s
        ORDER BY updated_at DESC
        LIMIT 1
    """, (customer_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {}
    return {"id": row[0], "status": row[1], "updated_at": row[2]}

def escalate_ticket(ticket_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE support_tickets SET status='escalated' WHERE id=%s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()

def close_ticket(ticket_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE support_tickets SET status='closed' WHERE id=%s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()

def get_open_tickets() -> list:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, t.customer_name, t.customer_username, t.status,
               COUNT(m.id) as msg_count, MAX(m.created_at) as last_msg
        FROM support_tickets t
        LEFT JOIN support_messages m ON m.ticket_id=t.id
        WHERE t.status IN ('open','escalated')
        GROUP BY t.id ORDER BY last_msg DESC LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# ===== УВЕДОМЛЕНИЯ ДЕНИСУ =====

def notify_denis(ticket_id: int, customer_name: str, message: str, is_escalation: bool = False):
    token = MAIN_TOKEN
    chat_id = DENIS_ID

    if is_escalation:
        text = (
            f"🚨 ЭСКАЛАЦИЯ — Тикет #{ticket_id}\n"
            f"👤 Клиент: {customer_name}\n"
            f"💬 Последнее сообщение:\n{message[:300]}\n\n"
            f"Для ответа: /reply_{ticket_id} текст"
        )
    else:
        text = (
            f"📩 Новое сообщение — Тикет #{ticket_id}\n"
            f"👤 {customer_name}\n"
            f"💬 {message[:200]}"
        )

    notify.send(text)

def send_to_customer(customer_id: str, text: str):
    token = SUPPORT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": customer_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        log.warning(f"Send error: {e}")

# ===== AI АГЕНТ =====

def normalize_telegram_answer(text: str, max_chars: int = MAX_TELEGRAM_ANSWER_CHARS) -> str:
    """Clean LLM output for a natural Telegram chat reply."""
    s = normalize_plain_text(text, max_chars=max_chars)
    if not s:
        return "Передам вопрос команде Amori, чтобы вам ответили точнее."
    return s


def ai_respond(message: str, history: list) -> dict:
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    faq_context = ""
    try:
        similar = recall(message, entity_type="faq", limit=3)
        if similar:
            faq_context = "\nРЕЛЕВАНТНЫЕ ОТВЕТЫ ИЗ БАЗЫ:\n"
            for s in similar:
                faq_context += f"- {s['content']}\n"
    except:
        pass

    system_prompt = PRODUCT_KNOWLEDGE + faq_context + """\n
Верни ТОЛЬКО валидный JSON без markdown. answer — до 650 символов:
{"answer":"ответ","should_escalate":false,"confidence":0.9,"save_to_faq":false,"faq_question":null}

should_escalate=true: не знаешь ответа, недовольный клиент, просит живого человека.
save_to_faq=true: типичный вопрос с хорошим ответом."""

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-6:]:
        role = "user" if h["role"] == "customer" else "assistant"
        messages.append({"role": role, "content": h["content"][:300]})
    messages.append({"role": "user", "content": message})

    try:
        resp = llm.groq_chat(
            client, "support_agent", messages,
            model=llm.DEFAULT_GROQ_MODEL, temperature=0.25, max_tokens=260,
        )
        result = resp.choices[0].message.content
        data = llm.parse_json(result)
        if not isinstance(data, dict):
            raise ValueError("LLM вернул не JSON")
        answer = str(data.get("answer") or "")
        if agent_contracts.output_issues(answer):
            data["answer"] = (
                "Пока не хочу обещать неподтверждённые функции. Amori ещё в разработке, "
                "поэтому точные параметры продукта команда подтверждает отдельно. "
                "Передам ваш вопрос, чтобы ответили точнее."
            )
            data["should_escalate"] = True
            data["confidence"] = min(float(data.get("confidence") or 0.0), 0.4)
        data["answer"] = normalize_telegram_answer(data.get("answer", ""))
        if data.get("save_to_faq") and data.get("faq_question"):
            remember(
                f"Q: {data['faq_question']}\nA: {data['answer']}",
                "faq", "support", "support_agent",
                {"question": data["faq_question"], "answer": data["answer"]}
            )
        return data
    except Exception as e:
        log.error(f"AI error: {e}")
        return {
            "answer": "Не получилось быстро ответить. Передам вопрос команде Amori.",
            "should_escalate": True,
            "confidence": 0,
        }

# ===== TELEGRAM HANDLERS =====

async def setup_support_commands(application):
    await set_application_commands(application, "support")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome = (
        f"Привет, {user.first_name}! 👋\n\n"
        f"Я помощник Amori. Отвечу на вопросы по продукту и передам команде всё, "
        f"где нужен живой ответ.\n\n"
        f"Напишите, что вас интересует."
    )
    await update.message.reply_text(welcome)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(command_menu_text("support"))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ticket = get_latest_ticket_for_customer(str(user.id))
    if not ticket:
        await update.message.reply_text("Пока не вижу открытого обращения. Напишите вопрос, и я создам диалог.")
        return
    status_names = {
        "open": "в работе у помощника",
        "escalated": "передано команде Amori",
        "closed": "закрыто",
    }
    await update.message.reply_text(
        f"Ваше обращение #{ticket['id']}: {status_names.get(ticket['status'], ticket['status'])}."
    )

async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    customer_id = str(user.id)
    customer_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    customer_username = user.username or ""
    ticket_id = get_or_create_ticket(customer_id, customer_name, customer_username)
    text = "Клиент запросил связь с командой через /contact"
    save_support_message(ticket_id, "customer", text)
    escalate_ticket(ticket_id)
    notify_denis(ticket_id, customer_name or "Клиент", text, is_escalation=True)
    await update.message.reply_text("Передал запрос команде Amori. Ответим, как только сможем.")

async def handle_customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    customer_id = str(user.id)
    customer_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    customer_username = user.username or ""
    text = update.message.text

    ticket_id = get_or_create_ticket(customer_id, customer_name, customer_username)
    save_support_message(ticket_id, "customer", text)
    history = get_ticket_history(ticket_id)

    await update.message.reply_text("Секунду, посмотрю.")

    loop = asyncio.get_event_loop()
    response_data = await loop.run_in_executor(
        _executor, lambda: ai_respond(text, history)
    )

    answer = normalize_telegram_answer(response_data.get("answer", "Не смог обработать запрос."))
    should_escalate = response_data.get("should_escalate", False)
    confidence = response_data.get("confidence", 0.5)

    save_support_message(ticket_id, "assistant", answer)
    await update.message.reply_text(answer)

    # Эскалация если нужно или уверенность низкая
    if should_escalate or confidence < 0.5:
        escalate_ticket(ticket_id)
        await update.message.reply_text(
            "Передал вопрос команде Amori. Ответим, как только сможем."
        )
        notify_denis(ticket_id, customer_name, text, is_escalation=True)
    else:
        # Уведомляем Дениса о новом сообщении (тихо)
        notify_denis(ticket_id, customer_name, text, is_escalation=False)

    if should_escalate or confidence < 0.5:
        return

    # Добавляем короткую кнопку обратной связи
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Помогло", callback_data=f"helpful_{ticket_id}"),
            InlineKeyboardButton("❌ Нужна помощь команды", callback_data=f"escalate_{ticket_id}")
        ]
    ])
    await update.message.reply_text("Ответ помог?", reply_markup=keyboard)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("helpful_"):
        ticket_id = int(data.split("_")[1])
        close_ticket(ticket_id)
        await query.edit_message_text("Рад помочь! Если появятся ещё вопросы — пишите 😊")

    elif data.startswith("escalate_"):
        ticket_id = int(data.split("_")[1])
        ticket_info = get_ticket_info(ticket_id)
        escalate_ticket(ticket_id)
        notify_denis(
            ticket_id,
            ticket_info.get("name", "Клиент"),
            "Клиент запросил помощь команды",
            is_escalation=True
        )
        await query.edit_message_text(
            "Передал вопрос команде Amori. Ответим, как только сможем."
        )

def main():
    db.wait_ready("customer_db")  # на буте Postgres поднимается позже агента
    init_support_db()
    print("Support Agent запущен...")

    app = Application.builder().token(SUPPORT_TOKEN).post_init(setup_support_commands).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_customer_message))
    install_error_handler(app, log, "support_agent")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
