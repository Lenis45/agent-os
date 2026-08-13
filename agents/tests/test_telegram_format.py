import json

import notify
from telegram_format import normalize_plain_text


def test_normalize_plain_text_removes_markdown_artifacts():
    raw = """### 🔴 ТРЕБУЕТ ОТВЕТА
1. **SecretaryAmo_bot** — нужно ответить.
_Дата: 25.07.2026_
`token.json` обновить вручную."""

    out = normalize_plain_text(raw)

    assert "###" not in out
    assert "**" not in out
    assert "_Дата" not in out
    assert "`" not in out
    assert "ТРЕБУЕТ ОТВЕТА" in out
    assert "SecretaryAmo_bot" in out
    assert "Дата: 25.07.2026" in out


def test_normalize_plain_text_converts_markdown_table_to_bullets():
    raw = """| Задача | Ответственный | Срок |
| ------ | -------------- | ---- |
| **Изучить договор** | Денис | Сегодня |
| Отправить ответ | Денис | После подписи |"""

    out = normalize_plain_text(raw)

    assert "| ------ |" not in out
    assert "**" not in out
    assert "• Задача — Ответственный; Срок" in out
    assert "• Изучить договор — Денис; Сегодня" in out
    assert "• Отправить ответ — Денис; После подписи" in out


def test_normalize_plain_text_shortens_at_sentence_boundary():
    raw = "Первое важное действие. " + ("Второе действие слишком длинное. " * 20)

    out = normalize_plain_text(raw, max_chars=80)

    assert len(out) <= 80
    assert out.endswith(".")


def test_normalize_plain_text_drops_unfinished_provider_reasoning():
    raw = "Готовый ответ<think>внутреннее рассуждение без закрывающего тега"

    assert normalize_plain_text(raw) == "Готовый ответ"


def test_notify_send_normalizes_body_before_telegram(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true, "result": {"message_id": 1, "chat": {"id": 123, "type": "private"}}}'

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_MY_ID", "123")
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.send("### Заголовок\n**Важно:** проверить `token.json`") is True

    text = captured["payload"]["text"]
    assert "###" not in text
    assert "**" not in text
    assert "`" not in text
    assert "Заголовок" in text
    assert "Важно:" in text
