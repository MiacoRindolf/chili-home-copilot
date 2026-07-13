from __future__ import annotations

import urllib.error

from app.services.context_brain import ollama_client


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
        think=False,
        options={"num_predict": 180, "num_ctx": 2048, "keep_alive": "15m", "format": "json"},
    )

    assert result.ok is True
    assert 0 < captured["timeout"] <= 12
    assert captured["payload"]["keep_alive"] == "15m"
    assert captured["payload"]["format"] == "json"
    assert captured["payload"]["think"] is False
    assert captured["payload"]["options"] == {
        "temperature": 0.1,
        "num_predict": 180,
        "num_ctx": 2048,
    }


def test_chat_omits_thinking_control_when_unspecified(monkeypatch):
    captured = {}

    def fake_post_json(url, payload, timeout):
        captured["payload"] = payload
        return {"model": "qwen", "message": {"content": "ok"}, "eval_count": 1}

    monkeypatch.setattr(ollama_client, "_post_json", fake_post_json)

    result = ollama_client.chat(
        [{"role": "user", "content": "hello"}],
        "qwen",
        base_url="http://ollama:11434",
    )

    assert result.ok is True
    assert "think" not in captured["payload"]


def test_chat_reports_primary_timeout_in_aggregated_error(monkeypatch):
    monkeypatch.setattr(ollama_client, "_DEFAULT_OLLAMA_HOST", "http://ollama:11434")
    monkeypatch.setattr(ollama_client, "_LAST_WORKING_OLLAMA_HOSTS", {})
    monkeypatch.setattr(ollama_client, "_LAST_MODEL_LIST_HOST", None)
    attempts = []

    def fake_post_json(url, payload, timeout):
        attempts.append(url)
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
    assert "http://127.0.0.1:11434" not in (result.error or "")
    assert attempts == ["http://ollama:11434/api/chat"]


def test_chat_reuses_last_working_fallback_host(monkeypatch):
    monkeypatch.setattr(ollama_client, "_DEFAULT_OLLAMA_HOST", "http://ollama:11434")
    monkeypatch.setattr(ollama_client, "_LAST_WORKING_OLLAMA_HOSTS", {})
    monkeypatch.setattr(ollama_client, "_LAST_MODEL_LIST_HOST", None)
    attempts = []

    def fake_post_json(url, payload, timeout):
        attempts.append(url)
        if "ollama:11434" in url:
            raise urllib.error.URLError("unresolvable")
        return {"model": "qwen", "message": {"content": "ok"}, "eval_count": 1}

    monkeypatch.setattr(ollama_client, "_post_json", fake_post_json)

    first = ollama_client.chat([{"role": "user", "content": "one"}], "qwen")
    second = ollama_client.chat([{"role": "user", "content": "two"}], "qwen")

    assert first.ok is True
    assert second.ok is True
    assert attempts == [
        "http://ollama:11434/api/chat",
        "http://127.0.0.1:11434/api/chat",
        "http://127.0.0.1:11434/api/chat",
    ]


def test_chat_pins_working_hosts_per_model(monkeypatch):
    monkeypatch.setattr(ollama_client, "_DEFAULT_OLLAMA_HOST", "http://ollama:11434")
    monkeypatch.setattr(ollama_client, "_LAST_WORKING_OLLAMA_HOSTS", {})
    monkeypatch.setattr(ollama_client, "_LAST_MODEL_LIST_HOST", None)
    attempts = []

    def fake_post_json(url, payload, timeout):
        attempts.append((payload["model"], url))
        if "ollama:11434" in url:
            raise urllib.error.URLError("unresolvable")
        return {
            "model": payload["model"],
            "message": {"content": "ok"},
            "eval_count": 1,
        }

    monkeypatch.setattr(ollama_client, "_post_json", fake_post_json)

    assert ollama_client.chat([{"role": "user", "content": "one"}], "qwen").ok
    assert ollama_client.chat([{"role": "user", "content": "two"}], "llama").ok
    assert ollama_client.chat([{"role": "user", "content": "three"}], "qwen").ok

    assert attempts == [
        ("qwen", "http://ollama:11434/api/chat"),
        ("qwen", "http://127.0.0.1:11434/api/chat"),
        ("llama", "http://ollama:11434/api/chat"),
        ("llama", "http://127.0.0.1:11434/api/chat"),
        ("qwen", "http://127.0.0.1:11434/api/chat"),
    ]
