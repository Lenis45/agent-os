import json
import subprocess
from datetime import datetime, timedelta, timezone
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
    version_calls = []
    auth_calls = []
    broker_worker._runtime_info_cache = None
    monkeypatch.setattr(broker_worker.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        broker_worker,
        "_command_version",
        lambda command: version_calls.append(command) or f"{command}-1",
    )
    monkeypatch.setattr(
        broker_worker,
        "_command_authenticated",
        lambda command: auth_calls.append(command) or command == "codex",
    )

    first = broker_worker._runtime_info()
    second = broker_worker._runtime_info()

    assert first == second
    assert first[1] == {"codex": True, "claude": False}
    assert version_calls == ["codex", "claude"]
    assert auth_calls == ["codex", "claude"]


@pytest.mark.parametrize(
    ("command", "stdout", "returncode", "expected"),
    [
        ("claude", '{"loggedIn": true}', 0, True),
        ("claude", '{"loggedIn": false}', 0, False),
        ("codex", "Logged in using ChatGPT", 0, True),
        ("codex", "Not logged in", 1, False),
    ],
)
def test_worker_checks_real_subscription_auth(monkeypatch, command, stdout, returncode, expected):
    monkeypatch.setattr(broker_worker.shutil, "which", lambda _command: f"/bin/{command}")
    monkeypatch.setattr(
        broker_worker.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout, ""),
    )

    assert broker_worker._command_authenticated(command) is expected


def test_broker_model_call_allows_routed_subscription_fallback(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, _request_id, *, timeout):
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "answer": "готово",
                "decision": {"provider": "codex"},
                "evidence": [],
            }, ensure_ascii=False),
            "",
        )

    monkeypatch.setattr(broker_worker, "_run_cancellable", fake_run)

    answer, evidence = broker_worker._model_call(
        "claude",
        "Проведи анализ",
        {"id": "request-1", "mode": "ask"},
        tmp_path,
    )

    assert answer == "готово"
    assert "--allow-subscription-fallback" in captured["command"]
    assert evidence[-1] == {"type": "model_result", "provider": "codex"}


def test_document_text_is_embedded_and_refusal_falls_back(monkeypatch, tmp_path):
    extracted = tmp_path / "extracted.txt"
    extracted.write_text("Project codename: ORCHID-742", encoding="utf-8")
    artifact = SimpleNamespace(
        owner="denis",
        original_name="brief.txt",
        extracted_text_path=str(extracted),
    )
    calls = []

    def fake_model_call(provider, prompt, _request, _cwd):
        calls.append((provider, prompt))
        if provider == "hermes":
            return "Не могу прочитать вложение, передайте данные Codex.", []
        return "ORCHID-742", [{"type": "model_result", "provider": provider}]

    monkeypatch.setattr(broker_worker.artifact_store, "get_artifact", lambda _artifact_id: artifact)
    monkeypatch.setattr(broker_worker, "_model_call", fake_model_call)
    monkeypatch.setattr(broker_worker.request_store, "append_event", lambda *_args: None)

    answer, evidence = broker_worker._router_call({
        "id": "request-1",
        "actor_id": "denis",
        "prompt_text": "Верни полный код",
        "input_artifact_ids": ["artifact-1"],
        "cwd": str(tmp_path),
        "route": {"provider": "hermes"},
    })

    assert answer == "ORCHID-742"
    assert [provider for provider, _prompt in calls] == ["hermes", "claude"]
    assert "извлечённый текст файлов" in calls[0][1]
    assert "Project codename: ORCHID-742" in calls[0][1]
    assert any(item["type"] == "model_fallback" for item in evidence)


def test_explicit_side_effect_requires_action_mode():
    assert orchestrator.infer_request_mode("Проанализируй договор и выпиши риски") == "ask"
    assert orchestrator.infer_request_mode("Добавь встречу завтра в 10:00") == "act"
    assert orchestrator.infer_request_mode("Исправь код и сделай коммит") == "act"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Продолжай", True),
        ("Да, делай", True),
        ("Исправь это и перепроверь", True),
        ("А теперь сделай короче", True),
        ("Почему там выбрана эта модель?", True),
        ("Почему небо голубое?", False),
        ("Новая задача: подготовь пост", False),
        ("Какая погода завтра?", False),
    ],
)
def test_continuation_classifier_separates_followups_from_new_topics(text, expected):
    latest = {
        "id": "parent",
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    assert bool(orchestrator.continuation_reason(text, latest)) is expected


def test_reply_to_message_continues_recent_task():
    latest = {
        "id": "parent",
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    assert orchestrator.continuation_reason(
        "Сформулируй иначе", latest, reply_to_message=True
    ) == "reply"


def test_stale_thread_is_not_resumed_implicitly():
    latest = {
        "id": "parent",
        "status": "completed",
        "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    }

    assert orchestrator.continuation_reason("Продолжай", latest) is None


def test_routing_text_contains_only_bounded_thread_context(monkeypatch):
    monkeypatch.setattr(
        request_broker.request_store,
        "list_thread_requests",
        lambda *_args, **_kwargs: [
            {"prompt_text": "Проверь систему", "result_text": "Найден риск"},
        ],
    )
    payload = request_broker.RequestCreate(
        source="telegram", actor_id="denis", session_id="chat",
        text="Исправь это", parent_request_id="00000000-0000-0000-0000-000000000001",
    )

    text = request_broker._routing_text(payload, {"id": "parent", "thread_id": "thread"})

    assert "Проверь систему" in text
    assert "Найден риск" in text
    assert text.endswith("Новое уточнение пользователя: Исправь это")


def test_worker_handoff_contains_goal_result_and_selected_skills(monkeypatch, tmp_path):
    monkeypatch.setattr(
        broker_worker.request_store,
        "list_thread_requests",
        lambda *_args, **_kwargs: [
            {
                "prompt_text": "Проведи аудит Emilia",
                "status": "completed",
                "result_text": "Найдена потеря контекста",
            }
        ],
    )
    prompt = broker_worker._execution_prompt({
        "id": "child",
        "thread_id": "thread",
        "parent_request_id": "parent",
        "prompt_text": "Исправь это и добавь тесты",
        "mode": "act",
        "cwd": str(tmp_path),
        "route": {
            "provider": "codex",
            "selected_skills": ["debugging", "testing"],
            "expected_outputs": ["code", "tests"],
        },
    })

    assert "Проведи аудит Emilia" in prompt
    assert "Найдена потеря контекста" in prompt
    assert "Исправь это и добавь тесты" in prompt
    assert "debugging, testing" in prompt
    assert len(prompt) <= 9000


def test_api_can_reset_current_topic(monkeypatch):
    monkeypatch.setenv("AMORI_BROKER_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(
        request_broker.request_store, "reset_session",
        lambda source, actor, session: calls.append((source, actor, session)),
    )
    client = TestClient(request_broker.app)

    response = client.post(
        "/v1/sessions/telegram/denis/chat/reset",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"reset": True}
    assert calls == [("telegram", "denis", "chat")]


def test_submission_rejects_parent_from_another_session(monkeypatch):
    monkeypatch.setattr(
        request_broker.request_store, "get_request",
        lambda _request_id: {
            "id": "parent", "source": "telegram", "actor_id": "other",
            "session_id": "chat", "status": "completed",
        },
    )

    with pytest.raises(ValueError, match="does not belong"):
        request_broker.submit(request_broker.RequestCreate(
            source="telegram", actor_id="denis", session_id="chat",
            text="Продолжай", parent_request_id="00000000-0000-0000-0000-000000000001",
        ))


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
