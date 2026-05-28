from __future__ import annotations

from app.services.context_brain import synthesizer, tree_coordinator
from app.services.context_brain.tree_types import ChunkPlan, ChunkResponse, DecompositionPlan, PurposePolicy


def test_synthesizer_honors_policy_model_override(monkeypatch):
    captured: dict = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "reply": "final answer",
            "tokens_used": 42,
            "model": kwargs["model_override"],
            "estimated_cost_usd": 0.001,
        }

    monkeypatch.setattr(synthesizer.openai_client, "chat", fake_chat)

    result, latency_ms = synthesizer.synthesize(
        user_query="What changed?",
        compiled_context="A concise compiled context.",
        user_name="Ada",
        chat_history=[{"role": "user", "content": "Earlier question"}],
        model="gpt-5.4-mini",
        trace_id="test_context_synth",
    )

    assert result["model"] == "gpt-5.4-mini"
    assert captured["model_override"] == "gpt-5.4-mini"
    assert captured["trace_id"] == "test_context_synth"
    assert "<compiled_context>" in captured["system_prompt"]
    assert latency_ms >= 0


def test_tree_synthesis_cost_flows_into_gateway_telemetry(monkeypatch):
    synth_call: dict = {}
    policy = PurposePolicy(
        purpose="test_tree",
        routing_strategy="tree",
        decompose=True,
        cross_examine=False,
        use_premium_synthesis=True,
        high_stakes=False,
        synthesizer_model="gpt-5.4-mini",
    )

    monkeypatch.setattr(
        tree_coordinator.decomposer_mod,
        "decompose",
        lambda *args, **kwargs: DecompositionPlan(
            chunks=[ChunkPlan(index=1, query="Summarize the signal.")],
            strategy="heuristic_passthrough",
        ),
    )
    monkeypatch.setattr(
        tree_coordinator.chunk_exec_mod,
        "execute_chunks",
        lambda *args, **kwargs: [
            ChunkResponse(
                plan=ChunkPlan(index=1, query="Summarize the signal."),
                selected_response="resolved chunk",
                primary_tokens_out=7,
                success=True,
            )
        ],
    )
    monkeypatch.setattr(
        tree_coordinator.compiler_mod,
        "compile_chunks",
        lambda *args, **kwargs: ("compiled answer context", 3),
    )
    def fake_synthesize(*args, **kwargs):
        synth_call.update(kwargs)
        return (
            {
                "reply": "polished answer",
                "tokens_used": 123,
                "model": "gpt-5.4-mini",
                "estimated_cost_usd": 0.0042,
            },
            9,
        )

    monkeypatch.setattr(tree_coordinator.synthesizer_mod, "synthesize", fake_synthesize)
    monkeypatch.setattr(tree_coordinator, "_persist_tree_to_db", lambda *args, **kwargs: None)

    outcome = tree_coordinator.run_tree(
        "What should I know?",
        db=object(),
        policy=policy,
        trace_id="test_tree",
    )

    assert outcome.success is True
    assert outcome.synthesizer_model == "gpt-5.4-mini"
    assert synth_call["model"] == "gpt-5.4-mini"
    assert outcome.final_text == "polished answer"
    assert outcome.premium_total_tokens == 123
    assert outcome.premium_cost_usd == 0.0042
    assert outcome.synthesize_latency_ms == 9
