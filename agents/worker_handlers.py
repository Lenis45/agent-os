"""
worker_handlers — специализированные хендлеры воркеров AI-команды (Фаза 3).

Вместо универсального LLM-хендлера каждый воркер — доменный эксперт со своим
промптом и структурой вывода. Регистрируются в base_agent через register_all().

Внешние интеграции, которых пока нет (честно помечено):
  - content_designer: реальной генерации картинок нет (ComfyUI на GPU-ноде offline /
    нет image-API) → выдаёт детальный визуальный бриф + готовый промпт для генератора.
  - web_researcher и dev_worker передают сложные задачи в subscription-router. Он
    выбирает Codex/Claude через существующие подписки, а простой запрос оставляет локально.
  - Воркеры работают в режиме ответа: изменения файлов требуют отдельного подтверждения.
"""
import os

import llm
import base_agent
import ponytail
import agent_contracts

AMORI = "стартап Amori — умные GPS-ошейники для домашних животных, рынок РФ/СНГ"
PRODUCT_GUARDRAILS = """
Фактологические правила Amori:
- Не утверждай без явного источника: real-time, точность GPS/геолокации, уведомления, геозоны,
  мониторинг здоровья/активности, готовое приложение, цену, сроки запуска, статистику продаж,
  отзывы клиентов, факт публикации/отправки во внешний канал.
- Если данных не хватает, напиши мягко и безопасно: «параметры уточняются», «команда проверяет»,
  «нужно подтвердить перед публикацией».
- Не добавляй хэштеги по умолчанию; добавляй только если задача явно просит.
"""


def _agent(key, role, goal):
    return llm.build_agent(key, name=key, role=role, goal=goal)


def _task_text(task):
    return (f"ЗАДАЧА: {task.get('title','')}\n\nОПИСАНИЕ:\n{task.get('spec') or '(не задано)'}"
            + base_agent.upstream_context(task))


def _subscription_answer(prompt: str) -> str:
    try:
        return llm.smart_router_answer(prompt, cwd=os.path.dirname(os.path.dirname(__file__)))
    except Exception as error:
        print(f"[worker] smart router недоступен, использую local fallback: {error}")
        return ""


def _safe_content_fallback(task, error: Exception) -> str:
    return (
        "⚠️ Нужна редактура перед публикацией: черновик модели содержал неподтверждённые "
        f"продуктовые обещания ({error}).\n\n"
        "Безопасная версия:\n\n"
        "Мы готовим Amori для владельцев, которым важно спокойствие за питомца на прогулке и дома. "
        "Сейчас команда уточняет параметры продукта, поэтому не будем обещать лишнего до проверки.\n\n"
        "Расскажите, в какой ситуации вам чаще всего хочется быстрее понять, где ваш питомец?"
    )


def content_writer(task):
    a = _agent(
        "content_writer", "Старший копирайтер бренда Amori",
        f"Ты пишешь продающий живой контент для {AMORI}. Тон: дружелюбный, экспертный, "
        f"без воды и канцелярита. По-русски, готово к публикации.\n{PRODUCT_GUARDRAILS}")
    p = (f"{_task_text(task)}\n\nНапиши финальный текст. Если это пост — хук, тело и "
         "бережный призыв к ответу/действию. Если письмо — тема + тело. "
         "Верни только готовый текст, без служебных комментариев.")
    result = str(llm.run(a, p, "content_writer"))
    try:
        return agent_contracts.ensure_safe_marketing_text(result, "content_writer")
    except ValueError as e:
        return _safe_content_fallback(task, e)


def content_designer(task):
    a = _agent(
        "content_designer", "Арт-директор и дизайнер Amori",
        f"Ты создаёшь визуальные брифы для {AMORI}. Генерацию делает отдельный инструмент — "
        f"твоя задача дать точный бриф и готовый промпт для image-генератора.\n{PRODUCT_GUARDRAILS}")
    p = (f"{_task_text(task)}\n\nДай в markdown: 1) концепт визуала, 2) композиция/цвета/настроение, "
         "3) готовый англоязычный промпт для image-генератора (Stable Diffusion/ComfyUI), "
         "4) формат/размеры под канал. Не изображай интерфейс приложения, точные GPS-карты, "
         "медицинские метрики или функции, которые не подтверждены.")
    return str(llm.run(a, p, "content_designer"))


def content_reviewer(task):
    a = _agent(
        "content_reviewer", "Редактор-ревьюер контента Amori",
        f"Ты проверяешь контент на качество, бренд-голос, фактологию и грамотность.\n{PRODUCT_GUARDRAILS}")
    p = (f"{_task_text(task)}\n\nПроверь контент (из описания и результатов предыдущих задач). "
         "Обязательно отдельно отметь неподтверждённые claims. "
         "Верни: вердикт (✅ годится / ⚠️ доработать), список замечаний, улучшенную версию.")
    return str(llm.run(a, p, "content_reviewer"))


def web_researcher(task):
    a = _agent(
        "web_researcher", "Аналитик-ресёрчер Amori",
        f"Ты проводишь структурный ресёрч для {AMORI} по методологии: "
        "контекст → находки → гипотезы → выводы → рекомендации.")
    p = (f"{_task_text(task)}\n\nСделай структурный ресёрч-бриф (markdown). "
         "Где данные могут устаревать — помечай «⚠ требует проверки живыми данными».")
    return _subscription_answer(p) or str(llm.run(a, p, "web_researcher"))


def dev_worker(task):
    a = _agent(
        "dev_worker", "Senior-разработчик Amori (Go бэкенд, Kotlin мобайл)",
        ponytail.apply(
            "Ты решаешь код-задачи текстом: даёшь предлагаемый код/диф, объяснение, тесты и edge-cases. "
            "У тебя нет прямого доступа менять репозитории, поэтому не пиши «внедрил», «исправил в коде» "
            "или «протестировал», если задача не содержит реального вывода инструмента."))
    p = (f"{_task_text(task)}\n\nВерни в markdown: 1) ПРЕДЛОЖЕННОЕ РЕШЕНИЕ, 2) код/диф в код-блоках, "
         "3) как проверить, 4) edge-cases. Явно пометь, что это предложение к применению.")
    return _subscription_answer(p) or str(llm.run(a, p, "dev_worker"))


def ops_worker(task):
    a = _agent(
        "lead_manager", "Операционный аналитик Amori (CRM/лиды/продажи/процессы)",
        "Ты решаешь операционные задачи: анализ лидов, планы продаж, отчётность, процессы. "
        "Не утверждай, что внешнее действие выполнено (публикация, письмо, изменение CRM), "
        "если в задаче нет результата интеграции/инструмента.")
    p = (f"{_task_text(task)}\n\nВерни конкретный результат/план (markdown), без воды. "
         "Если задача просит опубликовать, отправить или изменить внешний сервис — верни план, "
         "готовый текст и что нужно подтвердить; не пиши, что действие уже выполнено.")
    return str(llm.run(a, p, "lead_manager"))


REGISTRY = {
    "content_writer": content_writer,
    "content_designer": content_designer,
    "content_reviewer": content_reviewer,
    "web_researcher": web_researcher,
    "dev_worker": dev_worker,
    "lead_manager": ops_worker,
}


def register_all():
    import base_agent
    for key, fn in REGISTRY.items():
        base_agent.register(key, fn)
    return list(REGISTRY.keys())
