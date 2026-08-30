"""Юнит-тесты общих библиотек инфры (v3.0). Чистые функции + безопасные round-trip в ops_db."""
import pytest
import requests
import llm
import cost_guard
import retry
import ops_store
import db
import provider_health
import router
import task_sync
import praisonaiagents
from praisonaiagents.approval.registry import ApprovalRegistry


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


def test_token_count_handles_russian_with_named_local_method():
    count, method = llm.token_count("Проверка русского текста")
    assert count > 0
    assert method in {"local_o200k", "heuristic"}

def test_is_empty():
    assert llm._is_empty("") and llm._is_empty("   ") and llm._is_empty(None)

def test_not_empty():
    assert not llm._is_empty("x")


def test_clean_model_output_removes_reasoning_and_final_wrappers():
    raw = "<think>private reasoning</think><final>Короткий ответ</final>"
    assert llm.clean_model_output(raw) == "Короткий ответ"


def test_clean_model_output_drops_unfinished_reasoning_block():
    assert llm.clean_model_output("Ответ<think>unfinished reasoning") == "Ответ"


def test_clean_model_output_keeps_unclosed_final_content():
    assert llm.clean_model_output("<final>Короткий ответ") == "Короткий ответ"


def test_task_digest_fingerprint_ignores_input_order_but_tracks_decision_fields():
    first = {"source": "WEEEK", "id": "1", "title": "Позвонить", "status": "В работе"}
    second = {"source": "Taiga", "id": "2", "title": "Исправить", "status": "New"}
    assert task_sync.task_digest_fingerprint([first, second]) == task_sync.task_digest_fingerprint([second, first])
    changed = dict(first, status="Завершено")
    assert task_sync.task_digest_fingerprint([changed, second]) != task_sync.task_digest_fingerprint([first, second])


def test_task_sync_skips_llm_when_source_data_is_unchanged(monkeypatch):
    tasks = [{"source": "WEEEK", "id": "1", "title": "Позвонить", "status": "В работе"}]
    fingerprint = task_sync.task_digest_fingerprint(tasks)
    sent = []
    runs = []
    monkeypatch.setattr(task_sync, "init_db", lambda: None)
    monkeypatch.setattr(task_sync.ops_store, "init", lambda: None)
    monkeypatch.setattr(task_sync, "get_weeek_tasks", lambda: tasks)
    monkeypatch.setattr(task_sync, "get_taiga_tasks", lambda: [])
    monkeypatch.setattr(task_sync, "calculate_kpis", lambda *_args: ({"completion_rate": 0}, {}))
    monkeypatch.setattr(task_sync, "save_snapshot", lambda *_args: None)
    monkeypatch.setattr(task_sync, "get_historical_snapshots", lambda *_args: [])
    monkeypatch.setattr(
        task_sync.ops_store, "get_automation_state", lambda *_args: {"fingerprint": fingerprint}
    )
    monkeypatch.setattr(task_sync.ops_store, "record_run", lambda *args: runs.append(args))
    monkeypatch.setattr(task_sync.notify, "send", lambda message: sent.append(message) or True)
    monkeypatch.setattr(
        task_sync.llm, "run", lambda *_args: (_ for _ in ()).throw(AssertionError("LLM called"))
    )

    task_sync.run()

    assert sent and "AI-анализ не запускался" in sent[0]
    assert runs[0][1] == "unchanged"


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
    assert llm.normalize_groq_model("qwen/qwen3-32b") == "openai/gpt-oss-120b"
    assert llm.normalize_groq_model(
        "groq/meta-llama/llama-4-scout-17b-16e-instruct", litellm=True
    ) == "groq/qwen/qwen3.6-27b"
    assert llm.GROQ_VISION_MODEL == "qwen/qwen3.6-27b"


def test_run_falls_back_from_empty_groq_to_configured_provider(monkeypatch):
    used = []

    class PrimaryAgent:
        def start(self, _prompt):
            return ""

    class FallbackAgent:
        def __init__(self, model, **_kwargs):
            used.append(model)

        def start(self, _prompt):
            return "ответ через Gemini"

    primary = PrimaryAgent()
    llm._AGENT_BUILD[id(primary)] = ("groq/openai/gpt-oss-120b", {"name": "test"})
    monkeypatch.setattr(llm, "fallback_models", lambda _primary=None: ["gemini/gemini-3.6-flash"])
    monkeypatch.setattr(praisonaiagents, "Agent", FallbackAgent)
    monkeypatch.setattr(llm, "_record", lambda *args, **kwargs: None)

    result = llm.run(primary, "проверка", "orchestrator", attempts=1)

    assert result == "ответ через Gemini"
    assert used == ["gemini/gemini-3.6-flash"]


def test_run_falls_back_when_primary_contains_only_reasoning(monkeypatch):
    class PrimaryAgent:
        def start(self, _prompt):
            return "<think>unfinished internal reasoning</think>"

    primary = PrimaryAgent()
    llm._AGENT_BUILD[id(primary)] = ("groq/openai/gpt-oss-120b", {"name": "test"})
    monkeypatch.setattr(
        llm,
        "_fallback_text",
        lambda *_args, **_kwargs: ("готово", "gemini/gemini-3.6-flash", {}),
    )
    monkeypatch.setattr(llm, "_record_fallback_usage", lambda *_args, **_kwargs: None)

    assert llm.run(primary, "проверка", "orchestrator", attempts=1) == "готово"


def test_run_records_provider_usage_exposed_by_agent(monkeypatch):
    recorded = {}

    class PrimaryAgent:
        def __init__(self):
            self.after = False

        @property
        def cost_summary(self):
            if self.after:
                return {"tokens_in": 120, "tokens_out": 30, "llm_calls": 1, "cost": 0}
            return {"tokens_in": 0, "tokens_out": 0, "llm_calls": 0, "cost": 0}

        def start(self, _prompt):
            self.after = True
            return "готово"

    agent = PrimaryAgent()
    llm._AGENT_BUILD[id(agent)] = ("groq/openai/gpt-oss-120b", {"name": "test"})
    monkeypatch.setattr(cost_guard, "record_usage", lambda *args, **kwargs: recorded.update(kwargs))

    assert llm.run(agent, "проверка", "chief_of_staff", attempts=1) == "готово"
    assert recorded["token_count_source"] == "provider"
    assert recorded["latency_ms"] >= 0
    assert recorded["meta"]["llm_calls"] == 1


def test_groq_chat_returns_compatible_response_from_fallback(monkeypatch):
    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", False)
    class Completions:
        @staticmethod
        def create(**_kwargs):
            raise TimeoutError("Groq unavailable")

    class Client:
        class Chat:
            completions = Completions()

        chat = Chat()

    monkeypatch.setattr(
        llm,
        "_fallback_text",
        lambda *_args, **_kwargs: (
            "{\"tool\":\"answer\"}", "gemini/gemini-3.6-flash", {}
        ),
        raising=False,
    )

    response = llm.groq_chat(Client(), "orchestrator", [{"role": "user", "content": "route"}])

    assert response.choices[0].message.content == '{"tool":"answer"}'


def test_groq_chat_records_provider_cache_and_latency(monkeypatch):
    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", False)
    recorded = {}

    class Details:
        cached_tokens = 80

    class Usage:
        prompt_tokens = 100
        completion_tokens = 20
        prompt_tokens_details = Details()

    class Response:
        id = "request-test"
        usage = Usage()
        choices = [type("Choice", (), {"message": type("Message", (), {"content": "готово"})()})()]

    class Completions:
        @staticmethod
        def create(**_kwargs):
            return Response()

    class Client:
        class Chat:
            completions = Completions()

        chat = Chat()

    monkeypatch.setattr(cost_guard, "record_usage", lambda *args, **kwargs: recorded.update(kwargs))

    llm.groq_chat(Client(), "orchestrator", [{"role": "user", "content": "route"}])

    assert recorded["cached_prompt_tokens"] == 80
    assert recorded["token_count_source"] == "provider"
    assert recorded["latency_ms"] >= 0
    assert recorded["meta"]["cache_metrics_available"] is True


def test_groq_chat_falls_back_when_provider_returns_reasoning_only(monkeypatch):
    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", False)
    class Usage:
        prompt_tokens = 10
        completion_tokens = 10
        prompt_tokens_details = None

    class Message:
        content = "<think>unfinished internal reasoning</think>"

    class Choice:
        message = Message()

    class Response:
        id = "request-test"
        usage = Usage()
        choices = [Choice()]

    class Completions:
        @staticmethod
        def create(**_kwargs):
            return Response()

    class Client:
        class Chat:
            completions = Completions()

        chat = Chat()

    monkeypatch.setattr(cost_guard, "record_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        llm,
        "_fallback_text",
        lambda *_args, **_kwargs: ("готово", "gemini/gemini-3.6-flash", {}),
    )
    monkeypatch.setattr(llm, "_record_fallback_usage", lambda *_args, **_kwargs: None)

    response = llm.groq_chat(
        Client(), "orchestrator", [{"role": "user", "content": "route"}]
    )

    assert response.choices[0].message.content == "готово"


def test_fallback_text_supplies_default_agent_instructions(monkeypatch):
    created = []

    class FallbackAgent:
        def __init__(self, model, **kwargs):
            created.append((model, kwargs))
            if not any(kwargs.get(key) for key in ("name", "role", "goal", "backstory", "instructions")):
                raise ValueError("agent identity is required")

        def start(self, _prompt):
            return "готово"

    monkeypatch.setattr(llm, "fallback_models", lambda _primary=None: ["gemini/gemini-3.6-flash"])
    monkeypatch.setattr(praisonaiagents, "Agent", FallbackAgent)

    text, model, usage = llm._fallback_text(
        "проверка", "orchestrator", primary_model="groq/test"
    )

    assert text == "готово"
    assert model == "gemini/gemini-3.6-flash"
    assert usage["llm_calls"] == 0
    assert created[0][1]["instructions"]


def test_vision_skips_disabled_qwen_proxy(monkeypatch, tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(b"not-a-real-image")
    monkeypatch.setattr(llm, "FREEQWEN_ENABLED", False)
    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", False)
    monkeypatch.setattr(llm, "ALLOW_EXTERNAL_FALLBACK", True)
    monkeypatch.setattr(llm, "_freeqwen_chat", lambda *_args, **_kwargs: pytest.fail("Qwen must stay disabled"))
    monkeypatch.setattr(llm, "_groq_vision_analyze", lambda *_args, **_kwargs: "image understood")
    monkeypatch.setattr(llm, "_record", lambda *args, **kwargs: None)

    assert llm.vision_analyze("describe", str(image)) == "image understood"


def test_openmodel_does_not_retry_permanent_payment_error(monkeypatch):
    response = requests.Response()
    response.status_code = 402
    response.url = "https://api.openmodel.ai/v1/messages"
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(True)
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(requests.HTTPError):
        llm._openmodel_chat("ping", timeout=1)

    assert len(calls) == 1

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

    class FakeOpener:
        def open(self, request, timeout):
            calls["url"] = request.full_url
            calls["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setenv("OLLAMA_API_BASE", "http://example.local:11434")
    monkeypatch.setenv("OLLAMA_CHECK_TIMEOUT", "1.25")
    monkeypatch.setattr(router.urllib.request, "build_opener", lambda *_args: FakeOpener())
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
    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(router.urllib.request, "build_opener", lambda *_args: FakeOpener())
    monkeypatch.setattr(router, "_model_overrides", lambda: {})
    router._ollama_cache.update({"models": set(), "ok": False, "ts": 0, "error": ""})

    assert router._check_ollama(required_model="qwen3.6:35b-a3b-q4_K_M") is False
    assert router.get_model("task_sync") == router.LOCAL_WORK_MODEL


def test_local_first_chat_does_not_call_external_client(monkeypatch):
    class Completions:
        @staticmethod
        def create(**_kwargs):
            pytest.fail("external provider must not be called")

    class Client:
        class Chat:
            completions = Completions()

        chat = Chat()

    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", True)
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_args, **_kwargs: ("локальный ответ", {"prompt_tokens": 4, "completion_tokens": 2}),
    )
    monkeypatch.setattr(cost_guard, "record_usage", lambda *_args, **_kwargs: None)

    response = llm.groq_chat(
        Client(), "orchestrator", [{"role": "user", "content": "привет"}]
    )

    assert response.choices[0].message.content == "локальный ответ"


def test_local_vision_prevents_external_fallback(monkeypatch, tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", True)
    monkeypatch.setattr(llm, "ALLOW_EXTERNAL_FALLBACK", False)
    monkeypatch.setattr(
        llm,
        "_ollama_chat",
        lambda *_args, **_kwargs: ("на изображении ошейник", {"prompt_tokens": 8, "completion_tokens": 4}),
    )
    monkeypatch.setattr(
        llm,
        "_groq_vision_analyze",
        lambda *_args, **_kwargs: pytest.fail("external vision must not be called"),
    )
    monkeypatch.setattr(cost_guard, "record_usage", lambda *_args, **_kwargs: None)

    assert llm.vision_analyze("Опиши", str(image)) == "на изображении ошейник"


def test_local_vision_reserves_budget_for_thinking_model(monkeypatch, tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(b"image")
    captured = {}
    monkeypatch.setattr(llm, "LOCAL_FIRST_ENABLED", True)
    monkeypatch.setattr(llm, "ALLOW_EXTERNAL_FALLBACK", False)
    monkeypatch.setenv("LOCAL_VISION_MAX_TOKENS", "2200")

    def fake_ollama(messages, _model, **kwargs):
        captured["prompt"] = messages[0]["content"]
        captured["max_tokens"] = kwargs["max_tokens"]
        return "готово", {"prompt_tokens": 8, "completion_tokens": 4}

    monkeypatch.setattr(llm, "_ollama_chat", fake_ollama)
    monkeypatch.setattr(cost_guard, "record_usage", lambda *_args, **_kwargs: None)

    assert llm.vision_analyze("Опиши", str(image)) == "готово"
    assert "Сразу сформулируй конечный ответ" in captured["prompt"]
    assert captured["max_tokens"] == 2200


def test_praison_critical_approval_is_scoped_to_arguments_and_agent():
    baseline = ApprovalRegistry._approval_cache_key(
        "execute_command", {"command": "ls"}, "worker-a"
    )

    assert baseline != ApprovalRegistry._approval_cache_key(
        "execute_command", {"command": "env"}, "worker-a"
    )
    assert baseline != ApprovalRegistry._approval_cache_key(
        "execute_command", {"command": "ls"}, "worker-b"
    )


def test_smart_router_bounds_context_and_uses_configured_executable(monkeypatch, tmp_path):
    executable = tmp_path / "amori-ai"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    captured = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = '{"answer": "готово"}'

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(llm, "SMART_ROUTER_ENABLED", True)
    monkeypatch.setattr(llm, "SMART_ROUTER_COMMAND", str(executable))
    monkeypatch.setattr(llm, "SMART_ROUTER_MAX_CHARS", 1200)
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    result = llm.smart_router_answer(
        "начало" + "x" * 3000 + "конец",
        cwd=str(tmp_path),
        routing_prompt="короткий вопрос",
    )

    assert result == "готово"
    assert captured["command"][0] == str(executable)
    assert captured["command"][4:6] == ["--routing-text", "короткий вопрос"]
    assert len(captured["command"][-1]) < 1300
    assert "контекст сокращён" in captured["command"][-1]
    assert captured["command"][-1].endswith("конец")


def test_provider_health_reports_ollama_port_timeout(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://100.77.9.84:11434")
    monkeypatch.setattr(provider_health, "_tcp_probe", lambda *_args, **_kwargs: (False, "timeout"))

    icon, status, fix = provider_health.check_ollama()

    assert icon == "⚪"
    assert "порт недоступен" in status
    assert "brew services restart ollama" in fix


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


def test_provider_health_accepts_gemini_as_working_brain():
    ok, summary = provider_health.brain_summary(
        ("🔴", "HTTP 402", "top up"),
        ("🔴", "timeout", "check network"),
        ("🟢", "ok (gemini-3.6-flash)", ""),
    )

    assert ok is True
    assert "Gemini" in summary
    assert "лежит" not in summary


def test_provider_health_prefers_local_brain():
    ok, summary = provider_health.brain_summary(
        ("⏸", "disabled", ""),
        ("⏸", "disabled", ""),
        ("⏸", "disabled", ""),
        ("🟢", "ok", ""),
    )

    assert ok is True
    assert "Локальный мозг" in summary
    assert "Codex/Claude" in summary


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
    cost_guard.record_usage(
        "pytest_agent", "groq/openai/gpt-oss-120b", 100, 50, source="pytest",
        cached_prompt_tokens=25, latency_ms=123, token_count_source="provider",
        meta={"cache_metrics_available": True},
    )
    try:
        row = db.query(
            "SELECT cached_prompt_tokens, latency_ms, token_count_source "
            "FROM llm_usage WHERE agent='pytest_agent' ORDER BY id DESC LIMIT 1",
            dbname="ops_db",
        )[0]
        assert row == (25, 123, "provider")
    finally:
        db.execute("DELETE FROM llm_usage WHERE agent='pytest_agent'", dbname="ops_db")


def test_usage_summary_roundtrip():
    cost_guard.record_usage("pytest_summary", "groq/openai/gpt-oss-120b", 40, 10, source="pytest")
    try:
        summary = cost_guard.usage_summary(1)
        assert summary["calls"] >= 1
        assert summary["total_tokens"] >= 50
        assert summary["provider_calls"] >= 0
        assert summary["cache_observed_prompt_tokens"] >= 0
    finally:
        db.execute("DELETE FROM llm_usage WHERE agent='pytest_summary'", dbname="ops_db")


def test_automation_state_roundtrip():
    ops_store.set_automation_state("pytest_state", {"fingerprint": "abc"})
    try:
        assert ops_store.get_automation_state("pytest_state") == {"fingerprint": "abc"}
    finally:
        db.execute("DELETE FROM automation_state WHERE key='pytest_state'", dbname="ops_db")

def test_heartbeat_roundtrip():
    ops_store.heartbeat("pytest_component", "ok", {"t": 1})
    try:
        rows = db.query("SELECT status FROM infra_heartbeats WHERE component='pytest_component'", dbname="ops_db")
        assert rows and rows[0][0] == "ok"
    finally:
        db.execute("DELETE FROM infra_heartbeats WHERE component='pytest_component'", dbname="ops_db")
