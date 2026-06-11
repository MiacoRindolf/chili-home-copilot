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
FRONTIER_MODEL = "claude-opus-4-8"


@pytest.fixture
def local_configured(monkeypatch):
    monkeypatch.setattr(settings, "ollama_host", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "chili_code_local_model", LOCAL_MODEL)
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
