import pytest

import task_sync


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_weeek_task_pagination_fetches_all_pages(monkeypatch):
    offsets = []

    def fake_get(_url, **kwargs):
        offset = kwargs["params"]["offset"]
        offsets.append(offset)
        count = 100 if offset == 0 else 27
        return FakeResponse({"tasks": [{"id": offset + index + 1} for index in range(count)]})

    monkeypatch.setattr(task_sync.requests, "get", fake_get)

    tasks = task_sync._weeek_task_pages({"Authorization": "Bearer redacted"}, 1)

    assert len(tasks) == 127
    assert offsets == [0, 100]
    assert len({task["id"] for task in tasks}) == 127


def test_weeek_task_pagination_rejects_duplicate_ids(monkeypatch):
    monkeypatch.setattr(
        task_sync.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse({"tasks": [{"id": 1}, {"id": 1}]}),
    )

    with pytest.raises(RuntimeError, match="duplicate task id"):
        task_sync._weeek_task_pages({}, 1)


def test_successful_empty_sources_skip_llm_and_telegram(monkeypatch):
    runs = []
    heartbeats = []
    states = []
    monkeypatch.setattr(task_sync.db, "wait_ready", lambda *_args: True)
    monkeypatch.setattr(task_sync, "init_db", lambda: None)
    monkeypatch.setattr(task_sync.ops_store, "init", lambda: None)
    monkeypatch.setattr(
        task_sync,
        "get_weeek_tasks",
        lambda: task_sync.TaskSourceResult("WEEEK", tasks=[]),
    )
    monkeypatch.setattr(
        task_sync,
        "get_taiga_tasks",
        lambda: task_sync.TaskSourceResult("Taiga", enabled=False),
    )
    monkeypatch.setattr(task_sync, "save_snapshot", lambda *_args: None)
    monkeypatch.setattr(task_sync.ops_store, "set_automation_state", lambda *args: states.append(args))
    monkeypatch.setattr(task_sync.ops_store, "record_run", lambda *args: runs.append(args))
    monkeypatch.setattr(task_sync.ops_store, "heartbeat", lambda *args: heartbeats.append(args))
    monkeypatch.setattr(
        task_sync.notify,
        "send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Telegram called")),
    )
    monkeypatch.setattr(
        task_sync.llm,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LLM called")),
    )

    task_sync.run()

    assert runs[0][1] == "ok"
    assert runs[0][2]["active_tasks"] == 0
    assert runs[0][2]["telegram_skipped"] is True
    assert heartbeats[0][1] == "ok"
    assert states[0][0] == "task_sync_digest"


def test_disabled_taiga_makes_no_http_requests(monkeypatch):
    monkeypatch.setenv("TASK_SYNC_TAIGA_ENABLED", "false")
    monkeypatch.setattr(
        task_sync.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Taiga HTTP called")),
    )

    result = task_sync.get_taiga_tasks()

    assert result.enabled is False
    assert result.tasks == []


def test_api_failure_is_not_treated_as_empty_success(monkeypatch):
    monkeypatch.setattr(task_sync.db, "wait_ready", lambda *_args: True)
    monkeypatch.setattr(task_sync, "init_db", lambda: None)
    monkeypatch.setattr(task_sync.ops_store, "init", lambda: None)
    monkeypatch.setattr(
        task_sync,
        "get_weeek_tasks",
        lambda: task_sync.TaskSourceResult("WEEEK", ok=False, error="HTTP 500"),
    )
    monkeypatch.setattr(
        task_sync,
        "get_taiga_tasks",
        lambda: task_sync.TaskSourceResult("Taiga", enabled=False),
    )
    sent = []
    monkeypatch.setattr(task_sync.notify, "send", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(task_sync.ops_store, "record_run", lambda *_args: None)
    monkeypatch.setattr(task_sync.ops_store, "heartbeat", lambda *_args: None)

    task_sync.run()

    assert sent
    assert "недоступны" in sent[0][0]
