from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path("scripts/ross_admission_dry_run_audit.py")
    spec = importlib.util.spec_from_file_location("ross_admission_dry_run_audit_under_test", path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_append_jsonl_writes_rollback_marker(tmp_path: Path) -> None:
    mod = _load_module()
    out = tmp_path / "audit.jsonl"

    mod._append_jsonl(out, [{"symbol": "LHAI", "audit_rollback_only": True}])

    row = json.loads(out.read_text(encoding="utf-8"))
    assert row == {"symbol": "LHAI", "audit_rollback_only": True}


def test_audit_rows_rolls_back_and_closes(monkeypatch) -> None:
    mod = _load_module()
    events: list[str] = []

    class _DB:
        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    def admit(db, **kwargs):
        assert kwargs["dry_run"] is True
        assert kwargs["ignore_cooldown"] is True
        return {"symbol": kwargs["symbol"], "skipped": "dry_run", "would_admit": True}

    args = argparse.Namespace(
        assume_live=True,
        source="iqfeed_l1_dry_run_audit",
        refresh_viability=False,
        ignore_market_hours=True,
    )
    monkeypatch.setattr(mod, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(mod, "admit_ross_event", admit)

    rows = mod._audit_rows(args, ["LHAI"])

    assert rows[0]["symbol"] == "LHAI"
    assert rows[0]["audit_rollback_only"] is True
    assert events == ["rollback", "close"]
