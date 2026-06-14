"""
worker_handlers — специализированные хендлеры воркеров AI-команды (Фаза 3).

Вместо универсального LLM-хендлера каждый воркер — доменный эксперт со своим
промптом и структурой вывода. Регистрируются в base_agent через register_all().

Внешние интеграции, которых пока нет (честно помечено):
  - content_designer: реальной генерации картинок нет (ComfyUI на GPU-ноде offline /
    нет image-API) → выдаёт детальный визуальный бриф + готовый промпт для генератора.
  - web_researcher: живого веб-поиска из агента нет (нужен search-API) → структурный
    ресёрч из знаний модели с пометками «требует проверки живыми данными».
  - dev_worker: реального редактирования репозитория нет (OpenCode/Aider не подключён) →
    выдаёт код/решение/тесты текстом, готовые к применению.
"""
import llm
import base_agent

AMORI = "стартап Amori — умные GPS-ошейники для домашних животных, рынок РФ/СНГ"


def _agent(key, role, goal):
    return llm.build_agent(key, name=key, role=role, goal=goal)


def _task_text(task):
    return (f"ЗАДАЧА: {task.get('title','')}\n\nОПИСАНИЕ:\n{task.get('spec') or '(не задано)'}"
            + base_agent.upstream_context(task))


def content_writer(task):
    a = _agent(
        "content_writer", "Старший копирайтер бренда Amori",
        f"Ты пишешь продающий живой контент для {AMORI}. Тон: дружелюбный, экспертный, "
        "без воды и канцелярита. По-русски, готово к публикации.")
    p = (f"{_task_text(task)}\n\nНапиши ФИНАЛЬНЫЙ текст. Если это пост — хук, тело, "
         "призыв к действию и 3-5 хэштегов. Если письмо — тема + тело. Верни только готовый текст.")
    return str(llm.run(a, p, "content_writer"))


def content_designer(task):
    a = _agent(
        "content_designer", "Арт-директор и дизайнер Amori",
        f"Ты создаёшь визуальные брифы для {AMORI}. Генерацию делает отдельный инструмент — "
        "твоя задача дать точный бриф и готовый промпт для image-генератора.")
    p = (f"{_task_text(task)}\n\nДай в markdown: 1) концепт визуала, 2) композиция/цвета/настроение, "
         "3) готовый англоязычный промпт для image-генератора (Stable Diffusion/ComfyUI), "
         "4) формат/размеры под канал.")
    return str(llm.run(a, p, "content_designer"))


def content_reviewer(task):
    a = _agent(
        "content_reviewer", "Редактор-ревьюер контента Amori",
        "Ты проверяешь контент на качество, бренд-голос, фактологию и грамотность.")
    p = (f"{_task_text(task)}\n\nПроверь контент (из описания и результатов предыдущих задач). "
         "Верни: вердикт (✅ годится / ⚠️ доработать), список замечаний, улучшенную версию.")
    return str(llm.run(a, p, "content_reviewer"))


def web_researcher(task):
    a = _agent(
        "web_researcher", "Аналитик-ресёрчер Amori",
        f"Ты проводишь структурный ресёрч для {AMORI} по методологии: "
        "контекст → находки → гипотезы → выводы → рекомендации.")
    p = (f"{_task_text(task)}\n\nСделай структурный ресёрч-бриф (markdown). "
         "Где данные могут устаревать — помечай «⚠ требует проверки живыми данными».")
    return str(llm.run(a, p, "web_researcher"))


def dev_worker(task):
    a = _agent(
        "dev_worker", "Senior-разработчик Amori (Go бэкенд, Kotlin мобайл)",
        "Ты решаешь код-задачи: даёшь конкретный код/диф, объяснение, тесты и edge-cases.")
    p = (f"{_task_text(task)}\n\nВерни в markdown: 1) разбор, 2) код/решение в код-блоках, "
         "3) тесты, 4) edge-cases.")
    return str(llm.run(a, p, "dev_worker"))


def ops_worker(task):
    a = _agent(
        "lead_manager", "Операционный аналитик Amori (CRM/лиды/продажи/процессы)",
        "Ты решаешь операционные задачи: анализ лидов, планы продаж, отчётность, процессы.")
    p = f"{_task_text(task)}\n\nВыполни и верни конкретный результат/план (markdown), без воды."
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
