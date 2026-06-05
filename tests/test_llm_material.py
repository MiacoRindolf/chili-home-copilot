"""Unit tests for the LLM material-fingerprint foundation (pure, no DB).

Re-implemented on main from os-deploy unique work
(backup/os-deploy-cleanup-20260605). The contract that matters for replay-safe
caching: volatile metadata (cache_status, created_at, cost_usd, ...) must NOT
change the fingerprint, while schema and real payload content must.
"""
from __future__ import annotations

from app.services.llm_material import (
    is_volatile_material_key,
    stable_material_fingerprint,
    stable_material_text,
    stable_material_value,
)


def test_fingerprint_is_deterministic_sha256():
    a = stable_material_fingerprint(schema="v1", payload={"prompt": "hi", "n": 1})
    b = stable_material_fingerprint(schema="v1", payload={"prompt": "hi", "n": 1})
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_fingerprint_is_schema_sensitive():
    p = {"prompt": "hi"}
    assert stable_material_fingerprint(schema="v1", payload=p) != stable_material_fingerprint(
        schema="v2", payload=p
    )


def test_fingerprint_is_payload_sensitive():
    assert stable_material_fingerprint(schema="v1", payload={"prompt": "a"}) != stable_material_fingerprint(
        schema="v1", payload={"prompt": "b"}
    )


def test_fingerprint_ignores_volatile_metadata():
    base = {"prompt": "hello world", "model": "gpt", "n": 2}
    noisy = {
        **base,
        "cache_status": "hit",
        "created_at": "2026-06-05T01:02:03",
        "cost_usd": 0.012,
        "correlation_id": "abc-123",
        "agent_id": "agent-7",
    }
    assert stable_material_fingerprint(schema="v1", payload=base) == stable_material_fingerprint(
        schema="v1", payload=noisy
    )


def test_is_volatile_material_key():
    for k in ("cache_status", "created_at", "cost_usd", "correlation_id", "agent_id"):
        assert is_volatile_material_key(k) is True, k
    for k in ("prompt", "model", "messages", "system"):
        assert is_volatile_material_key(k) is False, k


def test_stable_material_value_strips_volatile_keys():
    assert stable_material_value({"keep": 1, "created_at": "t", "cache_status": "hit"}) == {
        "keep": 1
    }


def test_stable_material_value_recurses_into_nested_dicts():
    assert stable_material_value({"outer": {"keep": 2, "agent_id": "a"}}) == {
        "outer": {"keep": 2}
    }


def test_stable_material_value_passes_through_scalars():
    assert stable_material_value(5) == 5
    assert stable_material_value(None) is None


def test_stable_material_text_dict_is_sorted_compact_json():
    assert stable_material_text({"b": 1, "a": 2}) == '{"a":2,"b":1}'
