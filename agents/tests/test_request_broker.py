import json
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def test_worker_artifact_rejects_owner_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AMORI_BROKER_TOKEN", "test-token")
    monkeypatch.setattr(
        request_broker.request_store,
        "get_request",
        lambda _request_id: {"id": "r1", "actor_id": "denis"},
    )
    client = TestClient(request_broker.app)

    response = client.post(
        "/v1/workers/r1/artifacts?owner_id=someone-else",
        headers={"Authorization": "Bearer test-token"},
        files={"file": ("result.txt", b"done", "text/plain")},
    )

    assert response.status_code == 403


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


def test_image_download_accepts_only_known_https_hosts():
    assert broker_worker._validated_image_url("https://cdn.qwenlm.ai/result.png")
    with pytest.raises(RuntimeError, match="unsafe"):
        broker_worker._validated_image_url("http://127.0.0.1/private.png")
    with pytest.raises(RuntimeError, match="untrusted"):
        broker_worker._validated_image_url("https://example.com/result.png")


def test_image_payload_requires_real_image_magic():
    assert broker_worker._image_suffix(b"\x89PNG\r\n\x1a\nrest") == ".png"
    assert broker_worker._image_suffix(b"\xff\xd8\xffrest") == ".jpg"
    assert broker_worker._image_suffix(b"RIFFxxxxWEBPrest") == ".webp"
    with pytest.raises(RuntimeError, match="non-image"):
        broker_worker._image_suffix(b"<html>not an image</html>")


def test_worker_runtime_info_is_cached(monkeypatch):
    calls = []
    broker_worker._runtime_info_cache = None
    monkeypatch.setattr(broker_worker.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        broker_worker,
        "_command_version",
        lambda command: calls.append(command) or f"{command}-1",
    )
    monkeypatch.setattr(broker_worker.shutil, "which", lambda command: f"/bin/{command}")

    first = broker_worker._runtime_info()
    second = broker_worker._runtime_info()

    assert first == second
    assert calls == ["codex", "claude"]


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


def test_question_waits_for_offline_target_device(monkeypatch):
    statuses = []
    events = []
    monkeypatch.setattr(
        request_broker, "route_prompt",
        lambda *_a: {"provider": "claude", "target_device": "macbook"},
    )
    monkeypatch.setattr(
        request_broker.request_store, "create_request",
        lambda **_kwargs: ({"id": "r-wait", "status": "queued"}, True),
    )
    monkeypatch.setattr(request_broker.request_store, "worker_available", lambda _device: False)
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
        lambda _request_id: {"id": "r-wait", "status": "waiting_for_device"},
    )

    request, created = request_broker.submit(
        request_broker.RequestCreate(
            source="terminal", actor_id="denis", session_id="shell",
            text="Проведи ревью проекта", mode="ask", target_device="macbook",
        )
    )

    assert created is True
    assert request["status"] == "waiting_for_device"
    assert statuses == [("r-wait", "waiting_for_device")]
    assert events[0][1] == "waiting_for_device"
