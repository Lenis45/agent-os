"""Контракты качества для личной AI-команды Amori."""
import pathlib

import project_manager
import worker_handlers
import audit_agent_outputs
import content_factory
import email_agent
import knowledge_curator
import orchestrator
import support_agent
import base_agent
import tasks


AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent


def test_content_writer_blocks_unverified_product_claims(monkeypatch):
    monkeypatch.setattr(worker_handlers.llm, "run", lambda *a, **k: "Показываем геолокацию в реальном времени и здоровье.")
    out = worker_handlers.content_writer({"title": "Пост", "spec": "подготовь пост"})
    assert "Нужна редактура перед публикацией" in out
    assert "реальном времени" not in out
    assert "здоров" not in out.lower()


def test_content_review_task_routes_to_reviewer():
    assignee = project_manager._pick_assignee(
        role="writer",
        dom="content",
        title="Тестирование и корректировка поста",
        spec="проверь готовый текст перед публикацией",
    )
    assert assignee == "content_reviewer"


def test_content_visual_task_routes_to_designer():
    assignee = project_manager._pick_assignee(
        role=None,
        dom="content",
        title="Подготовить картинку к посту",
        spec="визуал для Telegram",
    )
    assert assignee == "content_designer"


def test_support_knowledge_does_not_promise_realtime_or_alerts():
    src = (AGENTS_DIR / "support_agent.py").read_text(encoding="utf-8").lower()
    forbidden = [
        "позволяет отслеживать местоположение питомца в реальном времени",
        "уведомления если питомец вышел",
        "данные передаются в приложение в реальном времени",
    ]
    assert not any(x in src for x in forbidden)


def test_support_answer_normalizer_removes_markdown_for_telegram():
    raw = "**Важно:** Amori ещё в разработке.\n\n* Мы не обещаем точные сроки.\n`Команда ответит позже.`"
    out = support_agent.normalize_telegram_answer(raw)
    assert "**" not in out
    assert "`" not in out
    assert "* " not in out
    assert "Важно:" in out
    assert "Команда ответит позже." in out


def test_support_answer_normalizer_shortens_long_reply():
    raw = " ".join(["Это длинный ответ для клиента."] * 60)
    out = support_agent.normalize_telegram_answer(raw, max_chars=180)
    assert len(out) <= 181
    assert out.endswith((".", "!", "?", "…"))


def test_orchestrator_reply_normalizer_removes_markdown_for_telegram():
    raw = "**Итог:** нужно проверить фото.\n\n## Дальше\n`Отвечу коротко.`"
    out = orchestrator.normalize_telegram_reply(raw)
    assert "**" not in out
    assert "`" not in out
    assert "##" not in out
    assert "Итог:" in out
    assert "Отвечу коротко." in out


def test_orchestrator_send_msg_retries_transient_telegram_error(monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("handshake operation timed out")
        return FakeResponse()

    monkeypatch.setenv("ORCHESTRATOR_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_MY_ID", "123")
    monkeypatch.setattr(orchestrator.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)

    assert orchestrator.send_msg("готово", "123") is True
    assert calls["count"] == 2


def test_dev_worker_contract_forbids_fake_applied_work():
    src = (AGENTS_DIR / "worker_handlers.py").read_text(encoding="utf-8")
    assert "нет прямого доступа менять репозитории" in src
    assert "ПРЕДЛОЖЕННОЕ РЕШЕНИЕ" in src


def test_report_audit_flags_fake_publication_and_product_claim():
    issues = audit_agent_outputs._hits("Пост опубликован. GPS показывает в реальном времени.")
    assert "unverified_external_action:published" in issues
    assert "unsupported_product_claim:real-time location" in issues


def test_content_factory_does_not_mark_unconfigured_channel_as_real_publish(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    result, info = content_factory._do_publish("telegram", {"body": "hello"})
    assert result == "manual"
    assert "ручной публикации" in info


def test_content_factory_blocks_empty_generation_before_approval(monkeypatch):
    inserted = {}
    notified = []

    monkeypatch.setattr(worker_handlers, "content_writer", lambda *_: "")
    monkeypatch.setattr(
        worker_handlers,
        "content_designer",
        lambda *_: (_ for _ in ()).throw(AssertionError("designer must not run without copy")),
    )
    monkeypatch.setattr(
        content_factory,
        "_insert",
        lambda *args, **kwargs: inserted.update(kwargs) or 41,
    )
    monkeypatch.setattr(content_factory, "_notify_preview", lambda *args: notified.append(args))
    monkeypatch.setattr(content_factory.report_mod, "report", lambda *args, **kwargs: None)

    assert content_factory.create("Тестовый бриф") == 41
    assert inserted["status"] == "failed"
    assert inserted["meta"]["failed_stage"] == "copy"
    assert notified == []


def test_content_factory_rejects_approval_without_required_artifacts(monkeypatch):
    monkeypatch.setattr(
        content_factory,
        "get",
        lambda _cid: {
            "id": 7,
            "kind": "post",
            "channel": "telegram",
            "body": "",
            "image_brief": "",
        },
    )
    monkeypatch.setattr(
        content_factory,
        "_set_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid item changed status")),
    )

    result = content_factory.approve(7)

    assert result["ok"] is False
    assert result["error"] == "content_incomplete"
    assert "текст" in result["message"]


def test_worker_does_not_complete_task_with_empty_result(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "claim", lambda _agent: {"id": 9, "title": "Проверка", "project_id": 3})
    monkeypatch.setattr(tasks, "dep_results", lambda _tid: [])
    monkeypatch.setattr(tasks, "start", lambda tid: calls.append(("start", tid)))
    monkeypatch.setattr(tasks, "complete", lambda tid, result: calls.append(("complete", tid, result)))
    monkeypatch.setattr(tasks, "fail", lambda tid, error: calls.append(("fail", tid, str(error))))
    monkeypatch.setattr(base_agent.report_mod, "report", lambda *args, **kwargs: None)
    monkeypatch.setitem(base_agent.HANDLERS, "empty_worker", lambda _task: "   ")

    assert base_agent.process_one("empty_worker") is True
    assert any(call[0] == "fail" for call in calls)
    assert not any(call[0] == "complete" for call in calls)


def test_completing_last_task_reconciles_project(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, query, params=None):
            statements.append((" ".join(query.split()), params))

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tasks, "_conn", Connection)

    tasks.complete(12, "готово")

    assert any("UPDATE projects p" in query for query, _params in statements)


def test_email_agent_rewrites_unsafe_marketing_body(monkeypatch):
    monkeypatch.setattr(email_agent.llm, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(
        email_agent.llm,
        "run",
        lambda *a, **k: '{"subject":"Amori","body":"GPS в реальном времени и мониторинг здоровья"}',
    )
    out = email_agent.generate_email({"name": "Анна", "pet_type": "собаки"})
    assert "реальном времени" not in out["body"]
    assert "здоров" not in out["body"].lower()
    assert "параметры продукта" in out["body"].lower()


def test_obsidian_path_is_kept_inside_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_curator, "VAULT", str(tmp_path))
    path = knowledge_curator.save_to_obsidian("../outside", "note", "body")
    assert str(path).startswith(str(tmp_path))
    assert (tmp_path / "outside" / "note.md").exists()
