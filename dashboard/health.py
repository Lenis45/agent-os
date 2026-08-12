"""Pure product-health rules shared by the dashboard and its tests."""
from datetime import UTC, datetime, timedelta


UP_AGENT_STATUSES = {"running", "scheduled", "cron"}
USABLE_PROVIDER_STATUSES = {"ok", "healthy", "configured"}


def _action(code, severity, title, detail, area="system", href=None):
    item = {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "area": area,
    }
    if href:
        item["href"] = href
    return item


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def summarize(*, agents, containers, heartbeats, projects, content, leads, surfaces=None, now=None):
    now = now or datetime.now(UTC)
    required_agents = [agent for agent in agents if agent.get("type") != "ondemand"]
    agents_up = sum(agent.get("status") in UP_AGENT_STATUSES for agent in required_agents)
    containers_up = sum(bool(value) for value in containers.values())
    actions = []
    surfaces = surfaces or {}

    if surfaces.get("smm_factory") is False:
        actions.append(_action(
            "smm_factory_down", "critical", "SMM-редакция недоступна",
            "Нельзя подготовить, согласовать или запланировать публикацию.",
            "content", "http://localhost:8180",
        ))

    down_containers = [name for name, running in containers.items() if not running]
    if down_containers:
        actions.append(_action(
            "containers_down", "critical", "Не запущены базовые сервисы",
            ", ".join(down_containers),
        ))

    down_agents = [agent.get("name") or agent.get("key") for agent in required_agents
                   if agent.get("status") not in UP_AGENT_STATUSES]
    if down_agents:
        actions.append(_action(
            "agents_down", "critical", "Не работают обязательные агенты",
            ", ".join(down_agents),
        ))

    provider_heartbeats = [
        item for item in heartbeats
        if str(item.get("component", "")).startswith("llm_")
        or item.get("component") in {"freeqwen", "freeglmkimi"}
    ]
    usable_providers = [item for item in provider_heartbeats if item.get("status") in USABLE_PROVIDER_STATUSES]
    if provider_heartbeats and not usable_providers:
        actions.append(_action(
            "llm_unavailable", "critical", "Нет подтверждённого LLM-маршрута",
            "Все проверенные провайдеры недоступны. Генерация может вернуть пустой результат.",
        ))
    elif usable_providers:
        fresh_cutoff = now - timedelta(hours=30)
        if all((_parse_datetime(item.get("last_seen")) or datetime.min.replace(tzinfo=UTC)) < fresh_cutoff
               for item in usable_providers):
            actions.append(_action(
                "llm_health_stale", "warning", "Проверка AI-моделей устарела",
                "Запустите provider health check перед важной генерацией.",
            ))

    for heartbeat in heartbeats:
        component = str(heartbeat.get("component", ""))
        if (component.startswith("llm_") or component in {"freeqwen", "freeglmkimi"}
                or heartbeat.get("status") in {"ok", "disabled"}):
            continue
        component_names = {
            "calendar_agent": "Календарь",
            "infra_monitor": "Инфраструктура Mac Mini",
            "backup": "Резервное копирование",
            "restore_test": "Проверка восстановления",
            "worker_dispatch": "Очередь агентов",
        }
        title = component_names.get(component, component.replace("_", " ").title())
        actions.append(_action(
            f"heartbeat_{component}", "warning", f"{title}: требуется внимание",
            str((heartbeat.get("meta") or {}).get("message")
                or (heartbeat.get("meta") or {}).get("status")
                or heartbeat.get("status") or "warning"),
        ))

    invalid_content = [item for item in content
                       if item.get("original_status", item.get("status")) in {"pending", "approved"}
                       and (not str(item.get("body") or "").strip()
                            or (item.get("kind") in {"post", "ad_creative", "landing"}
                                and not str(item.get("image_brief") or "").strip()))]
    if invalid_content:
        ids = ", ".join(f"#{item.get('id')}" for item in invalid_content[:5])
        actions.append(_action(
            "content_incomplete", "warning", "Неполные материалы сняты с согласования",
            f"Проверьте генерацию: {ids}.", "content", "http://localhost:8180",
        ))

    failed_content = [item for item in content if item.get("status") == "failed"]
    if failed_content:
        ids = ", ".join(f"#{item.get('id')}" for item in failed_content[:5])
        actions.append(_action(
            "content_generation_failed", "warning", "Не удалось подготовить материалы",
            f"Повторите создание в SMM-редакции: {ids}.", "content", "http://localhost:8180",
        ))

    stale_projects = [item for item in projects if item.get("status") == "active"
                      and int(item.get("total") or 0) > 0
                      and int(item.get("done") or 0) == int(item.get("total") or 0)]
    if stale_projects:
        actions.append(_action(
            "project_status_stale", "warning", "Завершённые проекты ещё отмечены активными",
            f"Найдено: {len(stale_projects)}. Статус будет синхронизирован.", "work",
        ))

    overdue = int((leads or {}).get("overdue") or 0)
    if overdue:
        actions.append(_action(
            "lead_followup_overdue", "warning", "Просрочены контакты с лидами",
            f"Нужно связаться: {overdue} из {(leads or {}).get('total', overdue)}.", "crm",
            "https://t.me/Emilia_Orchestrator_bot",
        ))

    critical = sum(item["severity"] == "critical" for item in actions)
    warnings = sum(item["severity"] == "warning" for item in actions)
    status = "blocked" if critical else "degraded" if warnings else "healthy"
    labels = {
        "healthy": "Можно работать",
        "degraded": "Есть задачи внимания",
        "blocked": "Ключевой сценарий заблокирован",
    }
    return {
        "status": status,
        "label": labels[status],
        "critical": critical,
        "warnings": warnings,
        "actions": actions,
        "counts": {
            "agents_up": agents_up,
            "agents_required": len(required_agents),
            "agents_on_demand": len(agents) - len(required_agents),
            "containers_up": containers_up,
            "containers_total": len(containers),
        },
    }
