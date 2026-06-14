import os
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from dotenv import load_dotenv

import notify
import llm
from applog import get_logger
from retry import net_retry

load_dotenv()
log = get_logger("email_watchdog")

agent = llm.build_agent(
    "email_watchdog",
    name="EmailWatchdog",
    role="Фильтр входящей почты руководителя стартапа",
    goal="""Ты анализируешь входящие письма Дениса Колесникова — основателя стартапа Amori (умные ошейники).
Твоя задача — отделить важное от мусора и дать чёткий дайджест.

Важное (всегда включай):
- Письма от партнёров, инвесторов, клиентов
- Договоры, счета, юридические документы
- Мероприятия и приглашения
- Письма требующие ответа
- Уведомления о платежах и транзакциях

Игнорируй:
- Рекламные рассылки и промо
- Автоматические уведомления от сервисов (GitHub, Jira и т.д.)
- Newsletters на которые подписан
- Спам

Отвечай на русском. Будь конкретным — указывай отправителя и суть.""",
)

def decode_str(s):
    if s is None:
        return ""
    decoded = decode_header(s)
    result = ""
    for part, enc in decoded:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="ignore")
        else:
            result += str(part)
    return result

def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
                except:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except:
            pass
    return body[:1000]

@net_retry(attempts=2)
def _imap_fetch(host, email_addr, password, hours=24):
    """IMAP-загрузка писем. Ретраится при сетевых сбоях; исключение наружу для safe()."""
    emails = []
    mail = imaplib.IMAP4_SSL(host)
    mail.login(email_addr, password)
    mail.select("INBOX")

    since = (datetime.now() - timedelta(hours=hours)).strftime("%d-%b-%Y")
    _, data = mail.search(None, f'(SINCE "{since}")')
    ids = data[0].split()[-30:]  # максимум 30 писем

    for uid in ids:
        _, msg_data = mail.fetch(uid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        date_str = msg.get("Date", "")
        try:
            date_fmt = parsedate_to_datetime(date_str).strftime("%d.%m %H:%M")
        except Exception:
            date_fmt = date_str[:16]
        emails.append({
            "account": email_addr,
            "from": decode_str(msg.get("From", "")),
            "subject": decode_str(msg.get("Subject", "")),
            "date": date_fmt,
            "body": get_body(msg),
        })
    mail.logout()
    return emails

def fetch_emails(host, email_addr, password, hours=24):
    """Обёртка: один сбойный ящик не валит весь прогон."""
    from retry import safe
    return safe(_imap_fetch, host, email_addr, password, hours,
                default=[], label=f"imap:{email_addr}", logger=log)

def run():
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    log.info("Email Watchdog запущен")

    all_emails = []
    accounts = [
        ("imap.gmail.com", "GMAIL1_EMAIL", "GMAIL1_PASSWORD"),
        ("imap.gmail.com", "GMAIL2_EMAIL", "GMAIL2_PASSWORD"),
        ("imap.yandex.ru", "YANDEX_EMAIL", "YANDEX_PASSWORD"),
    ]
    for host, ek, pk in accounts:
        addr = os.getenv(ek)
        if not addr:
            continue
        log.info(f"Проверяю {addr}...")
        all_emails += fetch_emails(host, addr, os.getenv(pk))

    if not all_emails:
        notify.send(f"Email Watchdog | {now_str}\nНовых писем за последние 24 часа нет.")
        return

    log.info(f"Найдено писем: {len(all_emails)}")

    # Формируем текст для анализа
    text = ""
    for e in all_emails:
        text += f"[{e['date']}] {e['account']}\nОт: {e['from']}\nТема: {e['subject']}\n{e['body'][:300]}\n---\n"

    prompt = f"""Вот входящие письма Дениса за последние 24 часа:

{text}

Составь дайджест по структуре:

📧 ТРЕБУЕТ ОТВЕТА
(письма где нужна реакция Дениса)

📋 К СВЕДЕНИЮ
(важное что нужно знать но не требует ответа)

📅 МЕРОПРИЯТИЯ И ДЕДЛАЙНЫ
(приглашения, события, сроки)

🗑 ПРОИГНОРИРОВАНО
(только количество — сколько рекламных/автоматических писем отфильтровано)

Для каждого письма указывай: отправитель, тема, суть в одной строке."""

    result = llm.run(agent, prompt, "email_watchdog")

    header = f"Email Watchdog | {now_str}\n{len(all_emails)} писем проверено\n\n"
    notify.send(header + str(result))
    log.info("Дайджест отправлен в Telegram")

if __name__ == "__main__":
    run()
