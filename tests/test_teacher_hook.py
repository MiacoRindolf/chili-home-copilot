"""Tests for the teacher-escalation chat hook (app/services/teacher_hook.py).

Pins the gating (flag off -> no-op; clean turn -> no fire; failed turn -> spawn),
the never-raises contract, and the strong-model adapter. No real LLM call, no
real background thread (both patched).
"""
import asyncio
from unittest.mock import patch

from app.config import settings as app_settings
from app.services import teacher_hook as th


_FAILED_TOOLS = [{"tool": "web_search", "output": "Unknown action 'x'", "error": "tool_execution_failed"}]
_CLEAN_TOOLS = [{"tool": "web_search", "output": "Found 3 results", "error": None}]


class TestMaybeFire:
    def test_noop_when_flag_off(self):
        with patch.object(th, "_spawn_escalation") as spawn, \
             patch.object(app_settings, "teacher_escalation_enabled", False, create=True):
            fired = th.maybe_fire_teacher_escalation("do x", _FAILED_TOOLS, "I don't have a tool")
        assert fired is False
        spawn.assert_not_called()

    def test_no_fire_on_clean_turn(self):
        with patch.object(th, "_spawn_escalation") as spawn, \
             patch.object(app_settings, "teacher_escalation_enabled", True, create=True):
            fired = th.maybe_fire_teacher_escalation("do x", _CLEAN_TOOLS, "Here you go!")
        assert fired is False
        spawn.assert_not_called()

    def test_fires_on_failed_turn(self):
        with patch.object(th, "_spawn_escalation") as spawn, \
             patch.object(app_settings, "teacher_escalation_enabled", True, create=True):
            fired = th.maybe_fire_teacher_escalation("do x", _FAILED_TOOLS, "I don't have a tool for that")
        assert fired is True
        spawn.assert_called_once()
        # The user request + reply are forwarded to the spawner.
        args = spawn.call_args[0]
        assert args[0] == "do x"

    def test_never_raises(self):
        # should_escalate blowing up must not propagate.
        with patch.object(th, "_spawn_escalation"), \
             patch.object(app_settings, "teacher_escalation_enabled", True, create=True), \
             patch("app.teacher_escalation.should_escalate", side_effect=Exception("boom")):
            fired = th.maybe_fire_teacher_escalation("do x", _FAILED_TOOLS, "fail")
        assert fired is False


class TestMaybeFireForTurn:
    """Scoping that prevents the hook from firing on ordinary chat."""

    def test_guest_never_fires(self):
        with patch.object(th, "maybe_fire_teacher_escalation") as inner:
            assert th.maybe_fire_for_turn("hi", "add_chore", "blocked", False, True) is False
        inner.assert_not_called()

    def test_conversational_turn_passes_no_error(self):
        # action_type 'unknown' + not executed must NOT be flagged as a tool error.
        with patch.object(th, "maybe_fire_teacher_escalation", return_value=False) as inner:
            th.maybe_fire_for_turn("how are you", "unknown", "I'm well!", False, False)
        tool_results = inner.call_args[0][1]
        assert tool_results[0]["error"] is None

    def test_real_tool_failure_flags_error(self):
        with patch.object(th, "maybe_fire_teacher_escalation", return_value=True) as inner:
            th.maybe_fire_for_turn("add chore X", "add_chore", "couldn't", False, False)
        tool_results = inner.call_args[0][1]
        assert tool_results[0]["error"] == "tool_execution_failed"

    def test_executed_tool_passes_no_error(self):
        with patch.object(th, "maybe_fire_teacher_escalation", return_value=False) as inner:
            th.maybe_fire_for_turn("add chore X", "add_chore", "done", True, False)
        tool_results = inner.call_args[0][1]
        assert tool_results[0]["error"] is None

    def test_conversational_turn_does_not_fire_end_to_end(self):
        # With the flag ON, a normal conversational turn must not spawn anything.
        from app.config import settings as app_settings
        with patch.object(th, "_spawn_escalation") as spawn, \
             patch.object(app_settings, "teacher_escalation_enabled", True, create=True):
            fired = th.maybe_fire_for_turn("how are you", "unknown", "I'm well, thanks!", False, False)
        assert fired is False
        spawn.assert_not_called()


class TestStrongCaller:
    def test_caller_returns_reply(self):
        async def run():
            caller = th._make_strong_llm_caller("t1")
            return await caller("teach me")
        with patch("app.services.context_brain.llm_gateway.gateway_chat",
                   return_value={"reply": "here is the skill", "model": "x"}) as gw:
            out = asyncio.run(run())
        assert out == "here is the skill"
        gw.assert_called_once()

    def test_caller_none_on_error(self):
        async def run():
            caller = th._make_strong_llm_caller("t1")
            return await caller("teach me")
        with patch("app.services.context_brain.llm_gateway.gateway_chat",
                   side_effect=Exception("gateway down")):
            out = asyncio.run(run())
        assert out is None

    def test_caller_none_on_empty_reply(self):
        async def run():
            return await th._make_strong_llm_caller("t1")("p")
        with patch("app.services.context_brain.llm_gateway.gateway_chat",
                   return_value={"reply": "", "model": "x"}):
            assert asyncio.run(run()) is None


class TestSpawnRunsEscalation:
    def test_spawn_invokes_escalate_and_learn(self):
        # Patch threading so the target runs synchronously, and stub the
        # escalation coroutine to assert it's driven with our args.
        captured = {}

        async def fake_escalate(user_request, tool_results, agent_reply, reason,
                                *, llm_caller, skill_saver=None):
            captured["args"] = (user_request, agent_reply, reason)
            captured["has_saver"] = skill_saver is not None
            return "skill-name"

        class _SyncThread:
            def __init__(self, target=None, **kw):
                self._t = target

            def start(self):
                self._t()

        with patch.object(th.threading, "Thread", _SyncThread), \
             patch("app.teacher_escalation.escalate_and_learn", side_effect=fake_escalate):
            th._spawn_escalation("do x", _FAILED_TOOLS, "reply", "reason", "t1")
        assert captured["args"] == ("do x", "reply", "reason")
        assert captured["has_saver"] is True  # combined file+index saver injected
