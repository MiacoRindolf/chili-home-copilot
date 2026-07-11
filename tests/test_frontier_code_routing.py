"""Tests for the opt-in frontier code-generation tier.

These verify the *routing* contract without any network or DB:

  * frontier is tried first only when explicitly requested via model_override
  * any frontier failure falls back to the existing local cascade (safety)
  * default calls (no frontier model_override) are byte-identical to before
  * the gateway only auto-routes code-generation purposes, and explicit
    per-purpose JSON overrides win

The frontier tier reuses the generic OpenAI-compatible ``_call_provider``;
Anthropic exposes an OpenAI-compatible endpoint, so the same plumbing reaches
Claude Fable 5 with no new SDK.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import openai_client
from app.config import settings
from app.services.context_brain import llm_gateway


FRONTIER_MODEL = "claude-fable-5"


@pytest.fixture
def frontier_configured(monkeypatch):
    """Configure a frontier provider (no real key is used — the call layer is
    mocked in each test) and silence the DB log writer."""
    monkeypatch.setattr(settings, "frontier_api_key", "test-frontier-key")
    monkeypatch.setattr(settings, "frontier_base_url", "https://api.anthropic.com/v1")
    monkeypatch.setattr(settings, "frontier_model", FRONTIER_MODEL)
    monkeypatch.setattr(openai_client, "_safe_log_llm_call", lambda **_k: None)
    # Clear any prior cached auth-failure state for the frontier endpoint.
    monkeypatch.setattr(openai_client, "_is_auth_failed", lambda _b: False)
    return monkeypatch


# ── helpers ──────────────────────────────────────────────────────────────


def _reply(text: str, *, base_url: str, model: str) -> dict:
    return {
        "reply": text,
        "tokens_used": 7,
        "model": model,
        "provider": "anthropic" if "anthropic" in base_url else "other",
        "provider_base_url": base_url,
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
        "estimated_cost_usd": 0.0,
    }


# ── unit: configuration + model matching ─────────────────────────────────


def test_frontier_configured_requires_key_url_and_model(monkeypatch):
    monkeypatch.setattr(settings, "frontier_api_key", "")
    monkeypatch.setattr(settings, "frontier_base_url", "https://api.anthropic.com/v1")
    monkeypatch.setattr(settings, "frontier_model", FRONTIER_MODEL)
    assert openai_client._frontier_configured() is False

    monkeypatch.setattr(settings, "frontier_api_key", "k")
    assert openai_client._frontier_configured() is True

    monkeypatch.setattr(settings, "frontier_model", "")
    assert openai_client._frontier_configured() is False


def test_is_frontier_model_exact_match(monkeypatch):
    monkeypatch.setattr(settings, "frontier_model", FRONTIER_MODEL)
    assert openai_client._is_frontier_model(FRONTIER_MODEL) is True
    assert openai_client._is_frontier_model("  " + FRONTIER_MODEL + " ") is True
    assert openai_client._is_frontier_model("gpt-4o-mini") is False
    assert openai_client._is_frontier_model(None) is False
    assert openai_client._is_frontier_model("") is False


# ── cascade behavior ─────────────────────────────────────────────────────


def test_chat_uses_frontier_first_when_requested(frontier_configured):
    monkeypatch = frontier_configured
    calls: list[str] = []

    def fake_call_provider(api_key, base_url, model, messages, prompt, trace_id, **kw):
        calls.append(base_url)
        return _reply(f"FRONTIER:{model}", base_url=base_url, model=model)

    monkeypatch.setattr(openai_client, "_call_provider", fake_call_provider)
    # The local cascade tiers should never be needed; make them loud if reached.
    monkeypatch.setattr(openai_client, "_chat_groq", lambda *a, **k: pytest.fail("groq reached"))
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: pytest.fail("openai reached"))
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: pytest.fail("gemini reached"))

    result = openai_client.chat(
        [{"role": "user", "content": "refactor this"}],
        trace_id="code-agent-edit-foo",
        model_override=FRONTIER_MODEL,
    )

    assert result["reply"] == f"FRONTIER:{FRONTIER_MODEL}"
    assert calls == ["https://api.anthropic.com/v1"]


def test_chat_falls_back_to_cascade_when_frontier_fails(frontier_configured):
    """The critical safety property: a frontier failure must transparently
    fall back to the existing local cascade, never hard-fail the code path."""
    monkeypatch = frontier_configured

    # Frontier tier returns nothing (simulates auth/rate/error → None).
    monkeypatch.setattr(openai_client, "_chat_frontier", lambda *a, **k: None)
    # Local primary answers.
    monkeypatch.setattr(
        openai_client,
        "_chat_groq",
        lambda *a, **k: _reply("LOCAL-GROQ", base_url="https://api.groq.com/openai/v1", model="llama-3.3-70b-versatile"),
    )
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: None)

    result = openai_client.chat(
        [{"role": "user", "content": "refactor this"}],
        trace_id="code-agent-edit-foo",
        model_override=FRONTIER_MODEL,
    )

    assert result["reply"] == "LOCAL-GROQ"


def test_chat_default_path_never_touches_frontier(frontier_configured):
    """Even with a frontier provider configured, a normal call (no frontier
    model_override) must behave exactly like the existing cascade."""
    monkeypatch = frontier_configured
    monkeypatch.setattr(openai_client, "_chat_frontier", lambda *a, **k: pytest.fail("frontier reached on default path"))
    monkeypatch.setattr(
        openai_client,
        "_chat_groq",
        lambda *a, **k: _reply("LOCAL-GROQ", base_url="https://api.groq.com/openai/v1", model="llama-3.3-70b-versatile"),
    )
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: None)

    result = openai_client.chat(
        [{"role": "user", "content": "hello"}],
        trace_id="chat",
        model_override=None,
    )

    assert result["reply"] == "LOCAL-GROQ"


def test_chat_frontier_not_prepended_when_unconfigured(monkeypatch):
    """No frontier key → frontier model_override is ignored, cascade only."""
    monkeypatch.setattr(settings, "frontier_api_key", "")
    monkeypatch.setattr(settings, "frontier_model", FRONTIER_MODEL)
    monkeypatch.setattr(openai_client, "_safe_log_llm_call", lambda **_k: None)
    monkeypatch.setattr(openai_client, "_chat_frontier", lambda *a, **k: pytest.fail("frontier reached while unconfigured"))
    monkeypatch.setattr(
        openai_client,
        "_chat_groq",
        lambda *a, **k: _reply("LOCAL-GROQ", base_url="https://api.groq.com/openai/v1", model="llama-3.3-70b-versatile"),
    )
    monkeypatch.setattr(openai_client, "_chat_openai", lambda *a, **k: None)
    monkeypatch.setattr(openai_client, "_chat_gemini", lambda *a, **k: None)

    result = openai_client.chat(
        [{"role": "user", "content": "x"}],
        model_override=FRONTIER_MODEL,
    )
    assert result["reply"] == "LOCAL-GROQ"


# ── _chat_frontier in isolation ──────────────────────────────────────────


def test_chat_frontier_marks_auth_failure_and_returns_none(frontier_configured):
    monkeypatch = frontier_configured
    marked: list[str] = []

    class _AuthError(Exception):
        pass

    def boom(*a, **k):
        raise _AuthError("401 unauthorized")

    monkeypatch.setattr(openai_client, "_call_provider", boom)
    monkeypatch.setattr(openai_client, "_looks_like_auth_error", lambda _e: True)
    monkeypatch.setattr(openai_client, "_mark_auth_failed", lambda b, t, e: marked.append(b))

    out = openai_client._chat_frontier(
        "sys", [{"role": "user", "content": "x"}], "x", "trace", 100, True, FRONTIER_MODEL
    )
    assert out is None
    assert marked == ["https://api.anthropic.com/v1"]


# ── gateway routing gate ─────────────────────────────────────────────────


def _policy(purpose: str, high_stakes: bool = False):
    return SimpleNamespace(purpose=purpose, high_stakes=high_stakes)


def test_gateway_frontier_override_routes_code_purposes(frontier_configured):
    monkeypatch = frontier_configured
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)

    assert llm_gateway._frontier_code_override(_policy("code_dispatch_edit")) == FRONTIER_MODEL
    assert llm_gateway._frontier_code_override(_policy("code_dispatch_plan")) == FRONTIER_MODEL
    assert llm_gateway._frontier_code_override(_policy("code_dispatch_pr_repair")) == FRONTIER_MODEL
    assert llm_gateway._frontier_code_override(_policy("code_review")) == FRONTIER_MODEL


def test_gateway_frontier_override_skips_non_code(frontier_configured):
    monkeypatch = frontier_configured
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)

    # Non-code purpose.
    assert llm_gateway._frontier_code_override(_policy("chat")) is None
    # Retrieval-only code_search is intentionally excluded.
    assert llm_gateway._frontier_code_override(_policy("code_search")) is None
    # None policy.
    assert llm_gateway._frontier_code_override(None) is None


def test_gateway_frontier_override_allows_high_stakes_code_purposes(frontier_configured):
    """high_stakes exists to block quality DOWNGRADES; the frontier tier is an
    upgrade. The seeded code_dispatch_* policies are all high-stakes, so a
    high_stakes veto here made the frontier tier unreachable for exactly the
    purposes it was built for (the quadruple-dead frontier bug)."""
    monkeypatch = frontier_configured
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)

    assert llm_gateway._frontier_code_override(_policy("code_dispatch_edit", high_stakes=True)) == FRONTIER_MODEL
    assert llm_gateway._frontier_code_override(_policy("code_dispatch_plan", high_stakes=True)) == FRONTIER_MODEL
    assert llm_gateway._frontier_code_override(_policy("code_dispatch_create", high_stakes=True)) == FRONTIER_MODEL
    # Non-code stays excluded even when high-stakes.
    assert llm_gateway._frontier_code_override(_policy("chat_user", high_stakes=True)) is None


def test_gateway_frontier_override_off_by_default(frontier_configured):
    monkeypatch = frontier_configured
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", False)
    assert llm_gateway._frontier_code_override(_policy("code_dispatch_edit")) is None


def test_gateway_frontier_override_requires_configured_provider(monkeypatch):
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)
    monkeypatch.setattr(settings, "frontier_api_key", "")  # not configured
    monkeypatch.setattr(settings, "frontier_model", FRONTIER_MODEL)
    assert llm_gateway._frontier_code_override(_policy("code_dispatch_edit")) is None


def test_temperature_param_omitted_for_reasoning_families():
    """claude*/gpt-5*/o1*/o3* reject or constrain custom temperature; the
    param must be omitted so the provider default applies. Anything else
    keeps the historical 0.7."""
    assert openai_client._temperature_param("claude-fable-5") == {}
    assert openai_client._temperature_param("claude-fable-5") == {}
    assert openai_client._temperature_param("gpt-5.5") == {}
    assert openai_client._temperature_param("o3-mini") == {}
    assert openai_client._temperature_param("gpt-4o-mini") == {"temperature": 0.7}
    assert openai_client._temperature_param("llama-3.3-70b-versatile") == {"temperature": 0.7}


# ── streaming cascade ────────────────────────────────────────────────────


def test_chat_stream_uses_frontier_first_when_requested(frontier_configured):
    monkeypatch = frontier_configured
    seen_urls: list[str] = []

    def fake_stream_provider(api_key, base_url, model, messages, prompt, trace_id, max_tokens=1024):
        seen_urls.append(base_url)
        yield "tok1", model
        yield "tok2", model

    monkeypatch.setattr(openai_client, "_stream_provider", fake_stream_provider)
    monkeypatch.setattr(openai_client, "_stream_tier_groq", lambda *a, **k: pytest.fail("groq stream reached"))
    monkeypatch.setattr(openai_client, "_stream_tier_openai", lambda *a, **k: pytest.fail("openai stream reached"))
    monkeypatch.setattr(openai_client, "_stream_tier_gemini", lambda *a, **k: pytest.fail("gemini stream reached"))

    out = list(openai_client.chat_stream(
        [{"role": "user", "content": "refactor"}],
        trace_id="code-stream",
        model_override=FRONTIER_MODEL,
    ))

    assert [t for t, _m in out] == ["tok1", "tok2"]
    assert seen_urls == ["https://api.anthropic.com/v1"]


def test_chat_stream_frontier_failure_falls_back_silently(frontier_configured):
    """A frontier stream error must not set the permanent/429 flags — the
    standard tiers (and the non-streaming tail fallback) still run."""
    monkeypatch = frontier_configured

    def boom_stream(api_key, base_url, model, messages, prompt, trace_id, max_tokens=1024):
        raise RuntimeError("frontier stream down")
        yield  # pragma: no cover — make this a generator

    def groq_ok(messages, prompt, trace_id, max_tokens, flags):
        yield "local-tok", "llama-3.3-70b-versatile"
        flags["done"] = True

    monkeypatch.setattr(openai_client, "_stream_provider", boom_stream)
    monkeypatch.setattr(openai_client, "_stream_tier_groq", groq_ok)

    out = list(openai_client.chat_stream(
        [{"role": "user", "content": "refactor"}],
        trace_id="code-stream",
        model_override=FRONTIER_MODEL,
    ))
    assert [t for t, _m in out] == ["local-tok"]


def test_chat_stream_default_path_never_touches_frontier(frontier_configured):
    monkeypatch = frontier_configured

    def frontier_fail(*a, **k):
        pytest.fail("frontier stream reached on default path")
        yield  # pragma: no cover

    def groq_ok(messages, prompt, trace_id, max_tokens, flags):
        yield "local-tok", "llama-3.3-70b-versatile"
        flags["done"] = True

    monkeypatch.setattr(openai_client, "_stream_tier_frontier", frontier_fail)
    monkeypatch.setattr(openai_client, "_stream_tier_groq", groq_ok)

    out = list(openai_client.chat_stream(
        [{"role": "user", "content": "hello"}],
        trace_id="chat-stream",
        model_override=None,
    ))
    assert [t for t, _m in out] == ["local-tok"]


def test_explicit_json_override_wins_over_frontier(frontier_configured):
    """Operator's explicit per-purpose JSON override must take precedence over
    the frontier auto-route (this mirrors the gateway resolution order
    ``_purpose_model_override(...) or _frontier_code_override(...)``)."""
    monkeypatch = frontier_configured
    monkeypatch.setattr(settings, "chili_code_frontier_enabled", True)
    monkeypatch.setattr(
        settings,
        "chili_llm_purpose_model_overrides_json",
        '{"code_dispatch_edit": "operator-pinned-model"}',
    )
    policy = _policy("code_dispatch_edit")
    resolved = (
        llm_gateway._purpose_model_override(policy.purpose, policy)
        or llm_gateway._frontier_code_override(policy)
    )
    assert resolved == "operator-pinned-model"
