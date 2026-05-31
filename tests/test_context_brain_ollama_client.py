from __future__ import annotations

import json
import urllib.error
from collections import OrderedDict

from app.services.context_brain import ollama_client


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_chat_forwards_keep_alive_and_generation_options(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {
            "model": "qwen",
            "message": {"content": "ok"},
            "eval_count": 1,
        }

    monkeypatch.setattr(ollama_client, "_post_json", fake_post_json)

    result = ollama_client.chat(
        [{"role": "user", "content": "hello"}],
        "qwen",
        temperature=0.1,
        timeout_sec=12,
        base_url="http://ollama:11434",
        options={"num_predict": 180, "num_ctx": 2048, "keep_alive": "15m", "format": "json"},
    )

    assert result.ok is True
    assert captured["timeout"] == 12
    assert captured["payload"]["keep_alive"] == "15m"
    assert captured["payload"]["format"] == "json"
    assert captured["payload"]["options"] == {
        "temperature": 0.1,
        "num_predict": 180,
        "num_ctx": 2048,
    }


def test_chat_reports_primary_timeout_in_aggregated_error(monkeypatch):
    monkeypatch.setattr(ollama_client, "_DEFAULT_OLLAMA_HOST", "http://ollama:11434")

    def fake_post_json(url, payload, timeout):
        if "ollama:11434" in url:
            raise TimeoutError("timed out")
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ollama_client, "_post_json", fake_post_json)

    result = ollama_client.chat(
        [{"role": "user", "content": "hello"}],
        "qwen",
        timeout_sec=1,
    )

    assert result.ok is False
    assert "http://ollama:11434" in (result.error or "")
    assert "timed out" in (result.error or "")
    assert "http://127.0.0.1:11434" in (result.error or "")


def test_list_models_reuses_fresh_successful_probe(monkeypatch):
    calls = 0
    monkeypatch.setattr(ollama_client, "_model_list_cache", OrderedDict())

    def fake_urlopen(url, timeout):
        nonlocal calls
        calls += 1
        return _FakeResponse({"models": [{"name": "qwen"}, {"name": "llama"}]})

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake_urlopen)

    first = ollama_client.list_models(base_url="http://ollama:11434", timeout_sec=1)
    second = ollama_client.list_models(base_url="http://ollama:11434", timeout_sec=1)

    assert first == ["qwen", "llama"]
    assert second == ["qwen", "llama"]
    assert calls == 1


def test_list_models_does_not_cache_failures(monkeypatch):
    calls = 0
    monkeypatch.setattr(ollama_client, "_model_list_cache", OrderedDict())
    monkeypatch.setattr(ollama_client, "_FALLBACK_OLLAMA_HOSTS", ())

    def fake_urlopen(url, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake_urlopen)

    assert ollama_client.list_models(base_url="http://ollama:11434", timeout_sec=1) == []
    assert ollama_client.list_models(base_url="http://ollama:11434", timeout_sec=1) == []
    assert calls == 2


def test_list_models_cache_is_bounded_and_refreshes_recency(monkeypatch):
    calls = 0
    monkeypatch.setattr(ollama_client, "_model_list_cache", OrderedDict())
    monkeypatch.setattr(ollama_client, "_MODEL_LIST_CACHE_MAX", 2)

    def fake_urlopen(url, timeout):
        nonlocal calls
        calls += 1
        return _FakeResponse({"models": [{"name": url.rsplit("/", 2)[0]}]})

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake_urlopen)

    ollama_client.list_models(base_url="http://one:11434", timeout_sec=1)
    ollama_client.list_models(base_url="http://two:11434", timeout_sec=1)
    ollama_client.list_models(base_url="http://one:11434", timeout_sec=1)
    ollama_client.list_models(base_url="http://three:11434", timeout_sec=1)

    assert list(ollama_client._model_list_cache) == [
        ("http://one:11434", 1.0),
        ("http://three:11434", 1.0),
    ]
    assert calls == 3
