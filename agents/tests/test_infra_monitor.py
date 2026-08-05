import infra_monitor


def test_telegram_check_reports_one_grouped_warning(monkeypatch):
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    monkeypatch.setenv("ORCHESTRATOR_BOT_TOKEN", "configured")
    monkeypatch.setenv("SUPPORT_BOT_TOKEN", "configured")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "configured")
    monkeypatch.setattr(
        infra_monitor,
        "telegram_bot_ok",
        lambda env_name: (env_name != "SUPPORT_BOT_TOKEN", "TLS timeout"),
    )

    infra_monitor.check_telegram()

    assert len(infra_monitor.warn) == 1
    assert "Support" in infra_monitor.warn[0]
    assert "configured" not in infra_monitor.warn[0]


def test_telegram_check_is_ok_when_all_bots_reach_api(monkeypatch):
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    for name in ("ORCHESTRATOR_BOT_TOKEN", "SUPPORT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(infra_monitor, "telegram_bot_ok", lambda _env_name: (True, "ok"))

    infra_monitor.check_telegram()

    assert not infra_monitor.warn
    assert "telegram bots 3/3" in infra_monitor.ok
