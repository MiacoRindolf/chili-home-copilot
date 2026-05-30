from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import openai_client as oc
from app.config import Settings
from app.services.context_brain import llm_gateway as gw
from app.services.context_brain import purpose_policy as policy_mod
from app.services.context_brain.llm_gateway import gateway_chat, gateway_chat_stream
from app.services.context_brain.tree_types import PurposePolicy
from app.services.llm_cost import estimate_cost_usd, provider_from_base_url


@pytest.fixture(autouse=True)
def reset_gateway_exact_cache():
    gw.reset_gateway_cache()
    yield
    gw.reset_gateway_cache()


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


def test_model_override_uses_cheaper_paid_model(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(oc.settings, "llm_free_tier_first", False)

    def fake_call(api_key, base_url, model, *args, **kwargs):
        calls.append(model)
        return {
            "reply": "This is a strong enough reply for the override path.",
            "tokens_used": 10,
            "model": model,
        }

    monkeypatch.setattr(oc, "_openai_official_configured", lambda: True)
    monkeypatch.setattr(oc, "_groq_stack_configured", lambda: False)
    monkeypatch.setattr(oc, "_premium_configured", lambda: False)
    monkeypatch.setattr(oc, "_near_daily_limit", lambda *args, **kwargs: False)
    monkeypatch.setattr(oc, "_near_paid_budget_limit", lambda *args, **kwargs: False)
    monkeypatch.setattr(oc, "_safe_log_llm_call", lambda **kwargs: None)
    monkeypatch.setattr(oc, "_call_provider", fake_call)

    result = oc.chat(
        messages=[{"role": "user", "content": "rank this"}],
        user_message="rank this",
        model_override="gpt-5.4-mini",
    )

    assert result["model"] == "gpt-5.4-mini"
    assert calls == ["gpt-5.4-mini"]
    assert oc.provider_base_url_for_model("gpt-5.4-mini") == oc.PAID_OPENAI_BASE_URL


def test_gateway_purpose_override_for_non_high_stakes(monkeypatch):
    chat = MagicMock(
        return_value={
            "reply": "ok",
            "model": "gpt-5.4-mini",
            "provider": "openai",
            "tokens_used": 10,
        }
    )
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.settings.chili_llm_purpose_model_overrides_json",
        '{"pattern_research_extract":"gpt-5.4-mini"}',
    )
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        lambda db, purpose: PurposePolicy(
            purpose=purpose,
            routing_strategy="passthrough",
            decompose=False,
            cross_examine=False,
            use_premium_synthesis=False,
            high_stakes=False,
        ),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._write_gateway_log_start", lambda *a, **k: 1)
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "chat", chat)

    result = gateway_chat(
        [{"role": "user", "content": "extract"}],
        purpose="pattern_research_extract",
        db=object(),
    )

    assert result["reply"] == "ok"
    assert chat.call_args.kwargs["model_override"] == "gpt-5.4-mini"


def test_gateway_purpose_override_ignored_for_high_stakes(monkeypatch):
    chat = MagicMock(return_value={"reply": "ok", "model": "gpt-5.5", "tokens_used": 10})
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.settings.chili_llm_purpose_model_overrides_json",
        '{"autotrader_revalidation":"gpt-5.4-mini"}',
    )
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        lambda db, purpose: PurposePolicy(
            purpose=purpose,
            routing_strategy="passthrough",
            decompose=False,
            cross_examine=False,
            use_premium_synthesis=False,
            high_stakes=True,
        ),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._write_gateway_log_start", lambda *a, **k: 1)
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", lambda *a, **k: None)
    monkeypatch.setattr(oc, "chat", chat)

    gateway_chat(
        [{"role": "user", "content": "validate"}],
        purpose="autotrader_revalidation",
        db=object(),
    )

    assert chat.call_args.kwargs["model_override"] is None


def test_gateway_exact_cache_replays_offline_passthrough_without_paid_call(monkeypatch):
    chat = MagicMock(
        return_value={
            "reply": "stable reflection",
            "model": "gpt-5.5",
            "provider": "openai",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "tokens_used": 120,
            "estimated_cost_usd": 0.0011,
        }
    )
    finalize = MagicMock()
    log_ids = iter([101, 102])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        lambda db, purpose: PurposePolicy(
            purpose=purpose,
            routing_strategy="passthrough",
            decompose=False,
            cross_examine=False,
            use_premium_synthesis=False,
            high_stakes=False,
        ),
    )
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "reflect on the close"}]
    first = gateway_chat(messages, purpose="trading_reflect", system_prompt="sys", db=object())
    second = gateway_chat(messages, purpose="trading_reflect", system_prompt="sys", db=object())

    assert first["reply"] == "stable reflection"
    assert second["reply"] == "stable reflection"
    assert chat.call_count == 1
    assert first["gateway_log_id"] == 101
    assert second["gateway_log_id"] == 102
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    cache_kwargs = finalize.call_args_list[1].kwargs
    assert cache_kwargs["cache_status"] == "gateway_cache_hit"
    assert cache_kwargs["provider"] == "cache"
    assert cache_kwargs["premium_calls"] == 0
    assert cache_kwargs["premium_tokens"] == 0
    assert cache_kwargs["estimated_cost_usd"] == 0.0


def test_gateway_exact_cache_never_caches_high_stakes_passthrough(monkeypatch):
    chat = MagicMock(
        side_effect=[
            {"reply": "allow only with evidence", "model": "gpt-5.5", "tokens_used": 12},
            {"reply": "allow only with evidence", "model": "gpt-5.5", "tokens_used": 12},
        ]
    )
    finalize = MagicMock()

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        lambda db, purpose: PurposePolicy(
            purpose="autotrader_revalidation",
            routing_strategy="passthrough",
            decompose=False,
            cross_examine=False,
            use_premium_synthesis=True,
            high_stakes=True,
        ),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._write_gateway_log_start", lambda *a, **k: 201)
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "validate live order"}]
    gateway_chat(messages, purpose="autotrader_revalidation", db=object())
    gateway_chat(messages, purpose="autotrader_revalidation", db=object())

    assert chat.call_count == 2
    assert all(call.kwargs.get("cache_status") is None for call in finalize.call_args_list)


def test_gateway_exact_cache_respects_shared_cache_disable(monkeypatch):
    chat = MagicMock(
        side_effect=[
            {"reply": "fresh deterministic reply", "model": "gpt-5.5", "tokens_used": 12},
            {"reply": "fresh deterministic reply", "model": "gpt-5.5", "tokens_used": 12},
        ]
    )
    finalize = MagicMock()

    monkeypatch.setattr(gw.settings, "llm_cache_max_entries", 0)
    monkeypatch.setattr(gw.settings, "llm_cache_ttl_seconds", 600)
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        lambda db, purpose: PurposePolicy(
            purpose="trading_reflect",
            routing_strategy="passthrough",
            decompose=False,
            cross_examine=False,
            use_premium_synthesis=False,
            high_stakes=False,
        ),
    )
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        MagicMock(side_effect=[301, 302]),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "same deterministic reflection"}]
    gateway_chat(messages, purpose="trading_reflect", system_prompt="sys", db=object())
    gateway_chat(messages, purpose="trading_reflect", system_prompt="sys", db=object())

    assert chat.call_count == 2
    assert all(call.kwargs.get("cache_status") is None for call in finalize.call_args_list)


@pytest.mark.parametrize(
    "purpose",
    [
        "reasoning_anticipate",
        "reasoning_evolve",
        "reasoning_proactive",
        "reasoning_user_model",
        "reasoning_web_research",
    ],
)
def test_reasoning_background_purpose_has_offline_code_default_when_db_seed_missing(purpose):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return EmptyResult()

    db = EmptyDb()

    policy = policy_mod.get_policy(db, purpose)

    assert policy.purpose == purpose
    assert policy.routing_strategy == "passthrough"
    assert policy.use_premium_synthesis is False
    assert policy.high_stakes is False
    assert db.calls == 1


@pytest.mark.parametrize(
    "purpose",
    [
        "chat_search",
        "code_review",
        "code_search",
        "desktop_normalize_app",
        "desktop_refine_speech",
        "memory_extract",
        "personality_apply",
        "planner_intent",
    ],
)
def test_code_purpose_has_offline_code_default_when_db_seed_missing(purpose):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return EmptyResult()

    db = EmptyDb()

    policy = policy_mod.get_policy(db, purpose)

    assert policy.purpose == purpose
    assert policy.routing_strategy == "passthrough"
    assert policy.use_premium_synthesis is False
    assert policy.high_stakes is False
    assert db.calls == 1


def test_project_playwright_has_offline_code_default_when_db_seed_missing():
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return EmptyResult()

    db = EmptyDb()

    policy = policy_mod.get_policy(db, "project_playwright")

    assert policy.purpose == "project_playwright"
    assert policy.routing_strategy == "passthrough"
    assert policy.use_premium_synthesis is False
    assert policy.high_stakes is False
    assert db.calls == 1


def test_wellness_chat_has_high_stakes_passthrough_default_when_db_seed_missing():
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return EmptyResult()

    db = EmptyDb()

    policy = policy_mod.get_policy(db, "wellness_chat")

    assert policy.purpose == "wellness_chat"
    assert policy.routing_strategy == "passthrough"
    assert policy.use_premium_synthesis is True
    assert policy.high_stakes is True
    assert db.calls == 1


@pytest.mark.parametrize(
    ("purpose", "use_premium_synthesis", "high_stakes"),
    [
        ("trading_reasoning", False, False),
        ("trading_pattern_mine", False, False),
        ("trading_reflect", False, False),
        ("pattern_research_extract", False, False),
        ("trading_analyze", True, False),
        ("trading_analyze_stream", True, False),
        ("smart_pick_stream", True, False),
        ("trading_smart_pick", True, False),
        ("trading_brain_assistant", True, False),
        ("autotrader_revalidation", False, True),
        ("pattern_adjustment", False, True),
        ("trade_plan_extract", False, True),
        ("position_plan_generator", False, True),
        ("pattern_suggest", False, True),
    ],
)
def test_trading_purpose_has_seed_equivalent_default_when_db_seed_missing(
    purpose,
    use_premium_synthesis,
    high_stakes,
):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return EmptyResult()

    db = EmptyDb()

    policy = policy_mod.get_policy(db, purpose)

    assert policy.purpose == purpose
    assert policy.routing_strategy == "passthrough"
    assert policy.use_premium_synthesis is use_premium_synthesis
    assert policy.high_stakes is high_stakes
    assert db.calls == 1


@pytest.mark.parametrize(
    "purpose",
    [
        "project_ai_engineer",
        "project_architect",
        "project_backend_engineer",
        "project_devops_engineer",
        "project_frontend_engineer",
        "project_product_owner",
        "project_project_manager",
        "project_qa_engineer",
        "project_security_engineer",
        "project_ux_designer",
    ],
)
def test_project_agent_purpose_has_offline_code_default_when_db_seed_missing(purpose):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return EmptyResult()

    db = EmptyDb()

    policy = policy_mod.get_policy(db, purpose)

    assert policy.purpose == purpose
    assert policy.routing_strategy == "passthrough"
    assert policy.use_premium_synthesis is False
    assert policy.high_stakes is False
    assert db.calls == 1


def test_gateway_exact_cache_uses_web_research_code_default(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '{"summary":"ok","sources":[],"relevance_score":0.8}',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 50,
        }
    )
    finalize = MagicMock()
    log_ids = iter([301, 302])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "summarize this search payload"}]
    gateway_chat(messages, purpose="reasoning_web_research", system_prompt="json only", db=EmptyDb())
    gateway_chat(messages, purpose="reasoning_web_research", system_prompt="json only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


@pytest.mark.parametrize("purpose", ["reasoning_anticipate", "reasoning_proactive"])
def test_gateway_exact_cache_uses_reasoning_background_code_default(monkeypatch, purpose):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '[{"description":"prep","domain":"general","confidence":0.7}]',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 55,
        }
    )
    finalize = MagicMock()
    log_ids = iter([501, 502])

    monkeypatch.setattr("app.services.context_brain.llm_gateway._write_gateway_log_start", lambda *a, **k: next(log_ids))
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "anticipate from stable model"}]
    gateway_chat(messages, purpose=purpose, system_prompt="json only", db=EmptyDb())
    gateway_chat(messages, purpose=purpose, system_prompt="json only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_exact_cache_uses_project_agent_code_default(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '{"issues_found":0}',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 40,
        }
    )
    finalize = MagicMock()
    log_start = MagicMock(side_effect=[401, 402])

    monkeypatch.setattr("app.services.context_brain.llm_gateway._write_gateway_log_start", log_start)
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "review backend findings"}]
    gateway_chat(messages, purpose="project_backend_engineer", db=EmptyDb())
    gateway_chat(messages, purpose="project_backend_engineer", db=EmptyDb())

    assert chat.call_count == 1
    assert log_start.call_args_list[0].kwargs["purpose"] == "project_backend_engineer"
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


@pytest.mark.parametrize("purpose", ["desktop_normalize_app", "desktop_refine_speech"])
def test_gateway_exact_cache_uses_desktop_defaults_when_db_seed_missing(monkeypatch, purpose):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": "visual studio code",
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 20,
        }
    )
    finalize = MagicMock()
    log_ids = iter([801, 802])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "User said: visual studio code"}]
    gateway_chat(messages, purpose=purpose, system_prompt="normalize", db=EmptyDb())
    gateway_chat(messages, purpose=purpose, system_prompt="normalize", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_exact_cache_uses_memory_extract_default_when_db_seed_missing(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '[{"category":"interest","content":"Enjoys hiking"}]',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 35,
        }
    )
    finalize = MagicMock()
    log_ids = iter([901, 902])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "USER: I enjoy hiking\nASSISTANT: Nice"}]
    gateway_chat(messages, purpose="memory_extract", system_prompt="json arrays only", db=EmptyDb())
    gateway_chat(messages, purpose="memory_extract", system_prompt="json arrays only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_exact_cache_uses_personality_apply_default_when_db_seed_missing(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '{"interests":["hiking"],"dietary":"","tone":"","notes":""}',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 35,
        }
    )
    finalize = MagicMock()
    log_ids = iter([951, 952])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "Facts:\n- [interest] Enjoys hiking"}]
    gateway_chat(messages, purpose="personality_apply", system_prompt="json only", db=EmptyDb())
    gateway_chat(messages, purpose="personality_apply", system_prompt="json only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_exact_cache_uses_planner_intent_default_when_db_seed_missing(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '[{"title":"Define scope","description":"Complexity: Low","estimated_days":0.25}]',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 35,
        }
    )
    finalize = MagicMock()
    log_ids = iter([961, 962])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": 'For a project called "Research obscure idea", suggest tasks.'}]
    gateway_chat(messages, purpose="planner_intent", system_prompt="json array only", db=EmptyDb())
    gateway_chat(messages, purpose="planner_intent", system_prompt="json array only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_exact_cache_uses_chat_search_default_when_db_seed_missing(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '{"answer":"stable","sources":[{"title":"A","url":"https://example.com"}]}',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 45,
        }
    )
    finalize = MagicMock()
    log_ids = iter([971, 972])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "Use these search results to answer: stable query"}]
    gateway_chat(messages, purpose="chat_search", system_prompt="search synthesis json only", db=EmptyDb())
    gateway_chat(messages, purpose="chat_search", system_prompt="search synthesis json only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_exact_cache_uses_trading_reflect_default_when_db_seed_missing(monkeypatch):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '{"reflection":"stable"}',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 42,
        }
    )
    finalize = MagicMock()
    log_ids = iter([601, 602])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "reflect from a stable market snapshot"}]
    gateway_chat(messages, purpose="trading_reflect", system_prompt="json only", db=EmptyDb())
    gateway_chat(messages, purpose="trading_reflect", system_prompt="json only", db=EmptyDb())

    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


@pytest.mark.parametrize(
    "purpose",
    ["trading_analyze", "trading_reasoning", "trading_smart_pick", "trading_brain_assistant"],
)
def test_gateway_exact_cache_uses_low_stakes_trading_defaults_when_db_seed_missing(
    monkeypatch,
    purpose,
):
    class EmptyResult:
        def fetchone(self):
            return None

    class EmptyDb:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    chat = MagicMock(
        return_value={
            "reply": '{"verdict":"stable"}',
            "model": "gpt-5.5",
            "provider": "openai",
            "tokens_used": 84,
        }
    )
    finalize = MagicMock()
    log_ids = iter([701, 702])

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: next(log_ids),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", chat)

    messages = [{"role": "user", "content": "analyze the same stable market context"}]
    first = gateway_chat(
        messages,
        purpose=purpose,
        system_prompt="stable context snapshot",
        user_message="same request",
        user_id=99,
        db=EmptyDb(),
    )
    second = gateway_chat(
        messages,
        purpose=purpose,
        system_prompt="stable context snapshot",
        user_message="same request",
        user_id=99,
        db=EmptyDb(),
    )

    assert first["reply"] == second["reply"] == '{"verdict":"stable"}'
    assert chat.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


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
    assert kwargs["cache_status"] == "gateway_stream_cache_miss"


def test_gateway_stream_replays_exact_cache_for_low_stakes_stream(monkeypatch):
    finalize = MagicMock()
    stream = MagicMock(return_value=iter([("cached ", "gpt-5.5"), ("answer", "gpt-5.5")]))

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
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        MagicMock(side_effect=[501, 502]),
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat_stream", stream)
    monkeypatch.setattr(oc, "provider_base_url_for_model", lambda model: "https://api.openai.com/v1")
    monkeypatch.setattr(oc, "_safe_log_llm_call", lambda **kwargs: None)

    kwargs = {
        "messages": [{"role": "user", "content": "same stream prompt"}],
        "purpose": "smart_pick_stream",
        "system_prompt": "static smart-pick policy",
        "trace_id": "stream-cache-test",
        "user_message": "same request",
        "user_id": 42,
        "db": object(),
    }
    first = list(gateway_chat_stream(**kwargs))
    second = list(gateway_chat_stream(**kwargs))

    assert "".join(tok for tok, _ in first) == "cached answer"
    assert "".join(tok for tok, _ in second) == "cached answer"
    assert stream.call_count == 1
    assert finalize.call_args_list[0].kwargs["cache_status"] == "gateway_stream_cache_miss"
    assert finalize.call_args_list[1].kwargs["cache_status"] == "gateway_stream_cache_hit"
    assert finalize.call_args_list[1].kwargs["provider"] == "cache"
    assert finalize.call_args_list[1].kwargs["premium_calls"] == 0


def test_gateway_dispatch_error_skips_direct_paid_fallback(monkeypatch):
    direct = MagicMock(side_effect=RuntimeError("provider down"))
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
    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._write_gateway_log_start",
        lambda *a, **k: 9901,
    )
    monkeypatch.setattr("app.services.context_brain.llm_gateway._finalize_gateway_log", finalize)
    monkeypatch.setattr(oc, "chat", direct)

    result = gateway_chat(
        [{"role": "user", "content": "do not bypass budget"}],
        purpose="trading_reflect",
        system_prompt="json only",
        db=object(),
    )

    assert result == {
        "reply": "",
        "tokens_used": 0,
        "model": "gateway_error",
        "gateway_log_id": None,
    }
    assert direct.call_count == 1
    assert finalize.call_args.kwargs["error_kind"] == "exception"


def test_gateway_missing_db_skips_direct_paid_fallback(monkeypatch):
    direct = MagicMock(side_effect=AssertionError("direct OpenAI bypassed gateway"))

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway._open_db_session",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(oc, "chat", direct)

    result = gateway_chat(
        [{"role": "user", "content": "db unavailable"}],
        purpose="memory_extract",
        system_prompt="json only",
    )

    assert result["model"] == "gateway_error"
    assert result["tokens_used"] == 0
    direct.assert_not_called()


def test_gateway_stream_policy_error_skips_direct_paid_fallback(monkeypatch):
    direct_stream = MagicMock(side_effect=AssertionError("direct OpenAI stream bypassed gateway"))

    monkeypatch.setattr(
        "app.services.context_brain.llm_gateway.policy_mod.get_policy",
        MagicMock(side_effect=RuntimeError("policy unavailable")),
    )
    monkeypatch.setattr(oc, "chat_stream", direct_stream)

    out = list(
        gateway_chat_stream(
            [{"role": "user", "content": "stream without policy"}],
            purpose="smart_pick_stream",
            system_prompt="static stream policy",
            db=object(),
        )
    )

    assert out == []
    direct_stream.assert_not_called()


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
