import os
import json
import asyncio
import tempfile
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import urllib.request
import concurrent.futures
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
from memory import get_db, get_team_prompt, init_db, remember
from groq import Groq

import db
import llm
from applog import get_logger

load_dotenv()
init_db()
log = get_logger("orchestrator")

_HERE = os.path.dirname(os.path.abspath(__file__))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ===== ИСТОРИЯ РАЗГОВОРА =====

def save_message(user_id: str, role: str, content: str, tool: str = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (user_id, role, content, tool_used) VALUES (%s,%s,%s,%s)",
        (user_id, role, content, tool)
    )
    conn.commit()
    cur.close()
    conn.close()

def get_history(user_id: str, limit: int = 15) -> list:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT role, content FROM conversations
        WHERE user_id = %s
        ORDER BY created_at DESC LIMIT %s
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def save_pending(user_id: str, action_type: str, params: dict) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pending_actions (user_id, action_type, params) VALUES (%s,%s,%s) RETURNING id",
        (user_id, action_type, json.dumps(params))
    )
    action_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return action_id

def get_pending(user_id: str) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, action_type, params FROM pending_actions
        WHERE user_id = %s AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"id": row[0], "type": row[1], "params": row[2]}
    return None

def resolve_pending(action_id: int, status: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE pending_actions SET status=%s WHERE id=%s", (status, action_id))
    conn.commit()
    cur.close()
    conn.close()

def clear_pending(user_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE pending_actions SET status='cancelled' WHERE user_id=%s AND status='pending'", (user_id,))
    conn.commit()
    cur.close()
    conn.close()

# ===== ИНСТРУМЕНТЫ =====

def tool_translate(task: str) -> str:
    agent = llm.build_agent(
        "context_translator",
        name="Translator",
        role="Chief of Staff",
        goal=f"""Ты Chief of Staff Amori. {get_team_prompt()}
Определи кого затрагивает задача и напиши постановку для каждого.
Верни JSON: {{"affected": ["имя"], "messages": {{"имя": "постановка"}}}}""",
    )
    result = llm.run(agent, f"Задача: {task}\nВерни только JSON.", "context_translator")
    try:
        data = llm.parse_json(result) or {}
        affected = data.get("affected", [])
        messages = data.get("messages", {})
        icons = {"Макс": "👨‍💻", "Саша": "👨‍💻", "Паша": "👨‍💻", "Лева": "🔧",
                 "Лиза": "🎨", "Ася": "🎨", "Максим": "📊", "Арина": "📣"}
        response = f"📋 {task}\n👥 {', '.join(affected)}\n{'─'*35}\n\n"
        for person, msg in messages.items():
            response += f"{icons.get(person,'👤')} {person.upper()}:\n{msg}\n\n"
        return response
    except:
        return result

def tool_check_tasks() -> str:
    import subprocess, sys
    subprocess.Popen([sys.executable, os.path.join(_HERE, "task_sync.py")])
    return "Анализ задач запущен, отчёт придёт отдельным сообщением."

def tool_calendar_check() -> str:
    import subprocess, sys
    subprocess.Popen([sys.executable, os.path.join(_HERE, "calendar_agent.py")])
    return "Проверяю календарь, отчёт придёт через минуту."

def tool_save_note(text: str) -> str:
    import re
    vault = os.getenv("OBSIDIAN_VAULT")
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder = os.path.join(vault, "01 - Inbox/Необработанное")
    os.makedirs(folder, exist_ok=True)
    filename = f"note-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    with open(os.path.join(folder, filename), 'w') as f:
        f.write(f"---\ndate: {date_str}\nsource: orchestrator\n---\n\n{text}\n")
    remember(text, "note", "orchestrator", "ai_assistant")
    return f"✅ Сохранено в Obsidian: {filename}"

def tool_update_team(action: str, name: str, role: str = None, direction: str = None) -> str:
    from memory import update_team_member
    if action == "remove":
        update_team_member(name, active=False)
        return f"✅ {name} удалён из команды"
    else:
        update_team_member(name, role, direction)
        return f"✅ {name} добавлен/обновлён: {role}, {direction}"

def tool_add_lead(text: str) -> str:
    from lead_manager import add_lead, parse_lead_from_text
    data = parse_lead_from_text(text)
    result = add_lead(
        name=data.get('name', 'Неизвестно'),
        email=data.get('email'),
        phone=data.get('phone'),
        telegram=data.get('telegram'),
        source=data.get('source', 'telegram'),
        pet_type=data.get('pet_type'),
        notes=data.get('notes'),
        lead_type=data.get('lead_type', 'b2c')
    )
    name = data.get('name', 'Неизвестно')
    weeek = '✅ добавлен в WEEEK CRM' if result.get('weeek_deal_id') else '⚠️ только в базе'
    return '✅ Лид добавлен\n👤 ' + name + '\n📊 ' + weeek + '\nID: ' + str(result['id'])

def tool_leads_report() -> str:
    from lead_manager import run_leads_report
    run_leads_report()
    return 'Отчёт по лидам отправлен'

def tool_new_project(goal: str) -> str:
    """Запустить проект AI-команды: декомпозиция цели на задачи + раздача работникам."""
    if not goal.strip():
        return "❌ Опиши цель проекта"
    from project_manager import new_project
    r = new_project(goal)
    return (f"🚀 Проект #{r['project_id']} создан, {r['count']} задач(и) ушли команде. "
            f"Воркеры выполнят их, результаты придут в отчёты (дашборд :8099).")

def tool_make_content(brief: str) -> str:
    """Контент-завод: сгенерировать контент для продаж и положить на аппрув в дашборд."""
    if not brief.strip():
        return "❌ Опиши, какой контент нужен (бриф)"
    from content_factory import create
    cid = create(brief)
    return (f"🏭 Контент #{cid} готов и ждёт аппрува в дашборде :8099 "
            f"(раздел «Контент-завод»). Одобришь — опубликую.")

# Статичные знания о проекте — чтобы «мозг» реально понимал контекст Amori.
PROJECT_BRIEF = """О ПРОЕКТЕ AMORI:
Amori — стартап умных GPS-ошейников для домашних животных (собаки, кошки). Денис Колесников — основатель/CEO.
Три направления: «Ошейники» (железо/прошивка), «Приложение» (мобайл + бэкенд), «Шоп/Сайт» (e-commerce/лендинг).
Есть AI-команда автоматизации (этот бот — её мозг): агенты ведут лидов (WEEEK CRM), почту, дайджесты переписок,
календарь, контент-завод для продаж, очередь задач, бэкапы. Данные клиентов — в отдельной БД (152-ФЗ)."""


def _last_digest_raw() -> str:
    """Последний сырой дайджест Chief of Staff (для контекста ответов)."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT raw_output, digest_date, period FROM chief_digests "
                    "ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone(); cur.close(); conn.close()
        if row and row[0]:
            return f"ПОСЛЕДНИЙ ДАЙДЖЕСТ ({row[1]} {row[2]}):\n{row[0][:1200]}"
    except Exception:
        pass
    return ""


def build_context(user_message: str) -> str:
    """Богатый контекст для мозга: команда + бриф + семантическая память + дайджест."""
    parts = [PROJECT_BRIEF, get_team_prompt()]
    try:
        from memory import recall
        hits = recall(user_message, limit=4)
        if hits:
            parts.append("РЕЛЕВАНТНОЕ ИЗ ПАМЯТИ:")
            parts += [f"  - {h['content'][:200]}" for h in hits]
    except Exception:
        pass
    dig = _last_digest_raw()
    if dig:
        parts.append(dig)
    return "\n\n".join(parts)


def tool_direct_answer(question: str, history: list) -> str:
    """Содержательный ответ «мозга» на Qwen (qwen3.7-max) с богатым контекстом проекта.
    Groq — авто-фолбэк внутри llm.qwen_answer, чтобы бот никогда не молчал."""
    system = f"""Ты — персональный AI-ассистент и «второй мозг» Дениса Колесникова, CEO стартапа Amori.
{build_context(question)}

Правила: отвечай по-русски, конкретно и по делу, с опорой на контекст проекта и команду.
Если не хватает данных — скажи чего именно и предложи, что проверить. Не выдумывай факты."""
    context = "\n".join([f"{m['role']}: {m['content']}" for m in history[-6:]])
    prompt = f"История разговора:\n{context}\n\nВопрос Дениса: {question}"
    return str(llm.qwen_answer(prompt, system=system, agent_key="orchestrator"))


def extract_text_from_file(path: str, max_chars: int = 12000) -> str:
    """Извлечь текст из документа (pdf/docx/xlsx/txt/код). None — формат не поддержан."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)[:max_chars]
        if ext == ".docx":
            import docx
            d = docx.Document(path)
            return "\n".join(p.text for p in d.paragraphs if p.text)[:max_chars]
        if ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"# Лист: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        out.append("\t".join(cells))
            return "\n".join(out)[:max_chars]
        if ext in (".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".html", ".yaml", ".yml"):
            return open(path, encoding="utf-8", errors="ignore").read()[:max_chars]
    except Exception as e:
        return f"[не удалось извлечь текст: {e}]"
    return None


def tool_check_agents() -> str:
    """Реальное состояние AI-инфры: пульс агентов, последние прогоны, очередь, активность LLM."""
    import ops_store
    lines = ["🤖 СОСТОЯНИЕ AI-КОМАНДЫ\n"]
    try:
        conn = ops_store.get_conn(); cur = conn.cursor()
    except Exception as e:
        return f"⚠️ Не могу подключиться к ops_db: {e}"
    # Пульс (heartbeats)
    try:
        cur.execute("""SELECT component, status, EXTRACT(EPOCH FROM (now()-last_seen))/60
                       FROM infra_heartbeats ORDER BY last_seen DESC""")
        rows = cur.fetchall()
        if rows:
            lines.append("━━━ ПУЛЬС ━━━")
            for comp, st, age_min in rows:
                age_min = float(age_min or 0)
                icon = "🟢" if (st == "ok" and age_min < 180) else ("🟡" if st in ("ok", "warn") else "🔴")
                age = f"{int(age_min)}м" if age_min < 120 else f"{int(age_min // 60)}ч"
                lines.append(f"{icon} {comp}: {st} · {age} назад")
    except Exception:
        conn.rollback()
    # Последние прогоны мониторинга/бэкапа
    try:
        cur.execute("""SELECT DISTINCT ON (kind) kind, status, ts::timestamp(0)
                       FROM infra_runs ORDER BY kind, ts DESC""")
        runs = cur.fetchall()
        if runs:
            lines.append("\n━━━ ПОСЛЕДНИЕ ПРОГОНЫ ━━━")
            for kind, st, ts in runs:
                icon = "🟢" if st == "ok" else ("🟡" if st in ("warn", "partial") else "🔴")
                lines.append(f"{icon} {kind}: {st} ({ts})")
    except Exception:
        conn.rollback()
    # Активность LLM за 24ч
    try:
        cur.execute("""SELECT agent, count(*), max(ts)::timestamp(0) FROM llm_usage
                       WHERE ts > now()-interval '24 hours' GROUP BY agent ORDER BY max(ts) DESC""")
        usage = cur.fetchall()
        if usage:
            lines.append("\n━━━ АКТИВНОСТЬ ЗА 24Ч ━━━")
            for agent, cnt, last in usage:
                lines.append(f"  {agent}: {cnt} вызов(ов), посл. {last}")
        else:
            lines.append("\n⚠️ За 24ч активности LLM нет.")
    except Exception:
        conn.rollback()
    # Очередь задач
    try:
        cur.execute("SELECT status, count(*) FROM tasks GROUP BY status")
        q = dict(cur.fetchall())
        if q:
            lines.append("\n━━━ ОЧЕРЕДЬ ЗАДАЧ ━━━")
            lines.append("  " + " · ".join(f"{k}: {v}" for k, v in q.items()))
    except Exception:
        conn.rollback()
    conn.close()
    return "\n".join(lines)

# ===== ORCHESTRATOR =====

TOOLS_DESCRIPTION = """
Доступные инструменты:
- check_agents: состояние AI-команды/инфры — пульс агентов, прогоны, очередь, активность (params: нет). Используй на «проверь агентов», «работают ли боты», «статус системы».
- translate: перевести задачу для команды (params: task)
- check_tasks: проверить задачи в WEEEK и Taiga (params: нет)
- check_calendar: проверить и синхронизировать календарь (params: нет)
- save_note: сохранить заметку в Obsidian (params: text)
- update_team: обновить состав команды (params: action[add/remove], name, role, direction)
- answer: ответить на вопрос напрямую (params: question)
- add_lead: добавить нового лида (params: text с информацией о лиде)
- leads_report: показать отчёт по лидам (params: нет)
- send_email_lead: отправить письмо лиду (params: lead_id, email_type[intro/followup/proposal])
- send_bulk_emails: массовая рассылка новым лидам (params: нет)
- update_lead: обновить информацию о лиде (params: lead_id, field, value)
- get_leads: показать список лидов (params: status[optional])
- new_project: запустить проект для AI-команды — декомпозирует цель на задачи и раздаёт работникам (params: goal с описанием цели проекта)
- make_content: контент-завод для продаж — сгенерировать пост/письмо/креатив/лендинг и положить на аппрув (params: brief с описанием нужного контента)
"""

# Подтверждение требуется ТОЛЬКО для исходящих/необратимых действий (отправка писем,
# рассылка, запуск проекта команды, удаление из команды). Чтения и анализ — сразу,
# без лишних «ответь ДА/НЕТ» (это бесило в старой версии). Политика жёсткая, на сервере,
# а не на доверии к LLM.
CONFIRM_TOOLS = {"send_email_lead", "send_bulk_emails", "update_team", "new_project"}

def orchestrate(message: str, history: list) -> dict:
    """Определяем намерение и инструмент"""
    history_text = "\n".join([f"{m['role']}: {m['content'][:200]}" for m in history[-8:]])

    prompt = f"""Ты — маршрутизатор намерений для AI-ассистента CEO стартапа Amori.
Твоя задача: выбрать ОДИН инструмент и его параметры. Не отвечай по существу сам —
для содержательного ответа есть инструмент answer (его обрабатывает мощная модель).

{TOOLS_DESCRIPTION}

История разговора:
{history_text}

Новое сообщение: {message}

Верни ТОЛЬКО JSON:
{{
  "tool": "название инструмента",
  "params": {{}},
  "confirmation_text": "одной фразой что именно будет сделано (для исходящих действий)"
}}

Подсказки по выбору:
- «проверь агентов/ботов», «всё работает?», «статус системы» → check_agents
- вопрос/просьба объяснить/совет/анализ без явного действия → answer (params: {{"question": "..."}})
- отправить письмо лиду → send_email_lead; запустить проект команде → new_project."""

    result = llm.groq_chat(
        groq_client, "orchestrator",
        [{"role": "user", "content": prompt}],
        model="llama-3.3-70b-versatile", temperature=0.1, max_tokens=500,
    )

    text = result.choices[0].message.content.strip()

    # Парсим JSON
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        # Кривой ответ LLM не должен ронять ход оркестратора — отвечаем напрямую.
        print(f"[orchestrate] не распарсил JSON ({e}); fallback на answer")
        return {
            "tool": "answer", "params": {}, "needs_confirmation": False,
            "response_if_answer": (text or "").strip() or "Не понял запрос, переформулируй, пожалуйста.",
        }

def execute_tool(tool: str, params: dict, history: list) -> str:
    if tool == "check_agents":
        return tool_check_agents()
    elif tool == "translate":
        return tool_translate(params.get("task", ""))
    elif tool == "check_tasks":
        return tool_check_tasks()
    elif tool == "check_calendar":
        return tool_calendar_check()
    elif tool == "save_note":
        return tool_save_note(params.get("text", ""))
    elif tool == "update_team":
        return tool_update_team(
            params.get("action", "add"),
            params.get("name", ""),
            params.get("role"),
            params.get("direction")
        )
    elif tool == "add_lead":
        return tool_add_lead(params.get("text", ""))
    elif tool == "send_email_lead":
        from email_agent import send_to_lead
        lid = int(params.get("lead_id", 0))
        etype = params.get("email_type", "intro")
        try:
            result = send_to_lead(lid, etype)
        except Exception as e:
            import traceback; log.error(traceback.format_exc())
            return f"❌ Не отправил письмо лиду {lid}: {str(e)[:300]}"
        if result:
            return f"✅ Письмо ({etype}) отправлено лиду {lid}"
        return (f"❌ Письмо лиду {lid} не отправлено. Вероятные причины: у лида нет email, "
                f"не настроен SMTP, или письмо уже отправлялось. Проверь: get_leads.")
    elif tool == "update_lead":
        lid = int(params.get("lead_id", 0))
        field = params.get("field", "")
        value = params.get("value", "")
        allowed = ["telegram_username","phone","email","notes","status","pet_type","source"]
        if lid and field in allowed:
            conn = db.connect("customer_db")  # клиентский контур
            cur = conn.cursor()
            cur.execute(f"UPDATE leads SET {field}=%s, updated_at=NOW() WHERE id=%s", (value, lid))
            conn.commit()
            cur.close(); conn.close()
            return f"✅ Лид {lid} обновлён: {field} = {value}"
        return "❌ Укажи lead_id и поле"
    elif tool == "get_leads":
        from lead_manager import get_leads
        status = params.get("status")
        leads = get_leads(status, limit=10)
        if not leads:
            return "Лидов не найдено"
        result = "📋 Лиды:\n"
        for l in leads:
            result += f"#{l[0]} {l[1]} | {l[6] or '?'} | {l[7]}\n"
        return result
    elif tool == "send_bulk_emails":
        from email_agent import send_bulk
        send_bulk()
        return "Рассылка запущена"
    elif tool == "leads_report":
        return tool_leads_report()
    elif tool == "new_project":
        return tool_new_project(params.get("goal", ""))
    elif tool == "make_content":
        return tool_make_content(params.get("brief", ""))
    elif tool == "answer":
        return tool_direct_answer(params.get("question", ""), history)
    return "Не знаю как выполнить это действие."

# ===== TELEGRAM =====

def send_msg(text: str, chat_id: str = None):
    token = os.getenv("ORCHESTRATOR_BOT_TOKEN")
    cid = chat_id or os.getenv("TELEGRAM_MY_ID")
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": cid, "text": chunk}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log.warning(f"send_msg failed: {e}")

async def transcribe_voice(file_id: str, context) -> str:
    """Транскрибируем голос через Groq Whisper"""
    file = await context.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        with open(tmp.name, 'rb') as audio:
            transcription = groq_client.audio.transcriptions.create(
                file=("audio.ogg", audio, "audio/ogg"),
                model="whisper-large-v3",
                language="ru"
            )
    return transcription.text

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id != os.getenv("TELEGRAM_MY_ID"):
        return

    await update.message.reply_text("🎙 Распознаю голосовое...")

    try:
        text = await transcribe_voice(update.message.voice.file_id, context)
        await update.message.reply_text(f"🗣 Ты сказал: {text}")
        await process_message(update, context, text, user_id)
    except Exception as e:
        await update.message.reply_text(f"Не смог распознать: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото → анализ через Qwen-vision (qwen3-vl-plus)."""
    user_id = str(update.message.from_user.id)
    if user_id != os.getenv("TELEGRAM_MY_ID"):
        return
    caption = (update.message.caption or "").strip()
    question = caption or "Что на этом изображении? Проанализируй детально в контексте проекта Amori."
    await update.message.reply_text("🖼 Анализирую изображение...")
    path = None
    try:
        photo = update.message.photo[-1]  # самое крупное
        file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            path = tmp.name
        loop = asyncio.get_event_loop()
        prompt = question + "\n\nОтвечай по-русски, конкретно."
        result = await loop.run_in_executor(_executor, lambda: llm.vision_analyze(prompt, [path]))
        if not str(result).strip():
            result = "Не смог проанализировать изображение (vision-модель недоступна, попробуй позже)."
        save_message(user_id, "user", f"[фото] {caption}")
        save_message(user_id, "assistant", str(result), "vision")
        send_msg(str(result), str(update.effective_chat.id))
    except Exception as e:
        import traceback; log.error(traceback.format_exc())
        send_msg(f"⚠️ Ошибка анализа фото: {str(e)[:200]}", str(update.effective_chat.id))
    finally:
        if path:
            try: os.unlink(path)
            except OSError: pass

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Документ → извлечь текст и проанализировать Qwen. Картинку-документ → vision."""
    user_id = str(update.message.from_user.id)
    if user_id != os.getenv("TELEGRAM_MY_ID"):
        return
    doc = update.message.document
    caption = (update.message.caption or "").strip()
    fname = doc.file_name or "файл"
    ext = os.path.splitext(fname)[1].lower()
    await update.message.reply_text(f"📄 Читаю «{fname}»...")
    path = None
    try:
        file = await context.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=ext or ".bin", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            path = tmp.name
        loop = asyncio.get_event_loop()
        # Картинка, присланная как документ → vision
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            q = (caption or "Проанализируй это изображение в контексте Amori.") + "\nОтвечай по-русски."
            result = await loop.run_in_executor(_executor, lambda: llm.vision_analyze(q, [path]))
        else:
            content = await loop.run_in_executor(_executor, lambda: extract_text_from_file(path))
            if content is None:
                send_msg(f"⚠️ Формат {ext or '?'} пока не поддержан для анализа. "
                         f"Поддерживаю: pdf, docx, xlsx, txt/md/csv/код, картинки.",
                         str(update.effective_chat.id))
                return
            task = caption or "Кратко суммируй документ, выдели ключевое и предложи действия по проекту Amori."
            system = (f"Ты — аналитик-ассистент Дениса (CEO Amori).\n{PROJECT_BRIEF}\n"
                      "Анализируй документ по делу, по-русски, структурно.")
            prompt = f"Задача: {task}\n\nСОДЕРЖИМОЕ ДОКУМЕНТА «{fname}»:\n{content}"
            result = await loop.run_in_executor(
                _executor, lambda: llm.qwen_answer(prompt, system=system, agent_key="orchestrator", max_tokens=2000))
        if not str(result).strip():
            result = "Не смог обработать документ (модель недоступна, попробуй позже)."
        save_message(user_id, "user", f"[документ {fname}] {caption}")
        save_message(user_id, "assistant", str(result), "document")
        send_msg(str(result), str(update.effective_chat.id))
    except Exception as e:
        import traceback; log.error(traceback.format_exc())
        send_msg(f"⚠️ Ошибка обработки документа: {str(e)[:200]}", str(update.effective_chat.id))
    finally:
        if path:
            try: os.unlink(path)
            except OSError: pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id != os.getenv("TELEGRAM_MY_ID"):
        return

    text = update.message.text

    # Проверяем подтверждение
    if text.lower() in ["да", "✅", "подтверждаю", "ок", "ok", "yes"]:
        pending = get_pending(user_id)
        if pending:
            await update.message.reply_text("⚙️ Выполняю...")
            history = get_history(user_id)
            result = execute_tool(pending["type"], pending["params"], history)
            resolve_pending(pending["id"], "confirmed")
            save_message(user_id, "assistant", result, pending["type"])
            send_msg(result, update.effective_chat.id)
            return

    if text.lower() in ["нет", "отмена", "cancel", "no"]:
        pending = get_pending(user_id)
        if pending:
            resolve_pending(pending["id"], "cancelled")
            await update.message.reply_text("❌ Отменено.")
            return

    await process_message(update, context, text, user_id)

async def process_message(update: Update, context, text: str, user_id: str):
    # Сохраняем сообщение пользователя
    save_message(user_id, "user", text)

    # Получаем историю
    history = get_history(user_id)

    await update.message.reply_text("🤔 Думаю...")

    loop = asyncio.get_event_loop()

    try:
        decision = await loop.run_in_executor(_executor, lambda: orchestrate(text, history))
        tool = decision.get("tool", "answer")
        params = decision.get("params", {})
        # Политика подтверждений — серверная, не на доверии к LLM:
        # подтверждаем только исходящие/необратимые действия.
        needs_confirmation = tool in CONFIRM_TOOLS

        if tool == "answer":
            response = await loop.run_in_executor(_executor, lambda: tool_direct_answer(text, history))
            save_message(user_id, "assistant", response, "answer")
            send_msg(response, str(update.effective_chat.id))
            return

        if needs_confirmation:
            confirmation_text = decision.get("confirmation_text", f"Выполнить: {tool}?")
            action_id = save_pending(user_id, tool, params)
            save_message(user_id, "assistant", confirmation_text)
            await update.message.reply_text(
                f"🔔 Подтверди действие:\n\n{confirmation_text}\n\nОтветь ДА или НЕТ"
            )
        else:
            result = await loop.run_in_executor(_executor, lambda: execute_tool(tool, params, history))
            save_message(user_id, "assistant", result, tool)
            send_msg(result, str(update.effective_chat.id))

    except Exception as e:
        import traceback
        log.error(f"Orchestrator error: {e}")
        log.error(traceback.format_exc())
        send_msg("⚠️ Ошибка: " + str(e)[:200] + "\n\nПопробуй ещё раз.", str(update.effective_chat.id))

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответить клиенту: /reply_123 текст ответа"""
    if str(update.message.from_user.id) != os.getenv("TELEGRAM_MY_ID"):
        return
    cmd = update.message.text
    parts = cmd.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Использование: /reply_<ticket_id> текст")
        return
    try:
        ticket_id = int(parts[0].replace("/reply_", "").replace("/reply", ""))
        reply_text = parts[1]
    except:
        await update.message.reply_text("Неверный формат. Используй: /reply_123 текст ответа")
        return

    from support_agent import get_ticket_info, save_support_message, send_to_customer
    ticket = get_ticket_info(ticket_id)
    if not ticket:
        await update.message.reply_text("Тикет не найден")
        return

    save_support_message(ticket_id, "assistant", reply_text)
    send_to_customer(ticket["customer_id"], f"Команда Amori: {reply_text}")
    await update.message.reply_text(f"✅ Ответ отправлен клиенту {ticket['name']}")

async def handle_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать открытые тикеты поддержки"""
    if str(update.message.from_user.id) != os.getenv("TELEGRAM_MY_ID"):
        return
    from support_agent import get_open_tickets
    tickets = get_open_tickets()
    if not tickets:
        await update.message.reply_text("Открытых тикетов нет ✅")
        return
    text = "📋 Открытые тикеты поддержки:\n\n"
    for t in tickets:
        tid, name, username, status, msg_count, last_msg = t
        emoji = "🚨" if status == "escalated" else "💬"
        text += f"{emoji} #{tid} {name}"
        if username:
            text += f" (@{username})"
        text += f"\n   Статус: {status} | Сообщений: {msg_count}\n"
        text += f"   Ответить: /reply_{tid} текст\n\n"
    await update.message.reply_text(text)

async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id != os.getenv("TELEGRAM_MY_ID"):
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM conversations WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    clear_pending(user_id)
    await update.message.reply_text("🗑 История разговора очищена.")

def main():
    db.wait_ready("agents")  # на буте Postgres поднимается позже агента
    log.info("AI Orchestrator запущен (мозг: OpenModel/DeepSeek V4 Flash, fallback Groq; vision qwen3-vl-plus)")
    log.info("Поддержка: текст, голос, фото (vision), документы (pdf/docx/xlsx/txt), контекст проекта")
    app = Application.builder().token(os.getenv("ORCHESTRATOR_BOT_TOKEN")).build()
    app.add_handler(CommandHandler("clear", handle_clear))
    app.add_handler(CommandHandler("reply", handle_reply))
    app.add_handler(CommandHandler("tickets", handle_tickets))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
