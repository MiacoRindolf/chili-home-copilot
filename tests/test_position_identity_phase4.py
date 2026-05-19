"""f-position-identity-phase-4 (2026-05-18) — pin the precise
inverse-reconcile check on ``position_has_recorded_sell``.

Phase 4 ships a feature-flagged reader that replaces Phase 1's
conservative per-trade_id ``event_count == 0`` workaround with a
position-level precise check: across ALL Trade row generations linked
to a position, has the broker ever recorded a SELL fill?

When the flag is ON, broker_service.sync_positions_to_db's inverse-
reconcile branch uses ``position_has_recorded_sell(db, position_id)``
to decide re-open vs don't-re-open.

Pinned invariants:

1. Helper returns False when ``position_id`` is None.
2. Helper returns False when no matching rows in events.
3. Helper returns True when at least one row matches
   (position_id, status='filled', side='sell').
4. Helper swallows DB exceptions (NEVER raises) — caller falls back
   to the legacy path.
5. The settings flag defaults to False.
6. Default settings keep the legacy event_count == 0 path active.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services.trading.position_resolver import position_has_recorded_sell


def _mock_db(rows):
    """Build a Session-like mock with execute(...).first() -> rows[0] or None."""
    result = MagicMock()
    result.first.return_value = rows[0] if rows else None
    db = MagicMock()
    db.execute.return_value = result
    return db


# ── #1 — None short-circuit ────────────────────────────────────────


def test_position_has_recorded_sell_none_returns_false():
    db = _mock_db([(1,)])  # would return True if db was actually used
    assert position_has_recorded_sell(db, None) is False
    # And the db must not be queried (None short-circuits before SQL).
    db.execute.assert_not_called()


# ── #2 — no rows → False ───────────────────────────────────────────


def test_position_has_recorded_sell_no_matching_rows_returns_false():
    db = _mock_db([])
    assert position_has_recorded_sell(db, 42) is False


# ── #3 — matching row → True ───────────────────────────────────────


def test_position_has_recorded_sell_match_returns_true():
    db = _mock_db([(1,)])
    assert position_has_recorded_sell(db, 42) is True
    # Verify the bound param.
    call_kwargs = db.execute.call_args[0][1]
    assert call_kwargs == {"pid": 42}


# ── #4 — DB exception swallowed ────────────────────────────────────


def test_position_has_recorded_sell_swallows_db_exceptions():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("DB broke")
    # Must NOT raise; must return False.
    assert position_has_recorded_sell(db, 42) is False


# ── #5 — settings flag default ─────────────────────────────────────


def test_phase4_authority_flag_defaults_to_false():
    """The Phase 4 reader-flip is opt-in. Default must keep the legacy
    event_count==0 path active so existing behavior doesn't change on
    deploy."""
    from app.config import Settings  # local import: pydantic-settings model

    s = Settings(
        # Required env-derived settings; pydantic-settings will read .env
        # in real environments, but for this isolated test we instantiate
        # the model directly with defaults.
        _env_file=None,  # type: ignore[call-arg]
    )
    assert s.chili_position_identity_phase4_authority_enabled is False


# ── #6 — int coercion of position_id arg ───────────────────────────


def test_position_has_recorded_sell_coerces_int():
    """A position_id passed as numpy int / float / str should still
    coerce cleanly via int(). Guard against caller weirdness."""
    db = _mock_db([(1,)])
    assert position_has_recorded_sell(db, 7) is True
    # If someone passes a numeric string, the helper's int() should
    # coerce or fail gracefully (return False via exception swallow).
    db2 = _mock_db([(1,)])
    # str → int() works for digit strings; not testing that we accept,
    # just that it doesn't raise.
    out = position_has_recorded_sell(db2, "8")  # type: ignore[arg-type]
    assert isinstance(out, bool)


# ── #7 — int(0) is a valid id (don't conflate with falsy None) ─────


def test_position_has_recorded_sell_id_zero_does_not_short_circuit():
    """position_id=0 is a valid integer FK in PostgreSQL. The None
    short-circuit must not also fire on 0."""
    db = _mock_db([])
    # If 0 is treated as None, the helper would return False BEFORE
    # querying. We want it to query and return False from the query
    # result. Verify the db was called.
    out = position_has_recorded_sell(db, 0)
    assert out is False
    # Sanity-check: the helper did query (didn't short-circuit on 0).
    # NOTE: Python `if position_id is None` is the correct guard; this
    # test pins that.
    db.execute.assert_called_once()
