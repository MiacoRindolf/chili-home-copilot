import inspect

import pytest
from unittest.mock import MagicMock

from app.services import desktop_refinement


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("Chrome", "chrome"),
        ("the visual studio code", "visual studio code"),
        ("note pad", "notepad"),
        ("notepad plus plus", "notepad++"),
        ("power shell", "powershell"),
        ("task manager", "task manager"),
    ],
)
def test_normalize_app_name_uses_mechanical_alias_without_llm(monkeypatch, spoken, expected):
    def fail_if_llm_checked():
        raise AssertionError("known desktop app aliases should not reach LLM setup")

    monkeypatch.setattr("app.openai_client.is_configured", fail_if_llm_checked)

    assert desktop_refinement.normalize_app_name(spoken) == expected


def test_normalize_app_name_falls_back_when_no_alias_and_llm_unconfigured(monkeypatch):
    monkeypatch.setattr("app.openai_client.is_configured", lambda: False)

    assert desktop_refinement.normalize_app_name("mystery app") == "mystery app"


def test_refine_desktop_transcription_routes_through_gateway(monkeypatch):
    gateway = MagicMock(return_value={"reply": "open visual studio code", "model": "gpt-5.5"})
    direct = MagicMock(side_effect=AssertionError("direct OpenAI bypassed gateway"))

    monkeypatch.setattr(desktop_refinement.settings, "desktop_refinement_enabled", True)
    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.openai_client.chat", direct)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    out = desktop_refinement.refine_desktop_transcription(
        "open vizual studio code",
        trace_id="desktop-refine-test",
    )

    assert out == "open visual studio code"
    assert gateway.call_count == 1
    assert gateway.call_args.kwargs["purpose"] == "desktop_refine_speech"
    direct.assert_not_called()


def test_refine_desktop_transcription_gateway_failure_keeps_original(monkeypatch):
    gateway = MagicMock(side_effect=RuntimeError("gateway unavailable"))
    direct = MagicMock(side_effect=AssertionError("direct OpenAI bypassed gateway"))

    monkeypatch.setattr(desktop_refinement.settings, "desktop_refinement_enabled", True)
    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.openai_client.chat", direct)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    out = desktop_refinement.refine_desktop_transcription(
        "open vizual studio code",
        trace_id="desktop-refine-test",
    )

    assert out == "open vizual studio code"
    assert gateway.call_count == 1
    direct.assert_not_called()


def test_normalize_app_name_gateway_failure_keeps_original(monkeypatch):
    gateway = MagicMock(side_effect=RuntimeError("gateway unavailable"))
    direct = MagicMock(side_effect=AssertionError("direct OpenAI bypassed gateway"))

    monkeypatch.setattr(desktop_refinement.settings, "desktop_refinement_enabled", True)
    monkeypatch.setattr("app.openai_client.is_configured", lambda: True)
    monkeypatch.setattr("app.openai_client.chat", direct)
    monkeypatch.setattr("app.services.context_brain.llm_gateway.gateway_chat", gateway)

    out = desktop_refinement.normalize_app_name(
        "mystery app",
        trace_id="desktop-norm-test",
    )

    assert out == "mystery app"
    assert gateway.call_count == 1
    assert gateway.call_args.kwargs["purpose"] == "desktop_normalize_app"
    direct.assert_not_called()


def test_desktop_refinement_source_has_no_direct_openai_fallback():
    source = inspect.getsource(desktop_refinement)

    assert "openai_client.chat(" not in source
    assert "direct_openai_bypass_disabled" in source
