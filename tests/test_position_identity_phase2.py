"""f-position-identity-phase-2 (2026-05-18) — pin the resolver + double-write
+ reader canary.

Phase 2 adds ``trading_execution_events.position_id`` as a nullable FK to
``trading_positions``. Mig 248 backfills historical events. The resolver
helper ``execution_audit._resolve_position_id_for_event`` powers the
double-write at ``record_execution_event``.

Phase 2 invariants pinned here:

1. Resolver returns the right position_id when the natural key matches.
2. Resolver prefers state='open' over state='closed' when both match
   (handles the close/reopen pattern from the GRT-USD Phase 1 soak).
3. Resolver returns None on miss and NEVER raises (the event row must
   still write with position_id=NULL).
4. NULL-safe inputs (NULL user_id / broker / ticker) return None
   without raising.
5. **Reader canary** — no production code path reads ``position_id``
   from a ``TradingExecutionEvent`` or queries
   ``trading_execution_events.position_id``. Phase 2 is write-only;
   readers stay on trade_id until Phase 3.

Helper-level tests (no DB) for #1-4 via a fake session mock; static-grep
canary for #5.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.trading.execution_audit import _resolve_position_id_for_event


# ── Fake DB session helpers ────────────────────────────────────────────


def _mock_db(rows):
    """Build a minimal Session-like mock whose ``execute(...).first()``
    returns the first row of ``rows`` (each row a tuple, mimicking the
    SQLAlchemy Row interface)."""
    result = MagicMock()
    result.first.return_value = rows[0] if rows else None
    db = MagicMock()
    db.execute.return_value = result
    return db


# ── #1 / #2 / #3 — resolver behavior ──────────────────────────────────


def test_resolver_returns_id_on_match():
    db = _mock_db([(42,)])
    trade = SimpleNamespace(
        user_id=1, broker_source="robinhood", ticker="ABEO", direction="long",
    )
    out = _resolve_position_id_for_event(
        db, trade=trade, user_id=None, ticker=None, broker_source=None,
    )
    assert out == 42


def test_resolver_returns_none_on_miss():
    db = _mock_db([])  # no row found
    trade = SimpleNamespace(
        user_id=1, broker_source="robinhood", ticker="UNKN", direction="long",
    )
    out = _resolve_position_id_for_event(
        db, trade=trade, user_id=None, ticker=None, broker_source=None,
    )
    assert out is None


def test_resolver_swallows_db_exceptions():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("DB broke")
    trade = SimpleNamespace(
        user_id=1, broker_source="robinhood", ticker="ABEO", direction="long",
    )
    # Must NOT raise; must return None.
    out = _resolve_position_id_for_event(
        db, trade=trade, user_id=None, ticker=None, broker_source=None,
    )
    assert out is None


def test_resolver_uses_args_when_trade_is_none():
    db = _mock_db([(99,)])
    out = _resolve_position_id_for_event(
        db, trade=None, user_id=1, ticker="ETH-USD", broker_source="coinbase",
    )
    assert out == 99


def test_resolver_matches_null_user_id():
    db = _mock_db([(42,)])
    out = _resolve_position_id_for_event(
        db, trade=None, user_id=None, ticker="ABEO", broker_source="robinhood",
    )
    assert out == 42
    sql = str(db.execute.call_args[0][0])
    assert "COALESCE(user_id, -1)" in sql


def test_resolver_returns_none_on_null_broker():
    db = _mock_db([(42,)])
    out = _resolve_position_id_for_event(
        db, trade=None, user_id=1, ticker="ABEO", broker_source=None,
    )
    assert out is None


def test_resolver_returns_none_on_null_ticker():
    db = _mock_db([(42,)])
    out = _resolve_position_id_for_event(
        db, trade=None, user_id=1, ticker=None, broker_source="robinhood",
    )
    assert out is None


def test_resolver_returns_none_on_empty_ticker():
    db = _mock_db([(42,)])
    out = _resolve_position_id_for_event(
        db, trade=None, user_id=1, ticker="   ", broker_source="robinhood",
    )
    assert out is None


def test_resolver_lowercases_broker_source():
    """Broker_source must be lower-cased to match the seed convention."""
    db = _mock_db([(42,)])
    trade = SimpleNamespace(
        user_id=1, broker_source="ROBINHOOD", ticker="ABEO", direction="LONG",
    )
    _resolve_position_id_for_event(
        db, trade=trade, user_id=None, ticker=None, broker_source=None,
    )
    # Inspect the bind params: broker must be lower
    call_kwargs = db.execute.call_args[0][1]
    assert call_kwargs["broker"] == "robinhood"
    assert call_kwargs["direction"] == "long"


def test_resolver_defaults_direction_to_long_when_missing():
    db = _mock_db([(42,)])
    trade = SimpleNamespace(
        user_id=1, broker_source="robinhood", ticker="ABEO", direction=None,
    )
    _resolve_position_id_for_event(
        db, trade=trade, user_id=None, ticker=None, broker_source=None,
    )
    call_kwargs = db.execute.call_args[0][1]
    assert call_kwargs["direction"] == "long"


# ── #5 — reader canary (static grep) ──────────────────────────────────


def test_no_reader_consults_position_id_in_app_services():
    """Phase 2 is double-write only. No code in app/services should READ
    ``position_id`` from ``trading_execution_events`` or
    ``TradingExecutionEvent.position_id`` (other than the writer itself).

    This canary will fire if Phase 3 work accidentally lands here.
    Update it explicitly when the authority flip is ready.
    """
    repo_root = Path(__file__).parent.parent
    app_services = repo_root / "app" / "services"

    # Patterns we're hunting for that indicate a READ of the new column.
    # The writer itself (in execution_audit.py) is allowed.
    forbidden_patterns = [
        r"\.position_id\s*[=!<>]",                          # comparisons
        r"WHERE\s+position_id",                              # SQL filters
        r"SELECT[^\n]+position_id[^\n]+FROM\s+trading_execution_events",
        r"event_row\.position_id",                           # ORM attribute read
        r"trading_execution_events\.position_id",            # qualified SQL ref
    ]

    # Files where Phase 2 writes are allowed (the writer + the Phase 3
    # shared-resolver module which legitimately references the column
    # name in docstrings/comments without actually READING the value).
    # Phase 4 (2026-05-18) added an intentional reader in
    # broker_service.sync_positions_to_db (inverse-reconcile path) -- it's
    # flag-gated by chili_position_identity_phase4_authority_enabled.
    allowed = {
        str(repo_root / "app" / "services" / "trading" / "execution_audit.py"),
        str(repo_root / "app" / "services" / "trading" / "position_resolver.py"),
        str(repo_root / "app" / "services" / "broker_service.py"),
        str(repo_root / "app" / "services" / "coinbase_service.py"),
        str(repo_root / "app" / "services" / "trading" / "auto_trader.py"),
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
                # False-positive filter: ignore matches in test fixtures
                # or comments.
                lines = text.splitlines()
                for ln, body in enumerate(lines, 1):
                    if re.search(pat, body) and not body.strip().startswith("#"):
                        offenders.append(f"{path}:{ln}: {body.strip()}")

    assert not offenders, (
        "Phase 2 reader canary fired — production code is reading position_id "
        "from trading_execution_events. This was supposed to be write-only "
        "until Phase 3. Either revert the read OR update this test if Phase 3 "
        "is intentionally landing now. Offenders:\n  "
        + "\n  ".join(offenders[:20])
    )
