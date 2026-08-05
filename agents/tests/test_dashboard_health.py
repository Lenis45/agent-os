from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path


HEALTH_PATH = Path(__file__).resolve().parents[2] / "dashboard" / "health.py"


def _load_health_module():
    spec = importlib.util.spec_from_file_location("dashboard_health", HEALTH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_on_demand_agent_is_not_counted_as_required():
    health = _load_health_module()
    agents = [
        {"key": "orchestrator", "name": "Emilia", "type": "longrun", "status": "running"},
        {"key": "email_agent", "name": "Email", "type": "ondemand", "status": "on-demand"},
    ]

    summary = health.summarize(
        agents=agents,
        containers={"ai_postgres": True},
        heartbeats=[{"component": "llm_groq", "status": "ok", "last_seen": datetime.now(UTC).isoformat()}],
        projects=[],
        content=[],
        leads={"total": 0, "overdue": 0},
        surfaces={"smm_factory": True, "pixel_office": True},
    )

    assert summary["counts"]["agents_up"] == 1
    assert summary["counts"]["agents_required"] == 1
    assert summary["status"] == "healthy"


def test_health_surfaces_unavailable_llm_and_invalid_content_as_actions():
    health = _load_health_module()
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    summary = health.summarize(
        agents=[{"key": "orchestrator", "name": "Emilia", "type": "longrun", "status": "running"}],
        containers={"ai_postgres": True},
        heartbeats=[{"component": "llm_groq", "status": "warn", "last_seen": old, "meta": {"status": "timeout"}}],
        projects=[{"id": 4, "status": "active", "total": 2, "done": 2, "failed": 0}],
        content=[
            {"id": 5, "status": "pending", "kind": "post", "body": "", "image_brief": ""},
            {"id": 6, "status": "failed", "kind": "post", "body": "", "image_brief": ""},
        ],
        leads={"total": 14, "overdue": 14},
        surfaces={"smm_factory": False, "pixel_office": True},
    )

    assert summary["status"] == "blocked"
    assert {item["code"] for item in summary["actions"]} >= {
        "llm_unavailable",
        "content_incomplete",
        "content_generation_failed",
        "lead_followup_overdue",
        "project_status_stale",
        "smm_factory_down",
    }
    lead_action = next(item for item in summary["actions"] if item["code"] == "lead_followup_overdue")
    assert lead_action["href"] == "https://t.me/Emilia_Orchestrator_bot"
