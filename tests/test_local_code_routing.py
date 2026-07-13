"""Local-first code tier (free tier zero: own GPU).

Mirror of the frontier routing contract in the opposite direction:

  * code purposes route to the local Ollama coder FIRST when
    chili_code_local_first is on
  * weak or failed local replies escalate through the standard cascade
    (free Groq 70B -> paid) — cheap never caps quality
  * default (non-code) calls never touch the local coder
  * resolution order: explicit JSON override > local > frontier
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import openai_client
from app.config import settings
from app.services.context_brain import llm_gateway


LOCAL_MODEL = "qwen2.5-coder:7b"
LOCAL_REASONING_MODEL = "qwen3:8b"
LOCAL_ESCALATION_MODEL = "qwen2.5-coder:14b"
FRONTIER_MODEL = "claude-fable-5"


@pytest.fixture
def local_configured(monkeypatch):
    monkeypatch.setattr(settings, "ollama_host", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "chili_code_local_model", LOCAL_MODEL)
    monkeypatch.setattr(
        settings,
        "chili_code_local_reasoning_model",
        LOCAL_REASONING_MODEL,
    )
    monkeypatch.setattr(
        settings,
        "chili_code_local_escalation_model",
        LOCAL_ESCALATION_MODEL,
    )
    monkeypatch.setattr(settings, "chili_code_local_first", True)
    monkeypatch.setattr(openai_client, "_safe_log_llm_call", lambda **_k: None)
    return monkeypatch


def _reply(text: str, *, model: str) -> dict:
    return {
        "reply": text,
        "tokens_used": 7,
        "model": model,
        "provider": "ollama",
        "provider_base_url": "http://127.0.0.1:11434/v1",
        "prompt_tokens": 3,
        "completion_tokens": 4,
    }


# ── cascade behavior ─────────────────────────────────────────────────────


def test_chat_uses_local_first_when_requested(local_configured):
    monkeypatch = local_configured
    seen: list[tuple[str, str]] = []

    def fake_call_provider(api_key, base_url, model, messages, prompt, trace_id, **kw):
        seen.append((base_url, model))
        return _reply("def fix():\n    return 42  # a perfectly adequate local reply", model=model)

    monkeypatch.setattr(openai_client, "_call_provider", fake_call_provider)
    monkeypatch.setattr(openai_client, "_chat_groq", lambda *a, **k: pytest.fail("groq reached"))
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: pytest.fail("openai reached"))
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: pytest.fail("gemini reached"))

    result = openai_client.chat(
        [{"role": "user", "content": "implement the fix"}],
        trace_id="code-agent-edit-x",
        model_override=LOCAL_MODEL,
        strict_escalation=False,
    )

    assert result["model"] == LOCAL_MODEL
    assert seen == [("http://127.0.0.1:11434/v1", LOCAL_MODEL)]


def test_chat_local_failure_falls_back_to_cascade(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(openai_client, "_chat_local", lambda *a, **k: None)
    monkeypatch.setattr(
        openai_client, "_chat_groq",
        lambda *a, **k: {"reply": "GROQ-70B", "model": "llama-3.3-70b-versatile", "tokens_used": 5},
    )
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: None)

    result = openai_client.chat(
        [{"role": "user", "content": "implement"}],
        model_override=LOCAL_MODEL,
    )
    assert result["reply"] == "GROQ-70B"


def test_chat_local_only_failure_never_calls_cloud(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(openai_client, "_chat_local", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_groq", lambda *a, **k: pytest.fail("groq reached"))
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: pytest.fail("openai reached"))
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: pytest.fail("gemini reached"))
    monkeypatch.setattr(openai_client, "_chat_frontier", lambda *a, **k: pytest.fail("frontier reached"))

    result = openai_client.chat(
        [{"role": "user", "content": "diagnose and implement"}],
        model_override=LOCAL_MODEL,
        local_only=True,
    )

    assert result["reply"] == ""
    assert result["model"] == "local_error"
    assert result["local_only"] is True
    assert result["premium_calls"] == 0
    assert result["premium_cost_usd"] == 0.0


def test_chat_local_only_success_reports_zero_premium(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(
        openai_client,
        "_chat_local",
        lambda *a, **k: _reply("local diagnostic result", model=LOCAL_MODEL),
    )

    result = openai_client.chat(
        [{"role": "user", "content": "diagnose"}],
        local_only=True,
    )

    assert result["reply"] == "local diagnostic result"
    assert result["local_only"] is True
    assert result["premium_calls"] == 0
    assert result["premium_cost_usd"] == 0.0


def test_chat_local_only_allows_configured_local_escalation_model(local_configured):
    monkeypatch = local_configured
    seen: list[str] = []

    def fake_call_provider(api_key, base_url, model, messages, prompt, trace_id, **kwargs):
        seen.append(model)
        return _reply("bounded local escalation result", model=model)

    monkeypatch.setattr(openai_client, "_call_provider", fake_call_provider)
    monkeypatch.setattr(openai_client, "_chat_groq", lambda *a, **k: pytest.fail("groq reached"))
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: pytest.fail("openai reached"))
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: pytest.fail("gemini reached"))

    result = openai_client.chat(
        [{"role": "user", "content": "repair the remaining contract"}],
        model_override=LOCAL_ESCALATION_MODEL,
        local_only=True,
    )

    assert result["model"] == LOCAL_ESCALATION_MODEL
    assert result["premium_calls"] == 0
    assert seen == [LOCAL_ESCALATION_MODEL]


def test_chat_local_only_allows_configured_reasoning_model(local_configured):
    monkeypatch = local_configured
    seen: list[str] = []

    def fake_call_provider(api_key, base_url, model, messages, prompt, trace_id, **kwargs):
        seen.append(model)
        return _reply("bounded local reasoning result", model=model)

    monkeypatch.setattr(openai_client, "_call_provider", fake_call_provider)
    monkeypatch.setattr(openai_client, "_chat_groq", lambda *a, **k: pytest.fail("groq reached"))
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: pytest.fail("openai reached"))
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: pytest.fail("gemini reached"))

    result = openai_client.chat(
        [{"role": "user", "content": "derive the causal repair mechanism"}],
        model_override=LOCAL_REASONING_MODEL,
        local_only=True,
    )

    assert result["model"] == LOCAL_REASONING_MODEL
    assert result["premium_calls"] == 0
    assert seen == [LOCAL_REASONING_MODEL]


def test_chat_local_only_rejects_unconfigured_model_override(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(
        openai_client,
        "_chat_local",
        lambda *a, **k: pytest.fail("unconfigured model reached Ollama lane"),
    )

    result = openai_client.chat(
        [{"role": "user", "content": "repair"}],
        model_override="operator-unapproved-model",
        local_only=True,
    )

    assert result["model"] == "local_unavailable"
    assert result["premium_calls"] == 0


def test_gateway_local_override_is_forwarded_only_on_local_lane(local_configured):
    monkeypatch = local_configured
    captured = {}
    monkeypatch.setattr(
        llm_gateway,
        "_open_db_session",
        lambda: (_ for _ in ()).throw(RuntimeError("no database")),
    )

    def fake_passthrough(messages, **kwargs):
        captured.update(kwargs)
        return {"reply": "local", "model": kwargs["model_override"], "local_only": True}

    monkeypatch.setattr(llm_gateway, "_passthrough", fake_passthrough)

    result = llm_gateway.gateway_chat(
        [{"role": "user", "content": "repair"}],
        purpose="code_dispatch_edit",
        local_only=True,
        local_model_override=LOCAL_ESCALATION_MODEL,
    )

    assert result["model"] == LOCAL_ESCALATION_MODEL
    assert captured["model_override"] == LOCAL_ESCALATION_MODEL
    assert captured["local_only"] is True


def test_weak_local_reply_escalates(local_configured):
    """The load-bearing quality property: a weak local reply must NOT be
    accepted just because it was free — it escalates to the 70B."""
    monkeypatch = local_configured
    monkeypatch.setattr(
        openai_client, "_call_provider",
        lambda *a, **k: _reply("idk", model=LOCAL_MODEL),  # weak: tiny reply
    )
    monkeypatch.setattr(openai_client, "_is_weak_response", lambda reply, um, strict: True)
    monkeypatch.setattr(
        openai_client, "_chat_groq",
        lambda *a, **k: {"reply": "GROQ-STRONG", "model": "llama-3.3-70b-versatile", "tokens_used": 5},
    )
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: None)

    result = openai_client.chat(
        [{"role": "user", "content": "implement the whole feature"}],
        model_override=LOCAL_MODEL,
        strict_escalation=True,
    )
    assert result["reply"] == "GROQ-STRONG"


def test_chat_default_path_never_touches_local(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(openai_client, "_chat_local", lambda *a, **k: pytest.fail("local reached on default path"))
    monkeypatch.setattr(
        openai_client, "_chat_groq",
        lambda *a, **k: {"reply": "GROQ", "model": "llama-3.3-70b-versatile", "tokens_used": 5},
    )
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: None)

    result = openai_client.chat([{"role": "user", "content": "hi"}], model_override=None)
    assert result["reply"] == "GROQ"


# ── streaming ────────────────────────────────────────────────────────────


def test_chat_stream_local_first_and_fallback(local_configured):
    monkeypatch = local_configured

    def local_ok(api_key, base_url, model, messages, prompt, trace_id, max_tokens=1024):
        assert "11434" in base_url
        yield "ltok", model

    monkeypatch.setattr(openai_client, "_stream_provider", local_ok)
    monkeypatch.setattr(openai_client, "_stream_tier_groq", lambda *a, **k: pytest.fail("groq stream reached"))
    monkeypatch.setattr(openai_client, "_stream_tier_openai", lambda *a, **k: pytest.fail("openai stream reached"))
    monkeypatch.setattr(openai_client, "_stream_tier_gemini", lambda *a, **k: pytest.fail("gemini stream reached"))

    out = list(openai_client.chat_stream(
        [{"role": "user", "content": "x"}], model_override=LOCAL_MODEL,
    ))
    assert [t for t, _m in out] == ["ltok"]


def test_chat_stream_local_error_falls_back(local_configured):
    monkeypatch = local_configured

    def boom(api_key, base_url, model, messages, prompt, trace_id, max_tokens=1024):
        raise RuntimeError("ollama down")
        yield  # pragma: no cover

    def groq_ok(messages, prompt, trace_id, max_tokens, flags):
        yield "gtok", "llama-3.3-70b-versatile"
        flags["done"] = True

    monkeypatch.setattr(openai_client, "_stream_provider", boom)
    monkeypatch.setattr(openai_client, "_stream_tier_groq", groq_ok)

    out = list(openai_client.chat_stream(
        [{"role": "user", "content": "x"}], model_override=LOCAL_MODEL,
    ))
    assert [t for t, _m in out] == ["gtok"]


def test_chat_stream_local_only_error_stops_without_cloud(local_configured):
    monkeypatch = local_configured

    def boom(api_key, base_url, model, messages, prompt, trace_id, max_tokens=1024):
        raise RuntimeError("ollama down")
        yield  # pragma: no cover

    monkeypatch.setattr(openai_client, "_stream_provider", boom)
    monkeypatch.setattr(openai_client, "_chat_local", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_stream_tier_groq", lambda *a, **k: pytest.fail("groq reached"))
    monkeypatch.setattr(openai_client, "_stream_tier_openai", lambda *a, **k: pytest.fail("openai reached"))
    monkeypatch.setattr(openai_client, "_stream_tier_gemini", lambda *a, **k: pytest.fail("gemini reached"))

    out = list(
        openai_client.chat_stream(
            [{"role": "user", "content": "x"}],
            model_override=LOCAL_MODEL,
            local_only=True,
        )
    )
    assert out == []


# ── gateway resolution order ─────────────────────────────────────────────


def _policy(purpose: str, high_stakes: bool = False):
    return SimpleNamespace(purpose=purpose, high_stakes=high_stakes)


def test_gateway_local_override_routes_code_purposes(local_configured):
    assert llm_gateway._local_code_override(_policy("code_dispatch_edit")) == LOCAL_MODEL
    assert llm_gateway._local_code_override(_policy("code_dispatch_plan")) == LOCAL_MODEL
    assert llm_gateway._local_code_override(_policy("code_review")) == LOCAL_MODEL
    assert llm_gateway._local_code_override(_policy("chat_user")) is None
    assert llm_gateway._local_code_override(None) is None


def test_gateway_local_override_off_when_disabled(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(settings, "chili_code_local_first", False)
    assert llm_gateway._local_code_override(_policy("code_dispatch_edit")) is None


def test_gateway_code_defaults_to_local_only_without_premium_opt_in(local_configured):
    monkeypatch = local_configured
    monkeypatch.setattr(settings, "chili_code_premium_fallback_enabled", False)
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", False)

    assert llm_gateway._local_only_code_routing("code_dispatch_edit", None) is True
    assert llm_gateway._local_only_code_routing("code_review", None) is True
    assert llm_gateway._local_only_code_routing("chat_user", None) is False
    assert llm_gateway._local_only_code_routing("code_dispatch_edit", False) is False

    monkeypatch.setattr(settings, "chili_code_premium_fallback_enabled", True)
    assert llm_gateway._local_only_code_routing("code_dispatch_edit", None) is False


def test_resolution_order_json_beats_local_beats_frontier(local_configured):
    """Mirrors the gateway expression:
    _purpose_model_override(...) or _local_code_override(...) or _frontier_code_override(...)."""
    monkeypatch = local_configured
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)
    monkeypatch.setattr(settings, "frontier_api_key", "k")
    monkeypatch.setattr(settings, "frontier_base_url", "https://api.anthropic.com/v1")
    monkeypatch.setattr(settings, "frontier_model", FRONTIER_MODEL)

    policy = _policy("code_dispatch_edit")

    def resolve():
        return (
            llm_gateway._purpose_model_override(policy.purpose, policy)
            or llm_gateway._local_code_override(policy)
            or llm_gateway._frontier_code_override(policy)
        )

    # Local beats frontier when both are on.
    monkeypatch.setattr(settings, "chili_llm_purpose_model_overrides_json", "{}")
    assert resolve() == LOCAL_MODEL

    # Frontier fires only when local-first is off.
    monkeypatch.setattr(settings, "chili_code_local_first", False)
    assert resolve() == FRONTIER_MODEL

    # Explicit JSON override beats everything.
    monkeypatch.setattr(settings, "chili_code_local_first", True)
    monkeypatch.setattr(
        settings, "chili_llm_purpose_model_overrides_json",
        '{"code_dispatch_edit": "operator-pinned"}',
    )
    assert resolve() == "operator-pinned"
