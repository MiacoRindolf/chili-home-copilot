"""A replay-seeded Alpaca session must carry the same admission proof production writes.

MEASURED 2026-09-04, the first Ross-bench run on ``alpaca_spot``: 160,148 ticks mirrored,
2,670 grid steps, and EVERY tick returned ``skipped='alpaca_account_scope_unfrozen_or_
mismatched'``. ``states_visited`` stayed ``['queued_live']``; the event histogram was
empty. No script had ever run the FSM replay on the Alpaca family, and
``seed_replay_session`` wrote only the NON-Alpaca identity key.

These tests drive the two real gates in ``live_runner`` against a seeded session — not a
re-implementation of them — so a future change to either gate's field list shows up here
rather than as another silent zero-event bench.

DB: uses the ``db`` fixture (``_test``-suffixed sink, truncated per test).
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.live_fsm import STATE_QUEUED_LIVE
from app.models.trading import TradingAutomationSession


CERTIFIED_PAPER_ACCOUNT = "c7d421e0-4fae-4219-9503-5ce051d4d923"


@pytest.fixture(autouse=True)
def _certified_account(monkeypatch):
    """The gate compares the frozen id to ``settings.chili_alpaca_expected_account_id``.
    Pin it here so the test is hermetic — it must not depend on which worktree's ``.env``
    happens to carry the id (wt-bench does, wt-seams does not)."""
    monkeypatch.setattr(lr.settings, "chili_alpaca_expected_account_id", CERTIFIED_PAPER_ACCOUNT)
    monkeypatch.setattr(lr.settings, "chili_alpaca_paper", True)


def _seed(db, family: str):
    arm = rv3.RecordedArm(
        symbol="SDOT",
        live_eligible_at_utc="2026-06-26T13:05:00",
        viability_score=0.9,
        atr_pct=0.05,
    )
    seed = rv3.seed_replay_session(db, arm=arm, execution_family=family)
    return db.get(TradingAutomationSession, seed.session_id)


def test_alpaca_seed_passes_the_account_scope_gate(db):
    """``alpaca_spot`` only: the quarantine gate refuses ``alpaca_short`` unconditionally
    at live_runner.py:1552 (``alpaca_short_execution_not_certified``) — that is a
    certification decision, not a seeding gap, so the short family is exercised by the
    generation-gate test below and not by this one."""
    family = "alpaca_spot"
    sess = _seed(db, family)
    assert sess.state == STATE_QUEUED_LIVE
    assert lr._frozen_alpaca_account_scope(sess) == "alpaca:paper"
    frozen = lr._frozen_alpaca_account_id(sess)
    assert frozen
    # The gate compares the FROZEN id to the certified paper account in settings
    # (live_runner.py:1585-1592). Pin the equality so a settings drift surfaces here
    # instead of as a silent zero-event bench.
    expected = str(getattr(lr.settings, "chili_alpaca_expected_account_id", "") or "").strip()
    assert expected, "CHILI_ALPACA_EXPECTED_ACCOUNT_ID must be set for the Alpaca replay seed"
    assert frozen == expected
    # The exact gate that returned on every tick of the first bench run
    # (live_runner.py:1540-1595; the ``_persisted_`` twin lives in operator_actions).
    assert lr._alpaca_execution_quarantine_reason(sess) is None


@pytest.mark.parametrize("family", ["alpaca_spot", "alpaca_short"])
def test_alpaca_seed_passes_the_confirmed_generation_gate(db, family):
    """The order-side gate: six marker fields must equal their snapshot twins."""
    sess = _seed(db, family)
    assert lr._confirmed_alpaca_arm_generation_reason(sess) is None
    marker = sess.risk_snapshot_json["confirmed_arm_generation"]
    assert marker["version"] == 1
    assert marker["session_id"] == sess.id
    assert marker["source"] == "replay_v3.seed_replay_session"


def test_alpaca_seed_is_deterministic_not_wall_clock(db):
    """Every stamped instant must be the RECORDED arm anchor — invariant 4, as-of reads.

    A wall-clock stamp would make two seeds of the same window differ, which is the
    no-op A/B (verification step 4) failing before any lever runs.
    """
    sess = _seed(db, "alpaca_spot")
    snap = sess.risk_snapshot_json
    anchor = "2026-06-26T13:05:00"
    assert snap["arm_confirmed_at_utc"] == anchor
    assert snap["expires_at_utc"] == anchor
    assert snap["confirmed_arm_generation"]["confirmed_at_utc"] == anchor
    assert snap["confirmed_arm_generation"]["expires_at_utc"] == anchor


def test_non_alpaca_seed_is_untouched(db):
    """Additive: the Robinhood/Coinbase seed keeps its identity key and gains no Alpaca keys."""
    sess = _seed(db, "robinhood_spot")
    snap = sess.risk_snapshot_json
    from app.services.trading.venue.account_identity import NON_ALPACA_ACCOUNT_IDENTITY_KEY

    assert snap.get(NON_ALPACA_ACCOUNT_IDENTITY_KEY)
    assert "alpaca_account_scope" not in snap
    assert "confirmed_arm_generation" not in snap
