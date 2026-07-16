from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path("scripts/ross_transcript_admission_audit.py")
    spec = importlib.util.spec_from_file_location("ross_transcript_admission_audit_under_test", path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_run_once_audits_trading_context_mentions_and_writes_jsonl(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    now = dt.datetime.now(dt.timezone.utc)
    transcript = tmp_path / "transcript.jsonl"
    out = tmp_path / "audit.jsonl"
    transcript.write_text(
        json.dumps({"ts": now.isoformat(), "text": "Watching CANF first pullback over VWAP"}) + "\n",
        encoding="utf-8",
    )
    events: list[str] = []

    class _DB:
        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    def admit(db, **kwargs):
        assert kwargs["symbol"] == "CANF"
        assert kwargs["dry_run"] is True
        assert kwargs["refresh_viability"] is True
        assert kwargs["ignore_cooldown"] is True
        return {"symbol": kwargs["symbol"], "would_admit": True, "skipped": "dry_run"}

    args = argparse.Namespace(
        path=str(transcript),
        out=str(out),
        lookback_seconds=90.0,
        max_symbols=8,
        max_lines=400,
        refresh_viability=True,
        ignore_market_hours=True,
        assume_live=True,
    )
    monkeypatch.setattr(mod, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(mod, "admit_ross_event", admit)

    rows = mod.run_once(args)

    assert rows[0]["symbol"] == "CANF"
    assert rows[0]["audit_rollback_only"] is True
    assert "Watching CANF" in rows[0]["transcript_text"]
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["symbol"] == "CANF"
    assert events == ["rollback", "close"]


def test_run_once_skips_non_trading_transcript(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    now = dt.datetime.now(dt.timezone.utc)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"ts": now.isoformat(), "text": "CANF is a word in random audio"}) + "\n",
        encoding="utf-8",
    )

    def fail_admit(*args, **kwargs):
        raise AssertionError("non-trading transcript should not reach admission")

    args = argparse.Namespace(
        path=str(transcript),
        out="-",
        lookback_seconds=90.0,
        max_symbols=8,
        max_lines=400,
        refresh_viability=True,
        ignore_market_hours=True,
        assume_live=True,
    )
    monkeypatch.setattr(mod, "admit_ross_event", fail_admit)

    assert mod.run_once(args) == []
