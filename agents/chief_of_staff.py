import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Message
from datetime import datetime, timedelta
from memory import remember, save_digest, get_recent_digests, get_team_prompt, init_db

import notify
import llm
from applog import get_logger
from retry import safe

load_dotenv()
init_db()
log = get_logger("chief_of_staff")

tg = TelegramClient(
    'chief_session',
    int(os.getenv("TELEGRAM_API_ID")),
    os.getenv("TELEGRAM_API_HASH")
)

def make_agent():
    team_prompt = get_team_prompt()
    return llm.build_agent(
        "chief_of_staff",
        name="ChiefOfStaff",
        role="Личный Chief of Staff основателя стартапа",
        goal=f"""Ты Chief of Staff Дениса Колесникова — основателя стартапа Amori (умные ошейники).

{team_prompt}

Правила анализа:
- Различай рабочие переписки и личные
- Если кто-то ждёт ответа от Дениса — это СРОЧНО
- Договорённости фиксируй точно: кто, что, когда
- Если контекст обрывается без ответа — незакрытый вопрос
- Игнорируй системные уведомления и спам
- Пиши конкретно с именами из команды
- ВАЖНО: если задача уже была в прошлых дайджестах и не закрыта — отмечай как ПОВТОРНАЯ""",
    )

async def collect_messages(hours=12):
    await tg.start(phone=os.getenv("TELEGRAM_PHONE"))
    since = datetime.now() - timedelta(hours=hours)
    collected = []
    skip_chats = {"Telegram", "BotFather"}

    async for dialog in tg.iter_dialogs():
        if dialog.is_channel or dialog.name in skip_chats:
            continue
        async for msg in tg.iter_messages(dialog, limit=100):
            if not isinstance(msg, Message) or not msg.text:
                continue
            if msg.date.replace(tzinfo=None) < since:
                break
            collected.append({
                "chat": dialog.name,
                "sender": getattr(msg.sender, 'first_name', '?') if msg.sender else '?',
                "text": msg.text[:600],
                "time": msg.date.strftime("%H:%M"),
                "is_me": msg.out
            })
    return collected

async def run():
    now = datetime.now()
    period = "утренний" if now.hour < 15 else "вечерний"
    now_str = now.strftime("%d.%m.%Y %H:%M")

    log.info(f"Chief of Staff ({period}) запущен")

    try:
        messages = await collect_messages(hours=12)
    except Exception as e:
        log.error(f"Сбор сообщений из Telegram упал: {e}")
        notify.send(f"Chief of Staff {now_str}: не удалось собрать сообщения ({e})", "warn")
        return
    if not messages:
        notify.send(f"Chief of Staff {now_str}\nСообщений нет.")
        return

    log.info(f"Сообщений: {len(messages)}")

    # Получаем прошлые дайджесты для контекста
    past_digests = get_recent_digests(days=3)
    past_context = ""
    if past_digests:
        past_context = "\nИЗВЕСТНОЕ ИЗ ПРОШЛЫХ ДАЙДЖЕСТОВ (не повторяй без изменений):\n"
        for d in past_digests[:3]:
            date, period_d, tasks, agreements, deadlines, important = d
            if tasks:
                past_context += f"[{date} {period_d}] Задачи: {', '.join(tasks[:3])}\n"
            if agreements:
                past_context += f"[{date} {period_d}] Договорённости: {', '.join(agreements[:2])}\n"

    # ── Статистика по переписке (детерминированная, до LLM) ──
    from collections import Counter
    chat_latest = {}
    for m in messages:  # collect_messages идёт newest-first внутри чата → первое вхождение = свежее
        chat_latest.setdefault(m["chat"], m)
    incoming = sum(1 for m in messages if not m["is_me"])
    outgoing = len(messages) - incoming
    waiting_chats = [c for c, m in chat_latest.items() if not m["is_me"]]
    busiest = Counter(m["chat"] for m in messages).most_common(3)

    stats_block = (
        f"📊 {len(messages)} сообщений · {len(chat_latest)} чатов · "
        f"входящих {incoming} / исходящих {outgoing}\n"
    )
    if waiting_chats:
        stats_block += f"⏳ Ждут ответа ({len(waiting_chats)}): {', '.join(waiting_chats[:6])}\n"
    if busiest:
        stats_block += "🔥 Активные: " + " · ".join(f"{c} ({n})" for c, n in busiest) + "\n"

    # Формируем текст переписок
    text = ""
    for m in messages:
        who = "Денис" if m["is_me"] else m["sender"]
        text += f"[{m['time']}] {m['chat']} | {who}: {m['text']}\n"

    prompt = f"""Проанализируй переписки Дениса за последние 12 часов.
{past_context}

СТАТИСТИКА (уже посчитана, используй как опору):
{stats_block}
Чаты, где последнее сообщение НЕ от Дениса (вероятно ждут ответа): {', '.join(waiting_chats) or 'нет'}

ПЕРЕПИСКИ:
{text}

Составь {period} дайджест. Будь конкретным, называй имена и чаты, указывай время где важно.
Для каждого пункта в «ТРЕБУЕТ ОТВЕТА» добавь [чат, во сколько] и одной строкой суть.

🔴 ТРЕБУЕТ ОТВЕТА — приоритезируй: сверху те, кто ждёт дольше/важнее. Формат: «Имя (чат, ЧЧ:ММ) — суть».

📋 ЗАДАЧИ НА СЕГОДНЯ — конкретные действия с ответственными.

🔁 ПОВТОРНЫЕ / ЗАВИСШИЕ — было в прошлых дайджестах и до сих пор не закрыто.

🤝 ДОГОВОРЁННОСТИ — кто что пообещал, кому, к какому сроку.

⏰ ДЕДЛАЙНЫ — с датами/временем.

❓ НЕЗАКРЫТЫЕ ВОПРОСЫ — где разговор оборвался без ответа.

💡 НА ЗАМЕТКУ — важный контекст, риски, настроение/тон если значимо.

Не выдумывай. Если раздел пуст — напиши «— нет»."""

    result = llm.run(make_agent(), prompt, "chief_of_staff")
    result_str = str(result)

    # Сохраняем дайджест в память (не валим прогон при ошибке БД)
    safe(save_digest, period, [], [], [], [], result_str, label="save_digest", logger=log)

    # Сохраняем новые сущности в shared memory
    for msg in messages:
        if any(word in msg["text"].lower() for word in ["встреча", "созвон", "звонок", "митинг"]):
            safe(remember, msg["text"], "meeting", "telegram", "chief_of_staff",
                 {"chat": msg["chat"], "sender": msg["sender"]}, label="remember", logger=log)

    header = (f"📋 Chief of Staff | {period.upper()} ДАЙДЖЕСТ\n{now_str}\n\n"
              f"{stats_block}\n")
    notify.send(header + result_str)
    log.info("Готово")

if __name__ == "__main__":
    asyncio.run(run())
