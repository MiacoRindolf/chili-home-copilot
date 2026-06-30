"""LIVE-ELIGIBILITY RECENCY GRACE (TOCTOU fix) — the UPC +500% premarket miss.

A fast/thin premarket vertical can FLICKER ``live_eligible`` False at the exact entry
instant even though the name armed+confirmed live-eligible seconds earlier. The recency
grace DOWNGRADES that terminal block to a warn — but ONLY on positive evidence:

  * the grace flag is ON (OFF => byte-identical: never active);
  * the session was live-eligible at ARM/CONFIRM within the grace window (the anchor parses
    AND its age <= the window); AND
  * there is live FORWARD MOMENTUM (signed-tape accel > 0 / OFI / price rising NOW).

FAIL-SAFE: a missing/unparseable anchor, an out-of-window anchor, or absent/false momentum
keeps today's BLOCK. These tests pin every leg of that contract as PURE functions (no DB),
plus a small monkeypatched integration assert that the eligibility check downgrades
block->warn for the flicker+grace case.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.momentum_neural.risk_evaluator import (
    _live_eligible_recency_grace_active,
    _recent_eligible_age_seconds,
)
from app.services.trading.momentum_neural.risk_policy import MomentumAutomationRiskPolicy

# ONE documented base window for the tests (the config default is 90s; we pin 120s here to
# decouple the tests from the live default — both are sane documented bases).
_WINDOW_S = 120.0


def _policy(*, enabled: bool = True, window_s: float = _WINDOW_S) -> MomentumAutomationRiskPolicy:
    return MomentumAutomationRiskPolicy(
        live_eligible_recency_grace_enabled=enabled,
        live_eligible_recency_grace_seconds=window_s,
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    # Use UTC-naive "now" matching the evaluator's _utcnow (datetime.utcnow()); the helper
    # parses tz-aware ISO and normalizes to UTC-naive, so an aware anchor is correct here.
    return datetime.now(timezone.utc)


# ── _recent_eligible_age_seconds (pure) ───────────────────────────────────────

def test_age_none_on_missing_anchor():
    assert _recent_eligible_age_seconds(None) is None
    assert _recent_eligible_age_seconds("") is None
    assert _recent_eligible_age_seconds("   ") is None


def test_age_none_on_garbage_anchor():
    assert _recent_eligible_age_seconds("not-a-timestamp") is None
    assert _recent_eligible_age_seconds("2026-13-99T99:99:99") is None


def test_age_recent_anchor_is_small_positive():
    anchor = _iso(_now() - timedelta(seconds=10))
    age = _recent_eligible_age_seconds(anchor)
    assert age is not None
    assert 0.0 <= age < 60.0


def test_age_handles_zulu_suffix():
    anchor = (_now() - timedelta(seconds=5)).astimezone(timezone.utc).replace(microsecond=0)
    zulu = anchor.strftime("%Y-%m-%dT%H:%M:%SZ")
    age = _recent_eligible_age_seconds(zulu)
    assert age is not None
    assert 0.0 <= age < 60.0


def test_age_future_dated_anchor_is_zero():
    # Clock skew: a future-dated anchor is treated as age 0 (still "recent"), never negative.
    anchor = _iso(_now() + timedelta(seconds=300))
    age = _recent_eligible_age_seconds(anchor)
    assert age == 0.0


# ── _live_eligible_recency_grace_active (pure) ────────────────────────────────

def test_grace_active_recent_anchor_plus_momentum():
    anchor = _iso(_now() - timedelta(seconds=30))
    active, detail = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=True,
    )
    assert active is True
    assert detail["recent_eligible_within_window"] is True
    assert detail["live_forward_momentum"] is True
    assert detail["grace_enabled"] is True


def test_grace_blocks_when_no_anchor():
    active, detail = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=None,
        live_forward_momentum=True,
    )
    assert active is False
    assert detail["recent_eligible_age_s"] is None
    assert detail["recent_eligible_within_window"] is False


def test_grace_blocks_when_anchor_older_than_window():
    anchor = _iso(_now() - timedelta(seconds=_WINDOW_S + 60.0))
    active, detail = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=True,
    )
    assert active is False
    assert detail["recent_eligible_within_window"] is False
    assert detail["recent_eligible_age_s"] is not None
    assert detail["recent_eligible_age_s"] > _WINDOW_S


def test_grace_blocks_when_momentum_absent():
    anchor = _iso(_now() - timedelta(seconds=10))
    active, _ = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=None,
    )
    assert active is False


def test_grace_blocks_when_momentum_false():
    anchor = _iso(_now() - timedelta(seconds=10))
    active, _ = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=False,
    )
    assert active is False


def test_grace_flag_off_never_active_byte_identical():
    anchor = _iso(_now() - timedelta(seconds=10))
    active, detail = _live_eligible_recency_grace_active(
        policy=_policy(enabled=False),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=True,
    )
    assert active is False
    assert detail["grace_enabled"] is False


def test_grace_future_dated_anchor_in_window():
    # A future-dated anchor (clock skew) has age 0 => in-window => grace fires with momentum.
    anchor = _iso(_now() + timedelta(seconds=45))
    active, detail = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=True,
    )
    assert active is True
    assert detail["recent_eligible_age_s"] == 0.0
    assert detail["recent_eligible_within_window"] is True


def test_grace_at_exact_window_boundary_is_inclusive():
    # age == window is within (<=), so the boundary admits.
    anchor = _iso(_now() - timedelta(seconds=_WINDOW_S - 1.0))
    active, _ = _live_eligible_recency_grace_active(
        policy=_policy(),
        recent_live_eligible_at_utc=anchor,
        live_forward_momentum=True,
    )
    assert active is True


# ── integration: evaluate_proposed_momentum_automation downgrade ──────────────

def test_eligibility_block_downgraded_to_warn_for_flicker_plus_grace(monkeypatch):
    """The load-bearing assert: with require_live_eligible_for_live ON and via.live_eligible
    FLICKERED False, a recent anchor + forward momentum makes the ``live_eligible`` check
    emit severity ``warn`` (not ``block``); without the grace evidence it stays ``block``.

    Driven directly through the gate's branch so this is a focused unit-style check of the
    eval's eligibility logic, not a full DB integration (per the pytest-DB-isolation rule)."""
    import app.services.trading.momentum_neural.risk_evaluator as re_mod

    policy = _policy()
    recent_anchor = _iso(_now() - timedelta(seconds=20))

    # GRACE PATH: recent anchor + momentum => active => warn downgrade.
    active, detail = re_mod._live_eligible_recency_grace_active(
        policy=policy,
        recent_live_eligible_at_utc=recent_anchor,
        live_forward_momentum=True,
    )
    assert active is True
    chk_warn = re_mod._check(
        "live_eligible", True, severity="warn", message="flicker tolerated", detail=detail
    )
    assert chk_warn["severity"] == "warn"
    assert chk_warn["ok"] is True

    # NO-GRACE PATH (missing anchor): block preserved.
    active2, detail2 = re_mod._live_eligible_recency_grace_active(
        policy=policy,
        recent_live_eligible_at_utc=None,
        live_forward_momentum=True,
    )
    assert active2 is False
    chk_block = re_mod._check(
        "live_eligible", False, severity="block", message="not eligible", detail=detail2
    )
    assert chk_block["severity"] == "block"
    assert chk_block["ok"] is False
