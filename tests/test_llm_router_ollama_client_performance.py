from __future__ import annotations

import requests

from app.services.llm_router import ollama_client


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_list_models_reuses_fresh_successful_probe(monkeypatch) -> None:
    calls = 0
    ollama_client.reset_model_list_cache_for_tests()

    def fake_get(url, timeout):
        nonlocal calls
        calls += 1
        return _FakeResponse({"models": [{"name": "qwen"}, {"name": "llama"}]})

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)

    assert ollama_client.list_models() == ["qwen", "llama"]
    assert ollama_client.list_models() == ["qwen", "llama"]
    assert calls == 1


def test_list_models_does_not_cache_failures(monkeypatch) -> None:
    calls = 0
    ollama_client.reset_model_list_cache_for_tests()

    def fake_get(url, timeout):
        nonlocal calls
        calls += 1
        raise requests.RequestException("offline")

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)

    assert ollama_client.list_models() == []
    assert ollama_client.list_models() == []
    assert calls == 2
