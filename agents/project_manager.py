"""
project_manager — приём проекта и декомпозиция на задачи для AI-команды.

new_project(goal) → создаёт проект, через LLM раскладывает цель на 3-6 задач,
назначает каждую доменному воркеру и кладёт в очередь. Воркеры разбирают её сами.
"""
import tasks
import report as report_mod
import llm

# домен → дефолтный воркер (agent_registry.kind='worker')
DOMAIN_WORKER = {
    "content": "content_writer",
    "research": "web_researcher",
    "dev": "dev_worker",
    "ops": "lead_manager",
    "sales": "lead_manager",
}
VALID_DOMAINS = set(DOMAIN_WORKER.keys())

# роль внутри домена → конкретный воркер (тонкое распределение работы команды)
ROLE_WORKER = {
    "writer": "content_writer", "copywriter": "content_writer",
    "designer": "content_designer", "design": "content_designer",
    "reviewer": "content_reviewer", "review": "content_reviewer", "editor": "content_reviewer",
    "researcher": "web_researcher", "research": "web_researcher", "analyst": "web_researcher",
    "dev": "dev_worker", "developer": "dev_worker", "engineer": "dev_worker",
    "ops": "lead_manager", "sales": "lead_manager", "manager": "lead_manager",
}


def _pick_assignee(role, dom):
    if role:
        w = ROLE_WORKER.get(str(role).strip().lower())
        if w:
            return w
    return DOMAIN_WORKER[dom]


def new_project(goal: str, name: str = None, domain: str = None) -> dict:
    name = (name or goal)[:80]
    pid = tasks.create_project(name, goal=goal, domain=domain, owner_agent="project_manager")

    pm = llm.build_agent(
        "orchestrator",
        name="ProjectManager",
        role="Проект-менеджер AI-команды стартапа Amori",
        goal=("Разложи цель проекта на 3-6 конкретных выполнимых задач для команды. "
              "Для каждой укажи домен (content, research, dev, ops) и роль исполнителя "
              "(writer, designer, reviewer, researcher, dev, ops). "
              'Если задача зависит от другой — укажи "after": номер задачи (1-based) в массиве. '
              "Например: контент-задачам обычно нужен порядок writer → designer → reviewer. "
              'Верни ТОЛЬКО JSON-массив объектов вида '
              '[{"title":"...","spec":"что конкретно сделать","domain":"content",'
              '"role":"writer","after":null}]. Без пояснений вне JSON.'),
    )
    raw = llm.run(pm, f"Цель проекта: {goal}\n\nВерни только JSON-массив задач.", "orchestrator")
    items = llm.parse_json(raw)
    if not isinstance(items, list):
        items = [{"title": name, "spec": goal, "domain": domain or "ops"}]

    tids = []
    for i, it in enumerate(items[:8]):
        if not isinstance(it, dict):
            continue
        dom = (it.get("domain") or domain or "ops").strip().lower()
        if dom not in VALID_DOMAINS:
            dom = "ops"
        assignee = _pick_assignee(it.get("role"), dom)
        # зависимость: "after" = 1-based индекс ранее созданной задачи
        deps = None
        after = it.get("after")
        try:
            ai = int(after) - 1
            if 0 <= ai < len(tids):
                deps = [tids[ai]]
        except (TypeError, ValueError):
            pass
        tid = tasks.enqueue(
            it.get("title", "задача")[:200], spec=it.get("spec", ""),
            project_id=pid, assignee=assignee, domain=dom, priority=5 + i, deps=deps,
        )
        tids.append(tid)

    report_mod.report(
        "project_manager", kind="result", title=f"Новый проект: {name}",
        summary=f"Создан проект #{pid} и {len(tids)} задач(и) для команды",
        project_id=pid, meta={"tasks": tids}, telegram=True,
    )
    print(f"[project_manager] project #{pid} «{name}» → {len(tids)} задач: {tids}")
    return {"project_id": pid, "tasks": tids, "count": len(tids)}


if __name__ == "__main__":
    import sys
    goal = " ".join(sys.argv[1:]) or "Подготовить контент-план на неделю для Telegram-канала Amori"
    print(new_project(goal))
