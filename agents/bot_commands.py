import logging

from telegram import BotCommand


log = logging.getLogger(__name__)


ORCHESTRATOR_COMMANDS = (
    ("start", "Что умеет Emilia и как с ней работать"),
    ("help", "Показать команды и примеры запросов"),
    ("agents", "Проверить систему, сервисы и агентов"),
    ("tickets", "Показать открытые обращения клиентов"),
    ("leads", "Показать последние лиды и CRM-статус"),
    ("content", "Как поставить задачу контент-заводу"),
    ("hypotheses", "Анализ гипотез, RICE и экспериментов"),
    ("calendar", "Показать, добавить или исправить событие"),
    ("jobs", "Показать последнюю задачу и её статус"),
    ("files", "Показать файлы последней задачи"),
    ("cancel", "Отменить последнюю выполняемую задачу"),
    ("context", "Показать активный документ в контексте"),
    ("new", "Начать новый диалог без старого контекста"),
    ("clear", "Очистить историю диалога с Emilia"),
)

SUPPORT_COMMANDS = (
    ("start", "Начать диалог с помощником Amori"),
    ("help", "Показать что умеет этот бот"),
    ("status", "Проверить статус вашего обращения"),
    ("contact", "Позвать команду Amori в диалог"),
)

SECRETARY_COMMANDS = (
    ("start", "Что умеет личный секретарь Amori"),
    ("help", "Показать команды и примеры"),
    ("save", "Сохранить заметку: /save текст"),
    ("translate", "Разложить задачу по команде"),
    ("team", "Показать или обновить состав команды"),
)


def to_bot_commands(commands: tuple[tuple[str, str], ...]) -> list[BotCommand]:
    return [BotCommand(command=command, description=description) for command, description in commands]


def command_menu_text(kind: str) -> str:
    if kind == "orchestrator":
        return (
            "Команды Emilia:\n\n"
            "/help — показать это меню.\n"
            "/agents — проверить, работают ли сервисы и агенты.\n"
            "/tickets — открытые обращения клиентов.\n"
            "/leads — последние лиды из CRM.\n"
            "/content — как быстро поставить задачу на пост, письмо или креатив.\n"
            "/hypotheses — анализ гипотез, RICE, рисков и следующих действий.\n"
            "/calendar — показать неделю; /calendar встреча завтра в 10:00 — добавить; /calendar перенеси событие 1 на завтра 12:00 — исправить.\n"
            "/jobs — статус последней задачи и выбранный исполнитель.\n"
            "/files — файлы, созданные последней задачей.\n"
            "/cancel — отменить последнюю незавершённую задачу.\n"
            "/context — показать активный документ.\n"
            "/new — начать новый диалог.\n"
            "/clear — очистить контекст диалога.\n\n"
            "Можно писать и обычным текстом или голосом: «добавь встречу завтра в 10:00», «удали событие 1»."
        )
    if kind == "support":
        return (
            "Я помощник Amori.\n\n"
            "/status — проверить статус вашего обращения.\n"
            "/contact — позвать команду Amori, если нужен живой ответ.\n"
            "/help — показать это меню.\n\n"
            "Также можно просто написать вопрос обычным сообщением."
        )
    if kind == "secretary":
        return (
            "Команды SecretaryAmo:\n\n"
            "/save текст — сохранить заметку, идею, контакт или ссылку в базу знаний.\n"
            "/translate задача — разложить задачу по людям и ролям команды.\n"
            "/team — показать команду.\n"
            "/team add Имя Роль Направление — добавить участника.\n"
            "/team remove Имя — убрать участника.\n"
            "/help — показать это меню.\n\n"
            "Можно просто отправить обычный текст — я сохраню его как заметку."
        )
    return "Команды пока не настроены."


async def set_application_commands(application, kind: str) -> bool:
    if kind == "orchestrator":
        commands = ORCHESTRATOR_COMMANDS
    elif kind == "secretary":
        commands = SECRETARY_COMMANDS
    else:
        commands = SUPPORT_COMMANDS
    try:
        await application.bot.set_my_commands(to_bot_commands(commands))
        return True
    except Exception as exc:
        # The command menu is helpful but must not prevent message polling from starting.
        log.warning("Не удалось обновить меню Telegram-команд (%s): %s", kind, exc)
        return False
