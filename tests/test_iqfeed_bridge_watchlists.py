from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_trade_bridge_watchlist_uses_ross_universe_without_eligible_rows(monkeypatch) -> None:
    mod = _load_script("iqfeed_trade_bridge_under_test", "scripts/iqfeed_trade_bridge.py")

    monkeypatch.setattr(mod, "_live_symbols", lambda: {"HELD"})
    monkeypatch.setattr(mod, "_eligible_symbols", lambda limit: [])
    monkeypatch.setattr(mod, "_ross_universe_symbols", lambda limit: ["CANF", "JEM", "BB"][:limit])

    assert mod._target_symbols(set(), 3) == {"HELD", "CANF", "JEM"}


def test_trade_bridge_watchlist_preserves_eligible_before_ross_fill(monkeypatch) -> None:
    mod = _load_script("iqfeed_trade_bridge_under_test_eligible", "scripts/iqfeed_trade_bridge.py")

    monkeypatch.setattr(mod, "_live_symbols", lambda: {"HELD"})
    monkeypatch.setattr(mod, "_eligible_symbols", lambda limit: ["ELAB"][:limit])
    monkeypatch.setattr(mod, "_ross_universe_symbols", lambda limit: ["CANF", "JEM"][:limit])

    assert mod._target_symbols(set(), 3) == {"HELD", "ELAB", "CANF"}


def test_trade_bridge_writes_nbbo_for_quote_only_l1_updates() -> None:
    mod = _load_script("iqfeed_trade_bridge_quote_only", "scripts/iqfeed_trade_bridge.py")
    mod._pending.clear()
    mod._pending_nbbo.clear()
    mod._last_trade.clear()

    first_print = "Q,CANF,4.10,100,09:30:01,1,1000,4.09,200,4.11,300"
    quote_only = "Q,CANF,4.10,100,09:30:01,1,1000,4.10,200,4.12,300"

    mod._parse_l1(first_print)
    assert len(mod._pending) == 1
    assert len(mod._pending_nbbo) == 1

    mod._parse_l1(quote_only)
    assert len(mod._pending) == 1
    assert len(mod._pending_nbbo) == 2
    assert mod._pending_nbbo[-1]["bid"] == 4.10
    assert mod._pending_nbbo[-1]["ask"] == 4.12


def test_depth_bridge_watchlist_uses_ross_universe_without_eligible_rows(monkeypatch) -> None:
    mod = _load_script("iqfeed_depth_bridge_under_test", "scripts/iqfeed_depth_bridge.py")

    monkeypatch.setattr(mod, "_live_symbols", lambda: {"HELD"})
    monkeypatch.setattr(mod, "_eligible_symbols", lambda limit: [])
    monkeypatch.setattr(mod, "_ross_universe_symbols", lambda limit: ["CANF", "JEM", "BB"][:limit])

    assert mod._target_symbols(set(), 3) == {"HELD", "CANF", "JEM"}
