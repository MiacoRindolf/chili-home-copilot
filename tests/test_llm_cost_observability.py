from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app import openai_client as oc
from app.config import Settings
from app.services.context_brain.llm_gateway import gateway_chat_stream
from app.services.context_brain.tree_types import PurposePolicy
from app.services.llm_cost import estimate_cost_usd, provider_from_base_url


def test_provider_from_base_url_labels_openai_not_config_slot():
    assert provider_from_base_url("https://api.openai.com/v1") == "openai"
    assert provider_from_base_url("https://api.groq.com/openai/v1") == "groq"
    assert provider_from_base_url("https://generativelanguage.googleapis.com/v1beta/openai/") == "gemini"


def test_estimate_cost_uses_cached_input_discount():
    cost = estimate_cost_usd(
        provider="openai",
        model="gpt-5.5",
        prompt_tokens=1_000,
        cached_tokens=400,
        completion_tokens=100,
    )
    assert cost == 0.0062


def test_paid_openai_legacy_aliases_populate_canonical_fields():
    settings = Settings(
        _env_file=None,
        database_url="postgresql://chili:chili@localhost:5433/chili",
        PAID_OPENAI_API_KEY="sk-paid",
        PAID_OPENAI_MODEL="gpt-5.5",
        PAID_OPENAI_BASE_URL="https://api.openai.com/v1",
    )
    assert settings.openai_api_key == "sk-paid"
    assert settings.openai_model == "gpt-5.5"
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.primary_api_key == "sk-paid"


def test_paid_budget_shadow_observes_but_does_not_block(monkeypatch):
    monkeypatch.setattr(oc.settings, "chili_llm_premium_daily_budget_usd", 1.0)
    monkeypatch.setattr(oc.settings, "chili_llm_cost_mode", "shadow")
    monkeypatch.setattr(oc, "_provider_spend_today_usd", lambda provider="openai": 1.5)

    assert oc._near_paid_budget_limit("https://api.openai.com/v1", "test") is False


def test_paid_budget_enforce_blocks_openai(monkeypatch):
    monkeypatch.setattr(oc.settings, "chili_llm_premium_daily_budget_usd", 1.0)
    monkeypatch.setattr(oc.settings, "chili_llm_cost_mode", "enforce")
    monkeypatch.setattr(oc, "_provider_spend_today_usd", lambda provider="openai": 1.5)

    assert oc._near_paid_budget_limit("https://api.openai.com/v1", "test") is True


def test_gateway_stream_logs_provider_and_estimated_cost(monkeypatch):
    finalize = MagicMock()
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        lambda db, purpose: PurposePolicy(
            purpose=purpose,
            routing_strategy="passthrough",
            decompose=False,
            cross_examine=False,
            use_premium_synthesis=True,
            high_stakes=False,
        ),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._write_gateway_log_start", lambda *a, **k: 123)
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(
        oc,
        "chat_stream",
        lambda **kwargs: iter([("hello ", "gpt-5.5"), ("world", "gpt-5.5")]),
    )
    monkeypatch.setattr(oc, "provider_base_url_for_model", lambda model: "https://api.openai.com/v1")
    monkeypatch.setattr(oc, "_safe_log_llm_call", lambda **kwargs: None)

    out = list(
        gateway_chat_stream(
            [{"role": "user", "content": "hi"}],
            purpose="trading_analyze_stream",
            system_prompt="sys",
            trace_id="stream-test",
            db=object(),
        )
    )

    assert "".join(tok for tok, _ in out) == "hello world"
    kwargs = finalize.call_args.kwargs
    assert kwargs["provider"] == "openai"
    assert kwargs["provider_base_url"] == "https://api.openai.com/v1"
    assert kwargs["estimated_cost_usd"] > 0
    assert kwargs["cache_status"] == "stream"


def test_no_direct_paid_openai_calls_outside_gateway_voice_vision():
    allowed = {
        Path("app/openai_client.py"),
        Path("app/vision.py"),
        Path("app/services/voice_service.py"),
    }
    offenders: list[str] = []
    for path in Path("app").rglob("*.py"):
        rel = Path(path.as_posix())
        text = path.read_text(encoding="utf-8", errors="ignore")
        if ("OpenAI(" in text or "openai.OpenAI(" in text) and rel not in allowed:
            offenders.append(path.as_posix())

    assert offenders == []
