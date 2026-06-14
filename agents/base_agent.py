"""
base_agent — каркас работника AI-команды.

Воркер берёт задачу из очереди (tasks.claim), выполняет, помечает done/failed и
сдаёт отчёт. По умолчанию задача выполняется универсальным LLM-хендлером; для
конкретного воркера можно зарегистрировать специализированный хендлер (register()).

process_one(agent_key) — обработать одну задачу воркера (вернёт True если была).
"""
import tasks
import report as report_mod
import llm

# agent_key -> функция(task: dict) -> str (результат)
HANDLERS = {}


def register(agent_key, fn):
    HANDLERS[agent_key] = fn


def upstream_context(task: dict) -> str:
    """Блок с результатами задач-зависимостей — чтобы воркер видел работу апстрима."""
    deps = task.get("deps_results") or []
    if not deps:
        return ""
    blocks = "\n\n".join(f"— [{d['title']}]:\n{(d['result'] or '')[:3000]}" for d in deps)
    return f"\n\n=== РЕЗУЛЬТАТЫ ПРЕДЫДУЩИХ ЗАДАЧ (используй как контекст) ===\n{blocks}"


def default_handler(task: dict) -> str:
    """Универсальный воркер: выполнить spec задачи через LLM и вернуть текст-результат."""
    domain = task.get("domain") or "ops"
    key = task.get("assignee") or "orchestrator"
    agent = llm.build_agent(
        key,
        name=key,
        role=f"Работник домена {domain} в AI-команде стартапа Amori (умные ошейники)",
        goal=(f"Ты выполняешь рабочие задачи домена «{domain}». "
              "Делай конкретно, по делу, на русском. Возвращай готовый результат, "
              "пригодный для использования (текст/план/анализ), без воды."),
    )
    prompt = (f"ЗАДАЧА: {task.get('title','')}\n\n"
              f"ОПИСАНИЕ:\n{task.get('spec') or '(описание не задано)'}"
              f"{upstream_context(task)}\n\n"
              "Выполни задачу и верни итоговый результат.")
    return str(llm.run(agent, prompt, key))


def process_one(agent_key) -> bool:
    """Взять и выполнить одну задачу воркера. True если задача была обработана."""
    t = tasks.claim(agent_key)
    if not t:
        return False
    tid = t["id"]
    try:
        # dep_results/start ВНУТРИ try: если упадут (ошибка БД), задача не зависнет
        # в 'claimed' — попадёт в except → tasks.fail (иначе воркер её больше не возьмёт).
        t["deps_results"] = tasks.dep_results(tid)
        tasks.start(tid)
        handler = HANDLERS.get(agent_key, default_handler)
        result = handler(t)
        tasks.complete(tid, result)
        report_mod.report(
            agent_key, kind="result", title=t.get("title", ""),
            summary=(result or "")[:400], body=(result or "")[:8000],
            project_id=t.get("project_id"), task_id=tid,
        )
        print(f"[worker {agent_key}] done #{tid}: {t.get('title','')[:60]}")
        return True
    except Exception as e:
        tasks.fail(tid, e)
        report_mod.report(
            agent_key, kind="alert", title=f"FAILED: {t.get('title','')}",
            summary=str(e)[:300], project_id=t.get("project_id"), task_id=tid,
        )
        print(f"[worker {agent_key}] FAILED #{tid}: {e}")
        return True
