from __future__ import annotations

from datetime import datetime

from app.services.code_brain import event_bus


def test_coerce_payload_skips_json_for_empty_dict(monkeypatch) -> None:
    def fail_dumps(*_args, **_kwargs):
        raise AssertionError("empty event payload should not call json.dumps")

    monkeypatch.setattr(event_bus.json, "dumps", fail_dumps)

    assert event_bus._coerce_payload(None) == "{}"
    assert event_bus._coerce_payload({}) == "{}"


def test_coerce_payload_preserves_non_empty_json_encoding() -> None:
    payload = {"message": "hello", "created_at": datetime(2026, 5, 30, 12, 0)}

    encoded = event_bus._coerce_payload(payload)

    assert '"message": "hello"' in encoded
    assert '"created_at": "2026-05-30 12:00:00"' in encoded
