"""f-position-identity-phase-3 (mig 249, 2026-05-18) — pin the shared
resolver + bracket_intents double-write + reader canary extension.

Phase 3 extends Phase 2's pattern from ``trading_execution_events`` to
``trading_bracket_intents``. Same resolver, same shape:

- ``app/services/trading/position_resolver.py`` is the shared module.
- ``execution_audit._resolve_position_id_for_event`` re-exports it
  (Phase 2's call sites + tests stay green).
- ``bracket_intent_writer.upsert_for_trade`` calls
  ``resolve_position_id(...)`` and writes the new column on INSERT.

Phase 3 invariants pinned here:

1. Shared resolver returns the right id on match (positive case).
2. Shared resolver returns None on miss without raising.
3. ``execution_audit._resolve_position_id_for_event`` STILL works
   (Phase 2 API unchanged via re-export).
4. Reader canary extended — no production code reads
   ``trading_bracket_intents.position_id`` either.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.trading.position_resolver import resolve_position_id
from app.services.trading.execution_audit import _resolve_position_id_for_event


# ── Fake session helpers (mirror Phase 2's pattern) ───────────────────


def _mock_db(rows):
    result = MagicMock()
    result.first.return_value = rows[0] if rows else None
    db = MagicMock()
    db.execute.return_value = result
    return db


# ── #1, #2 — shared resolver behavior ─────────────────────────────────


def test_shared_resolver_returns_id_on_match():
    db = _mock_db([(7,)])
    out = resolve_position_id(
        db, trade=None, user_id=1, ticker="ABEO",
        broker_source="robinhood", direction="long",
    )
    assert out == 7


def test_shared_resolver_returns_none_on_miss():
    db = _mock_db([])
    out = resolve_position_id(
        db, trade=None, user_id=1, ticker="UNKN",
        broker_source="robinhood", direction="long",
    )
    assert out is None


def test_shared_resolver_swallows_exceptions():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("DB broke")
    out = resolve_position_id(
        db, trade=None, user_id=1, ticker="ABEO",
        broker_source="robinhood",
    )
    assert out is None


def test_shared_resolver_handles_trade_object():
    db = _mock_db([(42,)])
    trade = SimpleNamespace(
        user_id=1, broker_source="ROBINHOOD",
        ticker="ETH-USD", direction="LONG",
    )
    out = resolve_position_id(db, trade=trade)
    assert out == 42
    # Verify lower-casing was applied to the bind params:
    call_kwargs = db.execute.call_args[0][1]
    assert call_kwargs["broker"] == "robinhood"
    assert call_kwargs["direction"] == "long"


def test_shared_resolver_returns_none_on_missing_inputs():
    db = _mock_db([(42,)])
    assert resolve_position_id(db, user_id=None, ticker="X", broker_source="y") is None
    assert resolve_position_id(db, user_id=1, ticker=None, broker_source="y") is None
    assert resolve_position_id(db, user_id=1, ticker="X", broker_source=None) is None


def test_shared_resolver_defaults_direction_to_long():
    db = _mock_db([(42,)])
    resolve_position_id(
        db, trade=None, user_id=1, ticker="ABEO",
        broker_source="robinhood", direction=None,
    )
    call_kwargs = db.execute.call_args[0][1]
    assert call_kwargs["direction"] == "long"


# ── #3 — Phase 2 re-export is intact ──────────────────────────────────


def test_phase2_re_export_delegates_to_shared_resolver():
    """The Phase 2 helper ``_resolve_position_id_for_event`` must still
    work with its existing signature — same call sites, same behavior."""
    db = _mock_db([(99,)])
    trade = SimpleNamespace(
        user_id=1, broker_source="coinbase", ticker="BTC-USD", direction="long",
    )
    out = _resolve_position_id_for_event(
        db, trade=trade, user_id=None, ticker=None, broker_source=None,
    )
    assert out == 99


def test_phase2_re_export_still_swallows_exceptions():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("DB broke")
    out = _resolve_position_id_for_event(
        db, trade=None, user_id=1, ticker="ABEO", broker_source="robinhood",
    )
    assert out is None


# ── #4 — reader canary covers BOTH columns now ────────────────────────


def test_no_reader_consults_position_id_on_bracket_intents_in_app_services():
    """Phase 3 is double-write only. No code in app/services should READ
    ``position_id`` from ``trading_bracket_intents`` or
    ``BracketIntent.position_id``.

    Update this canary when Phase 4 intentionally lands.
    """
    repo_root = Path(__file__).parent.parent
    app_services = repo_root / "app" / "services"

    forbidden_patterns = [
        r"\.position_id\s*[=!<>]",
        r"WHERE\s+position_id",
        r"SELECT[^\n]+position_id[^\n]+FROM\s+trading_bracket_intents",
        r"bracket_intent\.position_id",
        r"BracketIntent\.position_id",
        r"trading_bracket_intents\.position_id",
    ]

    # The writer is allowed (it sets position_id on INSERT).
    allowed = {
        str(repo_root / "app" / "services" / "trading" / "bracket_intent_writer.py"),
        str(repo_root / "app" / "services" / "trading" / "execution_audit.py"),
        str(repo_root / "app" / "services" / "trading" / "position_resolver.py"),
    }

    offenders = []
    for path in app_services.rglob("*.py"):
        if str(path) in allowed:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in forbidden_patterns:
            if re.search(pat, text):
                lines = text.splitlines()
                for ln, body in enumerate(lines, 1):
                    if re.search(pat, body) and not body.strip().startswith("#"):
                        offenders.append(f"{path}:{ln}: {body.strip()}")

    assert not offenders, (
        "Phase 3 reader canary fired — production code is reading position_id "
        "from trading_bracket_intents or BracketIntent. Phase 3 is supposed "
        "to be write-only until Phase 4. Offenders:\n  "
        + "\n  ".join(offenders[:20])
    )
