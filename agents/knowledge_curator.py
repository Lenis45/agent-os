import os
import re
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from memory import remember, recall, is_known, get_team_prompt, update_team_member, init_db

import notify
import llm
import db
from applog import get_logger

load_dotenv()
init_db()
log = get_logger("knowledge_curator")

VAULT = os.getenv("OBSIDIAN_VAULT")

curator_agent = llm.build_agent(
    "knowledge_curator",
    name="KnowledgeCurator",
    role="Личный архивариус",
    goal="""Структурируй сообщение в заметку Obsidian.
Категории: LINK, IDEA, TASK, NOTE, CONTACT, MEETING, RESOURCE
Папки: ссылки→01 - Inbox/Ссылки, бизнес идеи→07 - Ideas/Business Ideas, продукт→07 - Ideas/Product Ideas, контакты→05 - People/Network, встречи→06 - Meetings, ресурсы→08 - Resources/Articles, остальное→01 - Inbox/Необработанное
Верни ТОЛЬКО JSON без markdown: {"category":"IDEA","folder":"путь","filename":"имя","title":"Заголовок","content":"Содержимое","tags":["тег"]}""",
)

def get_translator_agent():
    team_prompt = get_team_prompt()
    return llm.build_agent(
        "context_translator",
        name="ContextTranslator",
        role="Chief of Staff стартапа Amori",
        goal=f"""Ты Chief of Staff стартапа Amori — умные ошейники для животных. Стек: Kotlin (мобайл), Go (бэкенд).

{team_prompt}

ЗАДАЧА: Прочитай описание задачи и определи кого она реально затрагивает.
Не добавляй лишних — только тех кто нужен для выполнения.

Для каждого затронутого напиши постановку на его языке:
- Разработчик (бэкенд/фронт): что реализовать, acceptance criteria, edge cases
- Дизайнер/Фронтенд: пользовательский флоу, список экранов, компоненты
- Лид: что организовать, кого задействовать, критерии готовности
- Финансист: какие данные собрать, метрики, формат результата
- Продажи/SMM: контент-план, аудитория, метрики

Каждая постановка — конкретная, человек начинает работу без вопросов.
Только русский язык. Верни ТОЛЬКО JSON:
{{
  "original": "исходная задача",
  "affected": ["имя1"],
  "messages": {{"имя1": "постановка"}}
}}""",
    )

ROLE_ICONS = {
    "Лева": "🔧", "Андрей": "🔧", "Даня": "🔧",
    "Макс": "👨‍💻", "Саша": "👨‍💻", "Паша": "👨‍💻", "Петя": "👨‍💻",
    "Лиза": "🎨", "Ася": "🎨",
    "Олег": "🧪", "Максим": "📊",
    "Арина": "📣", "Павел": "🎓"
}

def save_to_obsidian(folder, filename, content):
    folder_path = os.path.join(VAULT, folder)
    os.makedirs(folder_path, exist_ok=True)
    safe_name = re.sub(r'[<>:"/\\|?*]', '-', filename)
    if not safe_name.endswith('.md'):
        safe_name += '.md'
    filepath = os.path.join(folder_path, safe_name)
    if os.path.exists(filepath):
        ts = datetime.now().strftime("%H%M%S")
        safe_name = safe_name.replace('.md', f'-{ts}.md')
        filepath = os.path.join(folder_path, safe_name)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    return filepath

async def handle_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление командой: /team add Имя Роль Направление"""
    if str(update.message.from_user.id) != os.getenv("TELEGRAM_MY_ID"):
        return

    args = context.args
    if not args:
        team_prompt = get_team_prompt()
        await update.message.reply_text(f"Текущая команда:\n\n{team_prompt}\n\nКоманды:\n/team add Имя Роль Направление\n/team remove Имя")
        return

    if args[0] == "add" and len(args) >= 3:
        name = args[1]
        role = args[2]
        direction = args[3] if len(args) > 3 else "Общий"
        update_team_member(name, role, direction)
        await update.message.reply_text(f"✅ Добавлен: {name} ({role}, {direction})")

    elif args[0] == "remove" and len(args) >= 2:
        name = args[1]
        update_team_member(name, active=False)
        await update.message.reply_text(f"✅ Удалён из команды: {name}")

    else:
        await update.message.reply_text("Использование:\n/team add Имя Роль Направление\n/team remove Имя")

async def handle_translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != os.getenv("TELEGRAM_MY_ID"):
        return

    task_text = " ".join(context.args)
    if not task_text:
        await update.message.reply_text("Использование:\n/translate описание задачи")
        return

    await update.message.reply_text("⏳ Анализирую задачу...")
    now = datetime.now()

    # Проверяем похожие задачи в памяти
    similar = recall(task_text, limit=3)
    similar_context = ""
    if similar:
        similar_context = "\nПОХОЖИЕ ЗАДАЧИ ИЗ ПРОШЛОГО:\n"
        for s in similar:
            similar_context += f"- {s['content'][:100]} ({s['timestamp'][:10]})\n"

    try:
        translator = get_translator_agent()
        result = llm.run(
            translator,
            f"Задача от Дениса:\n---\n{task_text}\n---\n{similar_context}\nВерни только JSON.",
            "context_translator",
        )
        data = llm.parse_json(result)
        if not isinstance(data, dict):
            raise ValueError("LLM вернул не JSON")
        affected = data.get("affected", [])
        messages = data.get("messages", {})

        response = f"📋 {task_text}\n👥 {', '.join(affected)}\n{'─'*35}\n\n"
        for person, msg in messages.items():
            icon = ROLE_ICONS.get(person, "👤")
            response += f"{icon} {person.upper()}:\n{msg}\n\n"

        # Сохраняем в память
        remember(task_text, "task", "translate_bot", "context_translator",
                {"affected": affected})

        # Сохраняем в Obsidian
        date_str = now.strftime("%Y-%m-%d")
        md = f"---\ndate: {date_str}\ncategory: TASK\nsource: translator\n---\n\n# {task_text[:60]}\n\n## Затронуты\n{', '.join(affected)}\n\n"
        for person, msg in messages.items():
            md += f"## {person}\n{msg}\n\n"
        save_to_obsidian("02 - Projects/Amori/Team", f"task-{date_str}-{now.strftime('%H%M%S')}", md)

        notify.send(response)

    except Exception as e:
        log.error(f"Translate error: {e}")
        await update.message.reply_text("Ошибка. Попробуй ещё раз.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if str(message.from_user.id) != os.getenv("TELEGRAM_MY_ID"):
        return

    text = message.text or message.caption or ""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    log.info(f"{text[:80]}")

    # Проверяем дубликат
    if is_known(text):
        notify.send("ℹ️ Это уже есть в базе знаний")
        return

    try:
        result = llm.run(
            curator_agent,
            f"Сообщение:\n---\n{text}\n---\nДата: {date_str} {time_str}\nТолько JSON.",
            "knowledge_curator",
        )
        data = llm.parse_json(result)
        if not isinstance(data, dict):
            raise ValueError("LLM вернул не JSON")
        tags_hash = " ".join([f"#{t}" for t in data.get("tags", [])])
        md = f"---\ndate: {date_str}\ntime: {time_str}\ncategory: {data.get('category','NOTE')}\ntags: [{', '.join(data.get('tags',[]))}]\nsource: telegram\n---\n\n# {data.get('title','Заметка')}\n\n{data.get('content', text)}\n\n---\n{tags_hash}\n"

        filepath = save_to_obsidian(
            data.get("folder", "01 - Inbox/Необработанное"),
            data.get("filename", f"note-{date_str}"),
            md
        )

        # Сохраняем в shared memory
        remember(text, data.get("category", "NOTE").lower(),
                "telegram", "knowledge_curator",
                {"folder": data.get("folder"), "title": data.get("title")})

        notify.send(f"✅ Сохранено\n📂 {data.get('folder')}\n🏷 {data.get('category')}\n📄 {data.get('title')}")

    except Exception as e:
        log.error(f"Error: {e}")
        md = f"---\ndate: {date_str}\ntime: {time_str}\nsource: telegram\n---\n\n{text}\n"
        save_to_obsidian("01 - Inbox/Необработанное", f"inbox-{date_str}-{now.strftime('%H%M%S')}", md)
        notify.send("📥 Сохранено в Inbox")

def main():
    db.wait_ready("agents")  # на буте Postgres поднимается позже агента
    log.info("Knowledge Curator + Context Translator запущен")
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    app.add_handler(CommandHandler("translate", handle_translate))
    app.add_handler(CommandHandler("team", handle_team))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
