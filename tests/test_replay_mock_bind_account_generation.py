"""The replay mock must answer the runner's account bind exactly as the real adapter does.

MEASURED 2026-09-04 (SDOT 2026-06-26, alpaca_spot, stride 1). After #1317 froze the
admission proof and #1318 made the bench refuse to run without the certified account id,
the run STILL finished in 68 s with zero events and ``states_visited=['queued_live']``.
The driver's last_result on every grid step:

    {'ok': True, 'skipped': 'alpaca_adapter_account_generation_bind_failed', 'broker_calls': 0}

live_runner.py:30331-30340 requires ``adapter.bind_account_id(frozen_id) is True`` before
the first broker call of an Alpaca tick, and ``MockBrokerAdapter`` had no such method.
These tests pin (1) the mock's bind semantics line-for-line against
``AlpacaSpotAdapter.bind_account_id`` (venue/alpaca_spot.py:611-619), (2) the ONE identity
source the seeder and the driver's mock now share, and (3) the exact runner check against a
seeded session.
"""
from __future__ import annotations

import pathlib

import pytest

from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.replay_mock_broker import (
    REPLAY_MOCK_ACCOUNT_IDENTITY,
    MockBrokerAdapter,
)


CERTIFIED_PAPER_ACCOUNT = "c7d421e0-4fae-4219-9503-5ce051d4d923"


# -- 1. bind semantics, DB-free ---------------------------------------------------

def test_bind_accepts_exactly_this_brokers_account():
    mock = MockBrokerAdapter(account_identity=CERTIFIED_PAPER_ACCOUNT)
    assert mock.bind_account_id(CERTIFIED_PAPER_ACCOUNT) is True


def test_bind_refuses_another_account_and_the_empty_id():
    mock = MockBrokerAdapter(account_identity=CERTIFIED_PAPER_ACCOUNT)
    assert mock.bind_account_id("some-other-account") is False
    assert mock.bind_account_id("") is False
    assert mock.bind_account_id(None) is False  # type: ignore[arg-type]


def test_bind_is_idempotent_but_never_rebinds_to_a_different_id():
    mock = MockBrokerAdapter(account_identity=CERTIFIED_PAPER_ACCOUNT)
    assert mock.bind_account_id(CERTIFIED_PAPER_ACCOUNT) is True
    assert mock.bind_account_id(CERTIFIED_PAPER_ACCOUNT) is True
    mock.set_account_identity("rotated-account")
    # Bound generation is frozen: the identity rotation seam cannot re-bind it.
    assert mock.bind_account_id("rotated-account") is False


def test_the_default_mock_identity_never_binds_a_certified_id():
    """A mock built without the certified id must NOT bind it -- otherwise a bench could
    bind the placeholder identity to a certified frozen session and fill against it."""
    assert MockBrokerAdapter().bind_account_id(CERTIFIED_PAPER_ACCOUNT) is False


# -- 2. one identity source for seeder + mock --------------------------------------

def test_identity_is_the_certified_id_when_settings_carry_it(monkeypatch):
    monkeypatch.setattr(lr.settings, "chili_alpaca_expected_account_id", CERTIFIED_PAPER_ACCOUNT)
    assert rv3.replay_alpaca_account_identity() == CERTIFIED_PAPER_ACCOUNT
    assert rv3.replay_mock_identity_kwargs("alpaca_spot") == {"account_identity": CERTIFIED_PAPER_ACCOUNT}
    assert rv3.replay_mock_identity_kwargs("alpaca_short") == {"account_identity": CERTIFIED_PAPER_ACCOUNT}


def test_identity_falls_back_to_the_mock_placeholder_and_says_so(monkeypatch):
    monkeypatch.setattr(lr.settings, "chili_alpaca_expected_account_id", "")
    assert rv3.replay_alpaca_account_identity() == REPLAY_MOCK_ACCOUNT_IDENTITY


@pytest.mark.parametrize("family", ["robinhood_agentic_mcp", "coinbase_spot", None])
def test_non_alpaca_families_get_no_identity_kwargs(family):
    assert rv3.replay_mock_identity_kwargs(family) == {}


def test_apply_sets_identity_on_the_instance_for_alpaca_only(monkeypatch):
    monkeypatch.setattr(lr.settings, "chili_alpaca_expected_account_id", CERTIFIED_PAPER_ACCOUNT)
    alpaca = rv3.apply_replay_mock_identity(MockBrokerAdapter(), "alpaca_spot")
    assert alpaca.get_account_identity_truth()["identity"] == CERTIFIED_PAPER_ACCOUNT
    assert alpaca.bind_account_id(CERTIFIED_PAPER_ACCOUNT) is True
    rh = rv3.apply_replay_mock_identity(MockBrokerAdapter(), "robinhood_agentic_mcp")
    assert rh.get_account_identity_truth()["identity"] == REPLAY_MOCK_ACCOUNT_IDENTITY


def test_the_driver_applies_identity_right_after_the_pinned_parity_construction():
    """The parity string is pinned verbatim by test_replay_v3_fsm_window_extensions; the
    identity must be applied on the very next statement, on the instance, not as a kwarg
    -- otherwise the 2026-09-04 zero-event alpaca run silently comes back."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "replay_v3_fsm_window.py").read_text(
        encoding="utf-8"
    )
    i = src.index("def run_arm(")
    body = src[i : src.index("\ndef ", i + 1)]
    j = body.index("mock = rv3.MockBrokerAdapter(**_PARITY_MOCK_KWARGS)")
    tail = body[j:]
    assert "rv3.apply_replay_mock_identity(mock, EXEC_FAMILY)" in tail
    assert tail.index("rv3.apply_replay_mock_identity(mock, EXEC_FAMILY)") < tail.index("assert_mock_parity(")


# -- 3. the exact runner check against a seeded session (DB) -----------------------

@pytest.fixture
def _certified(monkeypatch):
    monkeypatch.setattr(lr.settings, "chili_alpaca_expected_account_id", CERTIFIED_PAPER_ACCOUNT)
    monkeypatch.setattr(lr.settings, "chili_alpaca_paper", True)


def test_seeded_alpaca_session_binds_the_drivers_mock(db, _certified):
    arm = rv3.RecordedArm(
        symbol="SDOT",
        live_eligible_at_utc="2026-06-26T13:05:00",
        viability_score=0.9,
        atr_pct=0.05,
    )
    seed = rv3.seed_replay_session(db, arm=arm, execution_family="alpaca_spot")
    sess = db.get(TradingAutomationSession, seed.session_id)
    mock = MockBrokerAdapter(**rv3.replay_mock_identity_kwargs("alpaca_spot"))
    frozen = lr._frozen_alpaca_account_id(sess)
    assert frozen == CERTIFIED_PAPER_ACCOUNT
    # live_runner.py:30331-30340, verbatim condition
    bind_account = getattr(mock, "bind_account_id", None)
    assert callable(bind_account) and frozen and bind_account(frozen) is True
    # and the gates that run before it still pass on the same session
    assert lr._alpaca_execution_quarantine_reason(sess) is None
    assert lr._confirmed_alpaca_arm_generation_reason(sess) is None
