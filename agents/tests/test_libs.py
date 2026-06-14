"""Юнит-тесты общих библиотек инфры (v3.0). Чистые функции + безопасные round-trip в ops_db."""
import pytest
import llm
import cost_guard
import retry
import ops_store
import db


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
    assert llm.count_tokens("groq/llama", "hello " * 10) > 0

def test_count_tokens_empty_is_zero():
    assert llm.count_tokens("groq/llama", "") == 0

def test_is_empty():
    assert llm._is_empty("") and llm._is_empty("   ") and llm._is_empty(None)

def test_not_empty():
    assert not llm._is_empty("x")


# ── cost_guard: тиры и цена ───────────────────────────────────────
def test_tier_free_local():
    assert cost_guard.model_tier("groq/llama-3.3-70b-versatile") == 3
    assert cost_guard.model_tier("ollama/gpt-oss:20b") == 3

def test_tier_paid_api():
    assert cost_guard.model_tier("claude-sonnet") == 2
    assert cost_guard.model_tier("gpt-4o") == 2

def test_tier_manual():
    assert cost_guard.model_tier("claude-pro-web") == 1

def test_cost_free_is_zero():
    assert cost_guard.estimate_cost_rub("groq/llama", 1000, 1000) == 0.0

def test_cost_paid_positive():
    assert cost_guard.estimate_cost_rub("claude-sonnet", 10000, 4000) > 0

def test_guard_passthrough_free():
    # free-модель никогда не даунгрейдится
    assert cost_guard.guard_model("groq/llama-3.3-70b-versatile", "test") == "groq/llama-3.3-70b-versatile"


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
    cost_guard.record_usage("pytest_agent", "groq/llama", 100, 50, source="pytest")
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
