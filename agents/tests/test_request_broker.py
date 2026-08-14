import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import broker_worker
import orchestrator
import request_broker


def test_route_prompt_retries_with_deterministic_rules(monkeypatch):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=2, stdout="", stderr="local timeout")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"provider": "hermes", "execution_handler": "local_answer"}),
            stderr="",
        )

    monkeypatch.setattr(request_broker.subprocess, "run", fake_run)

    route = request_broker.route_prompt("Что такое RAG?", "ask")

    assert route["provider"] == "hermes"
    assert "--no-neural-route" in calls[1]


def test_opencode_requests_target_macbook():
    payload = request_broker.RequestCreate(
        source="opencode", actor_id="denis", session_id="s1", text="Исправь код", mode="act"
    )

    assert request_broker._target_device(payload, {"target_device": "current"}) == "macbook"


def test_telegram_requests_target_mac_mini():
    payload = request_broker.RequestCreate(
        source="telegram", actor_id="denis", session_id="s1", text="Объясни кратко"
    )

    assert request_broker._target_device(payload, {"target_device": "auto"}) == "mac-mini"


def test_api_requires_bearer_token(monkeypatch):
    monkeypatch.setenv("AMORI_BROKER_TOKEN", "test-token")
    client = TestClient(request_broker.app)

    response = client.get("/v1/requests/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 401


def test_api_returns_persisted_request(monkeypatch):
    monkeypatch.setenv("AMORI_BROKER_TOKEN", "test-token")
    monkeypatch.setattr(request_broker.request_store, "get_request", lambda _rid: {"id": "r1", "status": "queued"})
    monkeypatch.setattr(request_broker.request_store, "list_events", lambda _rid: [])
    monkeypatch.setattr(request_broker.request_store, "list_artifacts", lambda _rid: [])
    client = TestClient(request_broker.app)

    response = client.get("/v1/requests/r1", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    assert response.json()["request"]["status"] == "queued"


def test_worker_rejects_native_handler_tool_mismatch(monkeypatch):
    monkeypatch.setattr(
        broker_worker.orchestrator,
        "orchestrate",
        lambda *_a, **_k: {"tool": "add_calendar_event", "params": {}},
    )
    request = {
        "prompt_text": "Добавь лида",
        "route": {"execution_handler": "crm"},
    }

    try:
        broker_worker._native_tool(request)
    except RuntimeError as error:
        assert "rejected incompatible tool" in str(error)
    else:
        raise AssertionError("tool mismatch must fail closed")


def test_worker_discovers_only_workspace_artifacts(tmp_path):
    inside = tmp_path / "report.pdf"
    inside.write_bytes(b"pdf")
    outside = tmp_path.parent / "secret.pdf"
    outside.write_bytes(b"secret")

    paths = broker_worker._safe_discovered_paths(
        f"Готово: {inside}\nТакже: {outside}", tmp_path
    )

    assert inside.resolve() in paths
    assert outside.resolve() not in paths


def test_explicit_side_effect_requires_action_mode():
    assert orchestrator.infer_request_mode("Проанализируй договор и выпиши риски") == "ask"
    assert orchestrator.infer_request_mode("Добавь встречу завтра в 10:00") == "act"
    assert orchestrator.infer_request_mode("Исправь код и сделай коммит") == "act"


def test_action_submission_waits_for_confirmation(monkeypatch):
    statuses = []
    events = []
    monkeypatch.setattr(
        request_broker, "route_prompt",
        lambda *_a: {"provider": "hermes", "execution_handler": "calendar"},
    )
    monkeypatch.setattr(
        request_broker.request_store, "create_request",
        lambda **_kwargs: ({"id": "r-act", "status": "queued"}, True),
    )
    monkeypatch.setattr(
        request_broker.request_store, "set_status",
        lambda request_id, status: statuses.append((request_id, status)),
    )
    monkeypatch.setattr(
        request_broker.request_store, "append_event",
        lambda *args: events.append(args),
    )
    monkeypatch.setattr(
        request_broker.request_store, "get_request",
        lambda _request_id: {"id": "r-act", "status": "awaiting_confirmation"},
    )

    request, created = request_broker.submit(
        request_broker.RequestCreate(
            source="telegram", actor_id="denis", session_id="chat",
            text="Добавь встречу завтра", mode="act",
        )
    )

    assert created is True
    assert request["status"] == "awaiting_confirmation"
    assert statuses == [("r-act", "awaiting_confirmation")]
    assert events[0][1] == "awaiting_confirmation"
