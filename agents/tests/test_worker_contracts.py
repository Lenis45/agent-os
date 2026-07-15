"""Контракты качества для личной AI-команды Amori."""
import pathlib

import project_manager
import worker_handlers
import audit_agent_outputs
import content_factory
import email_agent
import knowledge_curator
import support_agent


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
