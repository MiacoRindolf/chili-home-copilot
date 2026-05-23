"""f-coinbase-exit-side-recording (2026-05-19) — pin the writer hooks
that record sell-side execution_events in two Coinbase-touching paths.

Phase 4's ``position_has_recorded_sell`` helper queries
``trading_execution_events`` filtered on ``payload_json->>'side' = 'sell'``.
Without these writer hooks, Coinbase exits + stale-close auto-closures
would not accumulate sell events, and Phase 4 inverse-reconcile (when it
fires on Coinbase positions, even via the Robinhood path) would see
them as "no sell recorded → bookkeeping-only close → re-open".

Two writer sites pinned here:

1. ``trading/crypto/exit_monitor.py`` — after a successful market-sell
   submission via ``_place_market_sell_for_trade``. Covers both Coinbase
   AND Robinhood crypto exits (same function dispatches to both).

2. ``coinbase_service.py`` — after a stale-close auto-closure with
   ``exit_reason='coinbase_position_sync_gone'``. Writes a synthetic
   sell event since the position vanished from the broker.

Tests are static-grep against the source file because the surrounding
context (full DB session, broker mocks, full function-state) is too
heavy for unit-test isolation. Phase 2/3/4 already pin the helper
semantics; this file pins the WRITE-SITE existence.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


# ── #1 — crypto/exit_monitor.py writer hook ───────────────────────────


def test_crypto_exit_monitor_writes_sell_side_execution_event():
    """``trading/crypto/exit_monitor.py`` must call ``record_execution_event``
    after a successful ``_place_market_sell_for_trade`` with
    ``payload_json`` containing ``"side": "sell"``."""
    text = _read("app/services/trading/crypto/exit_monitor.py")

    assert "record_execution_event(" in text, (
        "crypto/exit_monitor.py is missing the record_execution_event "
        "call site. f-coinbase-exit-side-recording requires this writer "
        "hook so the Phase 4 helper sees crypto exits as recorded sells."
    )

    # The side='sell' payload entry must exist somewhere near the writer.
    assert re.search(r'"side"\s*:\s*"sell"', text), (
        "crypto/exit_monitor.py is missing the payload `\"side\": \"sell\"` "
        "entry. The Phase 4 helper queries "
        "`payload_json->>'side' = 'sell'`."
    )

    # The new event_type signal we use:
    assert "crypto_exit_submitted" in text, (
        "crypto/exit_monitor.py is missing the `crypto_exit_submitted` "
        "event_type label. Don't rename without updating audit queries."
    )


# ── #2 — coinbase_service.py stale-close writer hook ──────────────────


def test_coinbase_service_stale_close_writes_sell_side_event():
    """``coinbase_service.sync_positions_to_db`` stale-close branch must
    call ``record_execution_event`` after the ``coinbase_position_sync_gone``
    auto-close with ``payload_json`` containing ``"side": "sell"`` and a
    ``"synthetic": True`` flag (the position vanished, we didn't see a
    real fill)."""
    text = _read("app/services/coinbase_service.py")

    assert "record_execution_event(" in text, (
        "coinbase_service.py is missing the record_execution_event call. "
        "f-coinbase-exit-side-recording requires this writer hook so the "
        "Phase 4 helper sees coinbase_position_sync_gone closures as "
        "recorded sells."
    )

    assert re.search(r'"side"\s*:\s*"sell"', text), (
        "coinbase_service.py is missing the payload `\"side\": \"sell\"` "
        "entry. The Phase 4 helper queries `payload_json->>'side' = 'sell'`."
    )

    # The event_type label + the synthetic marker:
    event_type = "coinbase_sync_gone_close"
    assert len(event_type) <= 32
    assert event_type in text, (
        "coinbase_service.py is missing the "
        "`coinbase_sync_gone_close` event_type label. Don't "
        "rename without updating audit queries."
    )
    assert re.search(r'"synthetic"\s*:\s*True', text), (
        "coinbase_service.py is missing the `\"synthetic\": True` payload "
        "flag. This lets audit queries distinguish broker-truth-driven "
        "auto-closes from real explicit sells."
    )


# ── #3 — Both writers are wrapped in try/except (NEVER raise) ─────────


def _writer_wrapped_in_try_except(text: str, import_pat: str) -> bool:
    """Return True if there's a ``try:`` ... ``record_execution_event(`` ...
    ``except Exception:`` triple around the writer call. Walks line-by-line
    so dict literals (``{...}``) in the payload don't break detection
    (the prior regex used ``[^}]`` and false-negative'd on the payload)."""
    lines = text.splitlines()
    n = len(lines)
    for i, ln in enumerate(lines):
        if import_pat not in ln:
            continue
        # Look backwards up to 8 lines for `try:`
        try_found = any(
            lines[j].rstrip().endswith("try:")
            for j in range(max(0, i - 8), i)
        )
        if not try_found:
            continue
        # Look forward up to 60 lines for the writer call + an except
        writer_found = False
        except_found = False
        for k in range(i, min(n, i + 60)):
            if "record_execution_event(" in lines[k]:
                writer_found = True
            if writer_found and re.match(r"\s*except\b", lines[k]):
                except_found = True
                break
        if writer_found and except_found:
            return True
    return False


def test_crypto_exit_monitor_writer_wrapped_in_try():
    """The writer must NEVER block a successful exit submission. The
    surrounding try/except is load-bearing. If this regresses, a
    record-event DB error would orphan the exit and break the trade."""
    text = _read("app/services/trading/crypto/exit_monitor.py")
    assert _writer_wrapped_in_try_except(
        text, "from ..execution_audit import record_execution_event"
    ), (
        "crypto/exit_monitor.py record_execution_event call is not wrapped "
        "in try/except. f-coinbase-exit-side-recording mandates this so "
        "a writer DB error never blocks an already-submitted exit."
    )


def test_coinbase_service_stale_close_writer_wrapped_in_try():
    """Same invariant for the coinbase_service.py writer."""
    text = _read("app/services/coinbase_service.py")
    assert _writer_wrapped_in_try_except(
        text, "from .trading.execution_audit import record_execution_event"
    ), (
        "coinbase_service.py record_execution_event call is not wrapped "
        "in try/except. Required so a writer DB error never blocks "
        "the auto-close flow."
    )
