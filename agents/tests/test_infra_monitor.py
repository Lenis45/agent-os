import infra_monitor


def configure_state(monkeypatch, tmp_path):
    monkeypatch.setattr(infra_monitor, "TELEGRAM_STATE_FILE", str(tmp_path / "telegram.json"))


def test_telegram_check_reports_one_grouped_hard_warning(monkeypatch, tmp_path):
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    configure_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCHESTRATOR_BOT_TOKEN", "configured")
    monkeypatch.setenv("SUPPORT_BOT_TOKEN", "configured")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "configured")
    monkeypatch.setattr(
        infra_monitor,
        "telegram_bot_ok",
        lambda env_name: (env_name != "SUPPORT_BOT_TOKEN", "HTTP 401"),
    )

    infra_monitor.check_telegram()

    assert len(infra_monitor.warn) == 1
    assert "Support" in infra_monitor.warn[0]
    assert "configured" not in infra_monitor.warn[0]


def test_telegram_check_is_ok_when_all_bots_reach_api(monkeypatch, tmp_path):
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    configure_state(monkeypatch, tmp_path)
    for name in ("ORCHESTRATOR_BOT_TOKEN", "SUPPORT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(infra_monitor, "telegram_bot_ok", lambda _env_name: (True, "ok"))

    infra_monitor.check_telegram()

    assert not infra_monitor.warn
    assert "telegram bots 3/3" in infra_monitor.ok


def test_telegram_check_suppresses_single_transient_failure(monkeypatch, tmp_path):
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    configure_state(monkeypatch, tmp_path)
    monkeypatch.setattr(infra_monitor, "TELEGRAM_TRANSIENT_FAILURE_THRESHOLD", 3)
    for name in ("ORCHESTRATOR_BOT_TOKEN", "SUPPORT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(
        infra_monitor,
        "telegram_bot_ok",
        lambda env_name: (env_name != "SUPPORT_BOT_TOKEN", "TLS handshake timed out"),
    )

    infra_monitor.check_telegram()

    assert not infra_monitor.warn
    assert any("Support 1/3" in entry for entry in infra_monitor.ok)


def test_telegram_check_warns_after_repeated_transient_failure(monkeypatch, tmp_path):
    configure_state(monkeypatch, tmp_path)
    monkeypatch.setattr(infra_monitor, "TELEGRAM_TRANSIENT_FAILURE_THRESHOLD", 3)
    for name in ("ORCHESTRATOR_BOT_TOKEN", "SUPPORT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(
        infra_monitor,
        "telegram_bot_ok",
        lambda env_name: (env_name != "SUPPORT_BOT_TOKEN", "TLS handshake timed out"),
    )

    for _ in range(3):
        infra_monitor.ok.clear()
        infra_monitor.warn.clear()
        infra_monitor.check_telegram()

    assert len(infra_monitor.warn) == 1
    assert "Support" in infra_monitor.warn[0]
    assert "3 проверки подряд" in infra_monitor.warn[0]


def test_telegram_recovery_resets_transient_failure_streak(monkeypatch, tmp_path):
    configure_state(monkeypatch, tmp_path)
    monkeypatch.setattr(infra_monitor, "TELEGRAM_TRANSIENT_FAILURE_THRESHOLD", 3)
    for name in ("ORCHESTRATOR_BOT_TOKEN", "SUPPORT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(name, "configured")
    status = {"support_ok": False}
    monkeypatch.setattr(
        infra_monitor,
        "telegram_bot_ok",
        lambda env_name: (
            env_name != "SUPPORT_BOT_TOKEN" or status["support_ok"],
            "ok" if env_name != "SUPPORT_BOT_TOKEN" or status["support_ok"] else "TLS timeout",
        ),
    )

    infra_monitor.check_telegram()
    status["support_ok"] = True
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    infra_monitor.check_telegram()
    status["support_ok"] = False
    infra_monitor.ok.clear()
    infra_monitor.warn.clear()
    infra_monitor.check_telegram()

    assert not infra_monitor.warn
    assert any("Support 1/3" in entry for entry in infra_monitor.ok)


def test_telegram_probe_retries_transient_network_error(monkeypatch):
    calls = {"count": 0}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def urlopen(_request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("temporary VPN route failure")
        return Response()

    monkeypatch.setenv("ORCHESTRATOR_BOT_TOKEN", "configured")
    monkeypatch.setattr(infra_monitor.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(infra_monitor.time, "sleep", lambda _seconds: None)

    available, reason = infra_monitor.telegram_bot_ok("ORCHESTRATOR_BOT_TOKEN")

    assert available is True
    assert reason == "ok"
    assert calls["count"] == 2
