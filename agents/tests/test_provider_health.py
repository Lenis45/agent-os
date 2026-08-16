from unittest.mock import Mock, patch

import requests

import provider_health


def test_provider_probe_retries_transient_network_failure():
    response = Mock(status_code=200)
    with patch("requests.request", side_effect=[requests.Timeout("TLS timeout"), response]) as request:
        with patch("provider_health.time.sleep"):
            result = provider_health._get("https://example.test", {}, 1)

    assert result is response
    assert request.call_count == 2


def test_provider_probe_does_not_retry_permanent_http_error():
    response = Mock(status_code=401)
    with patch("requests.request", return_value=response) as request:
        result = provider_health._post("https://example.test", {}, {"ping": True}, 1)

    assert result is response
    request.assert_called_once()


def test_groq_health_checks_replacement_with_real_completion(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "configured")
    monkeypatch.setenv("DEFAULT_GROQ_MODEL", "openai/gpt-oss-120b")
    models = Mock(status_code=200)
    models.json.return_value = {"data": [{"id": "openai/gpt-oss-120b"}]}
    completion = Mock(status_code=200)
    completion.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
    monkeypatch.setattr(provider_health, "_get", lambda *_args, **_kwargs: models)
    post = Mock(return_value=completion)
    monkeypatch.setattr(provider_health, "_post", post)

    icon, status, fix = provider_health.check_groq()

    assert icon == "🟢"
    assert status == "ok (openai/gpt-oss-120b)"
    assert fix == ""
    assert post.call_args.args[2]["model"] == "openai/gpt-oss-120b"


def test_groq_health_migrates_retired_model_setting(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "configured")
    monkeypatch.setenv("DEFAULT_GROQ_MODEL", "llama-3.3-70b-versatile")
    models = Mock(status_code=200)
    models.json.return_value = {"data": [{"id": "openai/gpt-oss-120b"}]}
    completion = Mock(status_code=200)
    completion.json.return_value = {"choices": [{}]}
    monkeypatch.setattr(provider_health, "_get", lambda *_args, **_kwargs: models)
    post = Mock(return_value=completion)
    monkeypatch.setattr(provider_health, "_post", post)

    result = provider_health.check_groq()

    assert result[0] == "🟢"
    assert post.call_args.args[2]["model"] == "openai/gpt-oss-120b"


def test_groq_health_reports_missing_target_model(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "configured")
    monkeypatch.setenv("DEFAULT_GROQ_MODEL", "openai/gpt-oss-120b")
    models = Mock(status_code=200)
    models.json.return_value = {"data": [{"id": "qwen/qwen3.6-27b"}]}
    monkeypatch.setattr(provider_health, "_get", lambda *_args, **_kwargs: models)

    icon, status, fix = provider_health.check_groq()

    assert icon == "🔴"
    assert "недоступна" in status
    assert "DEFAULT_GROQ_MODEL=openai/gpt-oss-120b" in fix
