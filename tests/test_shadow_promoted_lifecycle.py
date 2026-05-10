"""f-promotion-pipeline-rebalance Phase 3 (2026-05-10).

Verify the ``shadow_promoted`` lifecycle stage:

* ``scan_pattern_eligible_main_imminent`` returns True for
  ``shadow_promoted`` patterns when the flag is on, False when off.
  Behavior for other lifecycle stages is unchanged (regression guards).
* ``is_shadow_promoted_pattern`` helper resolves the stage + flag
  combination correctly (pure unit, no DB).
* AutoTrader v1's ``_process_one_alert`` routes ``shadow_promoted``
  pattern alerts to shadow-log only — no broker call, no Trade row,
  audit row written with reason ``selector:shadow_promoted_pattern_eval``.
* AutoTrader v1's path for ``promoted`` patterns is BYTE-IDENTICAL
  pre/post Phase 3 (parity hard gate). Mixed alerts in the same tick
  route independently.

Test isolation: any case that needs the flag flipped uses
``monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", ...)``
to scope the flip to the test.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import models
from app.config import settings
from app.models.trading import (
    AutoTraderRun,
    BreakoutAlert,
    ScanPattern,
    Trade,
)
from app.services.trading import auto_trader
from app.services.trading.auto_trader import is_shadow_promoted_pattern
from app.services.trading.opportunity_scoring import (
    scan_pattern_eligible_main_imminent,
)


# ── Fixtures / helpers ───────────────────────────────────────────────


def _make_user(db, name: str = "shadow_u") -> models.User:
    u = models.User(name=name)
    db.add(u)
    db.flush()
    return u


def _make_pattern(
    db,
    *,
    name: str,
    lifecycle_stage: str,
    promotion_status: str = "",
) -> ScanPattern:
    pat = ScanPattern(
        name=name,
        rules_json={},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        confidence=0.7,
        evidence_count=10,
        active=True,
        promotion_status=promotion_status,
        lifecycle_stage=lifecycle_stage,
    )
    db.add(pat)
    db.flush()
    return pat


def _make_alert(db, *, pattern_id: int, ticker: str) -> BreakoutAlert:
    a = BreakoutAlert(
        scan_pattern_id=pattern_id,
        ticker=ticker,
        asset_type="stock",
        alert_tier="premium",
        score_at_alert=0.9,
        price_at_alert=10.0,
        indicator_snapshot={},
        signals_snapshot={},
        stop_loss=9.0,
        target_price=12.0,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


# ── Pure unit tests (no DB, no autotrader) ───────────────────────────


def test_is_shadow_promoted_pattern_true_when_stage_matches_and_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    pat = SimpleNamespace(lifecycle_stage="shadow_promoted")
    assert is_shadow_promoted_pattern(pat) is True


def test_is_shadow_promoted_pattern_false_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)
    pat = SimpleNamespace(lifecycle_stage="shadow_promoted")
    assert is_shadow_promoted_pattern(pat) is False


def test_is_shadow_promoted_pattern_false_for_promoted_regardless_of_flag(monkeypatch):
    pat = SimpleNamespace(lifecycle_stage="promoted")
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    assert is_shadow_promoted_pattern(pat) is False
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)
    assert is_shadow_promoted_pattern(pat) is False


def test_is_shadow_promoted_pattern_false_for_none_or_empty(monkeypatch):
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    assert is_shadow_promoted_pattern(None) is False
    assert is_shadow_promoted_pattern(SimpleNamespace(lifecycle_stage=None)) is False
    assert is_shadow_promoted_pattern(SimpleNamespace(lifecycle_stage="")) is False


def test_eligible_main_imminent_true_for_shadow_promoted_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    pat = SimpleNamespace(
        lifecycle_stage="shadow_promoted",
        promotion_status="",
    )
    assert scan_pattern_eligible_main_imminent(pat) is True


def test_eligible_main_imminent_false_for_shadow_promoted_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)
    pat = SimpleNamespace(
        lifecycle_stage="shadow_promoted",
        promotion_status="",
    )
    assert scan_pattern_eligible_main_imminent(pat) is False


def test_eligible_main_imminent_unchanged_for_promoted(monkeypatch):
    pat = SimpleNamespace(lifecycle_stage="promoted", promotion_status="")
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    assert scan_pattern_eligible_main_imminent(pat) is True
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)
    assert scan_pattern_eligible_main_imminent(pat) is True


def test_eligible_main_imminent_unchanged_for_live(monkeypatch):
    pat = SimpleNamespace(lifecycle_stage="live", promotion_status="")
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    assert scan_pattern_eligible_main_imminent(pat) is True
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)
    assert scan_pattern_eligible_main_imminent(pat) is True


def test_eligible_main_imminent_unchanged_for_challenged(monkeypatch):
    pat = SimpleNamespace(lifecycle_stage="challenged", promotion_status="")
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    assert scan_pattern_eligible_main_imminent(pat) is False
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)
    assert scan_pattern_eligible_main_imminent(pat) is False


# ── Integration: autotrader entry routing ────────────────────────────


def _autotrader_scaffold_patches():
    """Patches that bypass the rule gate / LLM / market-data / mutex /
    capital-resolve scaffolding so the test exercises just the
    lifecycle routing decision and (when applicable) the
    ``_execute_new_entry`` arg shape.
    """
    return [
        patch.object(auto_trader, "_current_price", return_value=10.0),
        # _ohlcv_summary hits yfinance for the alert ticker on every
        # _process_one_alert call (auto_trader.py:870). Synthetic
        # tickers like "PRTY1" / "SHDW1" trigger 3-attempt retry chains
        # that take ~3 minutes each and can drop the test DB connection
        # mid-test. Patch it to a deterministic stub.
        patch.object(auto_trader, "_ohlcv_summary", return_value="(test stub)"),
        patch.object(auto_trader, "count_autotrader_v1_open", return_value=0),
        patch.object(
            auto_trader, "count_autotrader_v1_open_by_lane", return_value={},
        ),
        patch.object(
            auto_trader, "autotrader_realized_pnl_today_et", return_value=0.0,
        ),
        patch.object(
            auto_trader, "autotrader_paper_realized_pnl_today_et", return_value=0.0,
        ),
        patch.object(auto_trader, "find_open_autotrader_trade", return_value=None),
        patch.object(auto_trader, "find_open_autotrader_paper", return_value=None),
        patch.object(auto_trader, "maybe_scale_in", return_value=None),
        patch.object(
            auto_trader, "passes_rule_gate", return_value=(True, "ok", {}),
        ),
        patch.object(
            auto_trader, "run_revalidation_llm", return_value=(True, {}),
        ),
        patch.object(
            auto_trader,
            "check_autopilot_entry_gate",
            return_value={"allowed": True, "reason": "free", "owner": None},
        ),
    ]


def test_autotrader_routes_shadow_promoted_to_shadow_log_no_broker_call(
    db, monkeypatch
):
    """A ``shadow_promoted`` pattern's alert is diverted to shadow-log
    audit only: no Trade row, no broker call, no _execute_new_entry."""
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)

    u = _make_user(db, name="shadow_route_user")
    pat = _make_pattern(
        db, name="shadow_pat_a", lifecycle_stage="shadow_promoted"
    )
    alert = _make_alert(db, pattern_id=pat.id, ticker="SHDW1")

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    exec_calls: list[dict] = []

    def _fail_exec(db_, uid, alert_, px, snap, llm_snap, live, out_):
        exec_calls.append({"alert_id": alert_.id})

    with patch.object(auto_trader, "_execute_new_entry", side_effect=_fail_exec):
        for p in _autotrader_scaffold_patches():
            p.start()
        try:
            auto_trader._process_one_alert(db, u.id, alert, out, runtime)
        finally:
            patch.stopall()

    # Diverted to shadow-log: blocked decision with the eval reason,
    # no _execute_new_entry, no Trade row.
    assert out["skipped"] == 1
    assert out["entered"] == 0
    assert exec_calls == []
    runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id)
        .all()
    )
    assert len(runs) == 1
    assert runs[0].decision == "blocked"
    assert runs[0].reason == "selector:shadow_promoted_pattern_eval"
    trades = db.query(Trade).filter(Trade.ticker == "SHDW1").all()
    assert trades == []


def test_autotrader_byte_identical_for_promoted_pattern(db, monkeypatch):
    """HARD GATE PARITY: a ``promoted`` pattern's alert reaches
    ``_execute_new_entry`` with identical args whether the
    Phase 3 helper is in place or stubbed-out (simulating pre-Phase-3).
    The new shadow-promoted check is a no-op for non-shadow_promoted
    patterns by construction; this test asserts that operationally.
    """
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)

    u = _make_user(db, name="parity_user")

    def _run_once(*, force_helper_false: bool):
        pat = _make_pattern(
            db, name=f"parity_pat_{force_helper_false}", lifecycle_stage="promoted"
        )
        alert = _make_alert(
            db, pattern_id=pat.id,
            ticker=f"PRTY{1 if force_helper_false else 2}",
        )
        out = {"scaled_in": 0, "skipped": 0, "entered": 0}
        runtime = {"live_orders_effective": True, "paper_mode_effective": False}
        captured: list[dict] = []

        def _spy(db_, uid, alert_, px, snap, llm_snap, live, out_):
            captured.append({
                "uid": uid,
                "alert_id": alert_.id,
                "ticker": alert_.ticker,
                "px": px,
                "live": live,
                "snap": dict(snap or {}),
                "llm_snap": dict(llm_snap or {}),
            })
            out_["entered"] = out_.get("entered", 0) + 1

        patches = list(_autotrader_scaffold_patches())
        patches.append(patch.object(auto_trader, "_execute_new_entry", side_effect=_spy))
        if force_helper_false:
            # Simulate pre-Phase-3 by forcing the helper to return False
            # universally. Promoted patterns should ALSO take the same
            # path (helper already returns False for them); this proves
            # the code path through _process_one_alert is identical.
            patches.append(
                patch.object(
                    auto_trader, "is_shadow_promoted_pattern", return_value=False,
                )
            )

        for p in patches:
            p.start()
        try:
            auto_trader._process_one_alert(db, u.id, alert, out, runtime)
        finally:
            patch.stopall()

        assert out["entered"] == 1, (
            f"force_helper_false={force_helper_false}: "
            f"_execute_new_entry not invoked; out={out}"
        )
        assert out["skipped"] == 0
        return captured[0]

    baseline = _run_once(force_helper_false=True)
    shipped = _run_once(force_helper_false=False)

    # The fields that must be byte-identical: uid, px, live boolean,
    # ticker shape, and the snap/llm_snap content. alert_id differs
    # because each run inserts a fresh BreakoutAlert; so we strip it.
    for k in ("uid", "px", "live"):
        assert baseline[k] == shipped[k], (
            f"PARITY VIOLATION on field {k!r}: "
            f"baseline={baseline[k]!r} shipped={shipped[k]!r}"
        )
    # snap is built by passes_rule_gate (mocked to {}) — the autotrader
    # mutates it in _execute_new_entry but BEFORE that call it is the
    # same {}; capture both before mutation.
    assert baseline["snap"] == shipped["snap"], (
        f"PARITY VIOLATION on snap: "
        f"baseline={baseline['snap']!r} shipped={shipped['snap']!r}"
    )
    assert baseline["llm_snap"] == shipped["llm_snap"]


def test_autotrader_mixed_alerts_route_independently(db, monkeypatch):
    """Two alerts in the same loop, one from a ``shadow_promoted``
    pattern and one from a ``promoted`` pattern, route to the right
    paths independently."""
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)

    u = _make_user(db, name="mixed_user")
    pat_shadow = _make_pattern(
        db, name="mixed_shadow", lifecycle_stage="shadow_promoted"
    )
    pat_promoted = _make_pattern(
        db, name="mixed_promoted", lifecycle_stage="promoted"
    )
    alert_shadow = _make_alert(db, pattern_id=pat_shadow.id, ticker="SMIX1")
    alert_promoted = _make_alert(db, pattern_id=pat_promoted.id, ticker="PMIX1")

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    exec_calls: list[int] = []

    def _spy(db_, uid, alert_, px, snap, llm_snap, live, out_):
        exec_calls.append(int(alert_.id))
        out_["entered"] = out_.get("entered", 0) + 1

    patches = list(_autotrader_scaffold_patches())
    patches.append(patch.object(auto_trader, "_execute_new_entry", side_effect=_spy))
    for p in patches:
        p.start()
    try:
        auto_trader._process_one_alert(db, u.id, alert_shadow, out, runtime)
        auto_trader._process_one_alert(db, u.id, alert_promoted, out, runtime)
    finally:
        patch.stopall()

    # Promoted alert → executed exactly once. Shadow alert → not executed.
    assert exec_calls == [int(alert_promoted.id)]
    assert out["entered"] == 1
    assert out["skipped"] == 1

    # Audit rows: shadow_promoted_pattern_eval for the shadow alert,
    # nothing blocked for the promoted alert (it reached _execute_new_entry).
    shadow_runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert_shadow.id)
        .all()
    )
    assert len(shadow_runs) == 1
    assert shadow_runs[0].decision == "blocked"
    assert shadow_runs[0].reason == "selector:shadow_promoted_pattern_eval"

    promoted_runs_blocked = (
        db.query(AutoTraderRun)
        .filter(
            AutoTraderRun.breakout_alert_id == alert_promoted.id,
            AutoTraderRun.decision == "blocked",
        )
        .all()
    )
    assert promoted_runs_blocked == []


def test_autotrader_shadow_promoted_with_flag_off_falls_through_to_lifecycle_reject(
    db, monkeypatch
):
    """Defense-in-depth: with the flag OFF, an in-flight
    ``shadow_promoted`` alert is rejected by the existing
    ``pattern_lifecycle_not_eligible`` gate (pre-Phase-3 behavior). No
    broker call either way."""
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", False)

    u = _make_user(db, name="flag_off_user")
    pat = _make_pattern(
        db, name="flag_off_shadow", lifecycle_stage="shadow_promoted"
    )
    alert = _make_alert(db, pattern_id=pat.id, ticker="OFF1")

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    exec_calls: list[int] = []

    def _fail_exec(db_, uid, alert_, px, snap, llm_snap, live, out_):
        exec_calls.append(int(alert_.id))

    patches = list(_autotrader_scaffold_patches())
    patches.append(patch.object(auto_trader, "_execute_new_entry", side_effect=_fail_exec))
    for p in patches:
        p.start()
    try:
        auto_trader._process_one_alert(db, u.id, alert, out, runtime)
    finally:
        patch.stopall()

    assert out["skipped"] == 1
    assert out["entered"] == 0
    assert exec_calls == []
    runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id)
        .all()
    )
    assert len(runs) == 1
    assert runs[0].decision == "skipped"
    assert (runs[0].reason or "").startswith(
        "pattern_lifecycle_not_eligible:shadow_promoted"
    )
    trades = db.query(Trade).filter(Trade.ticker == "OFF1").all()
    assert trades == []
