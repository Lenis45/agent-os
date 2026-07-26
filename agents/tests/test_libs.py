"""Юнит-тесты общих библиотек инфры (v3.0). Чистые функции + безопасные round-trip в ops_db."""
import pytest
import llm
import cost_guard
import retry
import ops_store
import db
import provider_health
import router


# ── llm.parse_json ────────────────────────────────────────────────
def test_parse_json_plain():
    assert llm.parse_json('{"a": 1}') == {"a": 1}

def test_parse_json_fenced():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}

def test_parse_json_embedded():
    assert llm.parse_json('бла-бла {"a": 1} конец')["a"] == 1

def test_parse_json_array():
    assert llm.parse_json('[1, 2, 3]') == [1, 2, 3]

def test_parse_json_bad_returns_default():
    assert llm.parse_json('совсем не json', default={}) == {}

def test_parse_json_none():
    assert llm.parse_json(None) is None


# ── llm.count_tokens / _is_empty ──────────────────────────────────
def test_count_tokens_positive():
    assert llm.count_tokens("groq/openai/gpt-oss-120b", "hello " * 10) > 0

def test_count_tokens_empty_is_zero():
    assert llm.count_tokens("groq/openai/gpt-oss-120b", "") == 0

def test_is_empty():
    assert llm._is_empty("") and llm._is_empty("   ") and llm._is_empty(None)

def test_not_empty():
    assert not llm._is_empty("x")


# ── cost_guard: тиры и цена ───────────────────────────────────────
def test_tier_free_local():
    assert cost_guard.model_tier("groq/openai/gpt-oss-120b") == 3
    assert cost_guard.model_tier("ollama/gpt-oss:20b") == 3

def test_tier_paid_api():
    assert cost_guard.model_tier("claude-sonnet") == 2
    assert cost_guard.model_tier("gpt-4o") == 2

def test_tier_manual():
    assert cost_guard.model_tier("claude-pro-web") == 1

def test_cost_free_is_zero():
    assert cost_guard.estimate_cost_rub("groq/openai/gpt-oss-120b", 1000, 1000) == 0.0

def test_cost_paid_positive():
    assert cost_guard.estimate_cost_rub("claude-sonnet", 10000, 4000) > 0

def test_guard_passthrough_free():
    # free-модель никогда не даунгрейдится
    assert cost_guard.guard_model("groq/openai/gpt-oss-120b", "test") == "groq/openai/gpt-oss-120b"

def test_deprecated_groq_model_is_normalized():
    assert llm.normalize_groq_model("llama-3.3-70b-versatile") == "openai/gpt-oss-120b"
    assert llm.normalize_groq_model("groq/llama-3.3-70b-versatile", litellm=True) == "groq/openai/gpt-oss-120b"

def test_unsupported_amori_claims_detected():
    claims = llm.unsupported_product_claims("Ошейник показывает местоположение в реальном времени и мониторит здоровье.")
    assert "real-time location" in claims
    assert "health/activity monitoring" in claims


def test_router_checks_ollama_tags_endpoint(monkeypatch):
    calls = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"models":[{"name":"qwen3.6:35b-a3b-q4_K_M"}]}'

    def fake_urlopen(url, timeout):
        calls["url"] = url
        calls["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("OLLAMA_API_BASE", "http://example.local:11434")
    monkeypatch.setenv("OLLAMA_CHECK_TIMEOUT", "1.25")
    monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
    router._ollama_cache.update({"models": set(), "ok": False, "ts": 0, "error": ""})

    assert router._check_ollama() is True
    assert calls["url"] == "http://example.local:11434/api/tags"
    assert calls["timeout"] == 1.25


def test_router_requires_selected_ollama_model(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"models":[]}'

    monkeypatch.setenv("OLLAMA_API_BASE", "http://example.local:11434")
    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(router, "_model_overrides", lambda: {})
    router._ollama_cache.update({"models": set(), "ok": False, "ts": 0, "error": ""})

    assert router._check_ollama(required_model="qwen3.6:35b-a3b-q4_K_M") is False
    assert router.get_model("task_sync") == router.DEFAULT_GROQ_LITELLM_MODEL


def test_provider_health_reports_ollama_port_timeout(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://100.77.9.84:11434")
    monkeypatch.setattr(provider_health, "_tcp_probe", lambda *_args, **_kwargs: (False, "timeout"))

    icon, status, fix = provider_health.check_ollama()

    assert icon == "⚪"
    assert "порт недоступен" in status
    assert "ollama serve" in fix


def test_provider_health_reports_missing_ollama_models(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"models": []}

    monkeypatch.setenv("OLLAMA_REQUIRED_MODELS", "qwen3.6:35b-a3b-q4_K_M,qwen3.6:27b-q4_K_M")
    monkeypatch.setattr(provider_health, "_tcp_probe", lambda *_args, **_kwargs: (True, "tcp ok"))
    monkeypatch.setattr(provider_health, "_get", lambda *_args, **_kwargs: FakeResponse())

    icon, status, fix = provider_health.check_ollama()

    assert icon == "⚠️"
    assert "нет моделей" in status
    assert "ollama pull qwen3.6:35b-a3b-q4_K_M" in fix


# ── retry ─────────────────────────────────────────────────────────
def test_safe_returns_default_on_error():
    assert retry.safe(lambda: 1 / 0, default="X") == "X"

def test_safe_returns_value():
    assert retry.safe(lambda: 42) == 42

def test_net_retry_exhausts_then_reraises():
    calls = {"n": 0}

    @retry.net_retry(attempts=3, base=0.01)
    def boom():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        boom()
    assert calls["n"] == 3


# ── DB доступность всех контуров ──────────────────────────────────
def test_agents_db_reachable():
    assert db.query("SELECT 1", dbname="agents")[0][0] == 1

def test_ops_db_reachable():
    assert db.query("SELECT 1", dbname="ops_db")[0][0] == 1

def test_customer_db_reachable():
    assert db.query("SELECT 1", dbname="customer_db")[0][0] == 1


# ── ops_db round-trips (с очисткой) ───────────────────────────────
def test_record_run_roundtrip():
    ops_store.record_run("pytest_kind", "ok", {"x": 1})
    try:
        r = ops_store.last_run("pytest_kind")
        assert r is not None and r[1] == "ok"
    finally:
        db.execute("DELETE FROM infra_runs WHERE kind='pytest_kind'", dbname="ops_db")

def test_cost_guard_record_roundtrip():
    cost_guard.record_usage("pytest_agent", "groq/openai/gpt-oss-120b", 100, 50, source="pytest")
    try:
        n = db.query("SELECT count(*) FROM llm_usage WHERE agent='pytest_agent'", dbname="ops_db")[0][0]
        assert n >= 1
    finally:
        db.execute("DELETE FROM llm_usage WHERE agent='pytest_agent'", dbname="ops_db")

def test_heartbeat_roundtrip():
    ops_store.heartbeat("pytest_component", "ok", {"t": 1})
    try:
        rows = db.query("SELECT status FROM infra_heartbeats WHERE component='pytest_component'", dbname="ops_db")
        assert rows and rows[0][0] == "ok"
    finally:
        db.execute("DELETE FROM infra_heartbeats WHERE component='pytest_component'", dbname="ops_db")
