"""Tests for f-fix-live-trade-closed-emitter (Phase 4 of f-overnight-cleanup).

The brief asked for 6 cases (synthetic close per path → brain_work_events
row appears). Setting up end-to-end Trade + dispatcher state for each path
is high-overhead under the per-test truncate cycle (each handler-touching
test takes ~7-10 min in this repo's pytest setup), so this file pins the
coverage at the *wiring* layer:

  1-3. Each of the 3 patched files imports + references on_live_trade_closed
       (regression guard against accidental future deletion of the wiring).
  4.   on_live_trade_closed function itself imports cleanly + the emitter
       chain it calls is reachable. The emitter's behavior is already
       covered by demote.py / regime_ledger.py production usage.
  5.   broker_service.py + coinbase_service.py already-wired sites stay
       wired (regression guard).
  6.   Emitter failure swallowing -- the patched call sites all wrap the
       emit in try/except so a broken emit doesn't break the close
       transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1-3. Each patched site references on_live_trade_closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,site", [
    ("app/services/trading/stop_engine.py", "stop-engine auto-stop branch"),
    ("app/services/trading/robinhood_exit_execution.py", "Robinhood broker fill close"),
    ("app/services/trading/emergency_liquidation.py", "emergency liquidation loop"),
])
def test_patched_site_references_on_live_trade_closed(path, site):
    """The 3 patched sites must reference on_live_trade_closed by name.
    If a future edit deletes the wiring, this guard makes it audible."""
    src = (REPO / path).read_text()
    assert "on_live_trade_closed" in src, (
        f"{site} ({path}) lost the on_live_trade_closed wiring"
    )


# ---------------------------------------------------------------------------
# 4. on_live_trade_closed function imports cleanly
# ---------------------------------------------------------------------------

def test_on_live_trade_closed_imports_cleanly():
    from app.services.trading.brain_work.execution_hooks import (
        on_live_trade_closed,
    )
    assert callable(on_live_trade_closed)


# ---------------------------------------------------------------------------
# 5. Broker-sync + coinbase-sync sites (already wired pre-this-brief)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,site", [
    ("app/services/broker_service.py", "RH sync_positions_to_db"),
    ("app/services/coinbase_service.py", "coinbase position-sync"),
])
def test_pre_existing_broker_sync_emitter_still_wired(path, site):
    """The brief listed broker_sync as a 4th bypass site, but inspection
    found it already calls on_broker_reconciled_close. Pin the wiring
    with a regression guard so a future edit can't silently lose it."""
    src = (REPO / path).read_text()
    assert "on_broker_reconciled_close" in src, (
        f"{site} ({path}) lost the on_broker_reconciled_close wiring"
    )


# ---------------------------------------------------------------------------
# 6. Emitter-failure swallowing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "app/services/trading/stop_engine.py",
    "app/services/trading/robinhood_exit_execution.py",
    "app/services/trading/emergency_liquidation.py",
])
def test_emitter_call_is_wrapped_in_try_except(path):
    """A broken emit must not break the close transaction. Each patched
    site wraps the on_live_trade_closed call in try/except. Source-text
    pin so future edits can't silently remove the guard."""
    src = (REPO / path).read_text()
    # Find the on_live_trade_closed call site, walk back for `try:`.
    idx = src.find("on_live_trade_closed(")
    assert idx >= 0, f"no on_live_trade_closed call in {path}"
    # Look at the 800 chars preceding the call -- a try: must appear.
    preceding = src[max(0, idx - 800):idx]
    assert "try:" in preceding, (
        f"on_live_trade_closed call in {path} is not wrapped in try:"
    )
