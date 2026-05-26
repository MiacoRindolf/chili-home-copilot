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
* AutoTrader v1's path for ``live`` patterns is BYTE-IDENTICAL
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
    PaperTrade,
    ScanPattern,
    Trade,
)
from app.services.trading import auto_trader
from app.services.trading.auto_trader import (
    LLM_REVALIDATION_SKIP_REASON_SHADOW_OBSERVATION,
    is_shadow_promoted_pattern,
)
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


def test_eligible_main_imminent_true_for_pilot_promoted_when_flag_on(monkeypatch):
    pat = SimpleNamespace(lifecycle_stage="pilot_promoted", promotion_status="")
    monkeypatch.setattr(settings, "chili_pilot_promoted_enabled", True)
    assert scan_pattern_eligible_main_imminent(pat) is True


def test_eligible_main_imminent_false_for_pilot_promoted_when_flag_off(monkeypatch):
    pat = SimpleNamespace(lifecycle_stage="pilot_promoted", promotion_status="")
    monkeypatch.setattr(settings, "chili_pilot_promoted_enabled", False)
    assert scan_pattern_eligible_main_imminent(pat) is False


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
        patch.object(auto_trader, "_maybe_check_feature_parity", return_value=None),
        patch.object(auto_trader, "_emit_netedge_shadow_score", return_value=None),
        patch.object(auto_trader, "_maybe_emit_regime_diagnostic", return_value=None),
        patch.object(
            auto_trader,
            "check_autopilot_entry_gate",
            return_value={"allowed": True, "reason": "free", "owner": None},
        ),
    ]


def test_autotrader_routes_shadow_promoted_to_shadow_log_no_broker_call(
    db, monkeypatch
):
    """A ``shadow_promoted`` pattern is broker-blocked but still produces
    a paper-shadow observation for learning."""
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    monkeypatch.setattr(
        settings, "chili_autotrader_shadow_promoted_paper_observation_enabled", True,
    )
    monkeypatch.setattr(settings, "chili_autotrader_paper_shadow_enabled", False)
    monkeypatch.setattr(
        settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(settings, "chili_autotrader_paper_shadow_max_open", 100)

    u = _make_user(db, name="shadow_route_user")
    pat = _make_pattern(
        db, name="shadow_pat_a", lifecycle_stage="shadow_promoted"
    )
    alert = _make_alert(db, pattern_id=pat.id, ticker="SHDW1")

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    from app.services.trading import paper_trading as pt_mod

    broker_buy = None
    patches = list(_autotrader_scaffold_patches())
    patches.extend([
        patch.object(
            auto_trader,
            "_resolve_entry_risk_notional",
            return_value=(100.0, {
                "notional_capital_usd": 10_000.0,
                "notional_explicit_fallback_usd": 100.0,
                "notional_risk_pct": 1.0,
            }),
        ),
        patch.object(pt_mod, "_compute_atr_levels", return_value=(9.0, 12.0, 1.0)),
        patch.object(
            pt_mod,
            "_apply_slippage",
            side_effect=lambda price, direction, is_entry: price,
        ),
        patch(
            "app.services.trading.position_sizer_emitter.emit_shadow_proposal",
            return_value=None,
        ),
        patch(
            "app.services.trading.position_sizer_writer.mode_is_authoritative",
            return_value=False,
        ),
    ])
    for p in patches:
        p.start()
    broker_patch = patch.object(auto_trader, "_execute_broker_buy")
    broker_buy = broker_patch.start()
    try:
        auto_trader._process_one_alert(db, u.id, alert, out, runtime)
    finally:
        broker_patch.stop()
        patch.stopall()

    # Blocked decision with the eval reason, no live Trade row, plus one
    # tagged paper-shadow observation for the learner.
    assert out["skipped"] == 1
    assert out["entered"] == 0
    broker_buy.assert_not_called()
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
    shadows = (
        db.query(PaperTrade)
        .filter(PaperTrade.paper_shadow_of_alert_id == alert.id)
        .all()
    )
    assert len(shadows) == 1
    assert shadows[0].ticker == "SHDW1"
    assert shadows[0].scan_pattern_id == pat.id
    assert shadows[0].signal_json["shadow_decision"] == "blocked_shadow_promoted"


def test_shadow_observation_skips_llm_revalidation_latency(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)
    monkeypatch.setattr(
        settings, "chili_autotrader_shadow_promoted_paper_observation_enabled", True,
    )
    monkeypatch.setattr(settings, "chili_autotrader_llm_revalidation_enabled", True)
    monkeypatch.setattr(
        settings,
        "chili_autotrader_llm_revalidation_skip_shadow_observation",
        True,
    )
    monkeypatch.setattr(settings, "chili_autotrader_paper_shadow_enabled", False)
    monkeypatch.setattr(
        settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True,
    )
    monkeypatch.setattr(settings, "chili_autotrader_paper_shadow_max_open", 100)

    u = _make_user(db, name="shadow_llm_skip_user")
    pat = _make_pattern(
        db, name="shadow_llm_skip_pat", lifecycle_stage="shadow_promoted"
    )
    alert = _make_alert(db, pattern_id=pat.id, ticker="SHLMSKIP")

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    from app.services.trading import paper_trading as pt_mod

    patches = list(_autotrader_scaffold_patches())
    patches.extend([
        patch.object(
            auto_trader,
            "_resolve_entry_risk_notional",
            return_value=(100.0, {
                "notional_capital_usd": 10_000.0,
                "notional_explicit_fallback_usd": 100.0,
                "notional_risk_pct": 1.0,
            }),
        ),
        patch.object(pt_mod, "_compute_atr_levels", return_value=(9.0, 12.0, 1.0)),
        patch.object(
            pt_mod,
            "_apply_slippage",
            side_effect=lambda price, direction, is_entry: price,
        ),
        patch(
            "app.services.trading.position_sizer_emitter.emit_shadow_proposal",
            return_value=None,
        ),
        patch(
            "app.services.trading.position_sizer_writer.mode_is_authoritative",
            return_value=False,
        ),
    ])
    for p in patches:
        p.start()
    broker_patch = patch.object(auto_trader, "_execute_broker_buy")
    broker_buy = broker_patch.start()
    llm_patch = patch.object(
        auto_trader,
        "run_revalidation_llm",
        side_effect=AssertionError("shadow observation should not call LLM"),
    )
    llm = llm_patch.start()
    try:
        auto_trader._process_one_alert(db, u.id, alert, out, runtime)
    finally:
        llm_patch.stop()
        broker_patch.stop()
        patch.stopall()

    broker_buy.assert_not_called()
    llm.assert_not_called()
    run = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id)
        .one()
    )
    assert run.reason == "selector:shadow_promoted_pattern_eval"
    assert run.rule_snapshot["llm_revalidation_skipped"] is True
    assert run.rule_snapshot["llm_revalidation_skip_reason"] == (
        LLM_REVALIDATION_SKIP_REASON_SHADOW_OBSERVATION
    )


def test_autotrader_byte_identical_for_live_pattern(db, monkeypatch):
    """HARD GATE PARITY: a ``live`` pattern's alert reaches
    ``_execute_new_entry`` with identical args whether the
    Phase 3 helper is in place or stubbed-out (simulating pre-Phase-3).
    The new shadow-promoted check is a no-op for non-shadow_promoted
    patterns by construction; this test asserts that operationally.
    """
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)

    u = _make_user(db, name="parity_user")

    def _run_once(*, force_helper_false: bool):
        pat = _make_pattern(
            db, name=f"parity_pat_{force_helper_false}", lifecycle_stage="live"
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
            # universally. Live patterns should ALSO take the same
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


def test_autotrader_promoted_requires_live_stage_for_live_orders(db, monkeypatch):
    """A promoted pattern can still be observed, but live broker orders require
    the explicit live lifecycle stage by default."""
    monkeypatch.setattr(settings, "chili_autotrader_live_requires_live_lifecycle", True)

    u = _make_user(db, name="promoted_live_gate_user")
    pat = _make_pattern(db, name="promoted_needs_live", lifecycle_stage="promoted")
    alert = _make_alert(db, pattern_id=pat.id, ticker="PLIVE1")

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
    assert exec_calls == []
    runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id)
        .all()
    )
    assert len(runs) == 1
    assert (runs[0].reason or "").startswith(
        "pattern_lifecycle_not_live_approved:promoted"
    )


def test_autotrader_mixed_alerts_route_independently(db, monkeypatch):
    """Two alerts in the same loop, one from a ``shadow_promoted``
    pattern and one from a ``live`` pattern, route to the right
    paths independently."""
    monkeypatch.setattr(settings, "chili_shadow_promoted_lifecycle_enabled", True)

    u = _make_user(db, name="mixed_user")
    pat_shadow = _make_pattern(
        db, name="mixed_shadow", lifecycle_stage="shadow_promoted"
    )
    pat_live = _make_pattern(
        db, name="mixed_live", lifecycle_stage="live"
    )
    alert_shadow = _make_alert(db, pattern_id=pat_shadow.id, ticker="SMIX1")
    alert_live = _make_alert(db, pattern_id=pat_live.id, ticker="PMIX1")

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}

    exec_calls: list[tuple[int, bool]] = []

    def _spy(db_, uid, alert_, px, snap, llm_snap, live, out_):
        observation = bool(getattr(alert_, "_chili_shadow_observation_only", False))
        exec_calls.append((int(alert_.id), observation))
        if observation:
            out_["skipped"] = out_.get("skipped", 0) + 1
        else:
            out_["entered"] = out_.get("entered", 0) + 1

    patches = list(_autotrader_scaffold_patches())
    patches.append(patch.object(auto_trader, "_execute_new_entry", side_effect=_spy))
    for p in patches:
        p.start()
    try:
        auto_trader._process_one_alert(db, u.id, alert_shadow, out, runtime)
        auto_trader._process_one_alert(db, u.id, alert_live, out, runtime)
    finally:
        patch.stopall()

    # Shadow alert reaches execution in observation mode so quote/sizing can
    # create paper evidence; live alert reaches normal execution.
    assert exec_calls == [
        (int(alert_shadow.id), True),
        (int(alert_live.id), False),
    ]
    assert out["entered"] == 1
    assert out["skipped"] == 1

    # The stubbed execution layer owns terminal audit rows in this test, so
    # _process_one_alert itself should not early-block either alert.
    shadow_runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert_shadow.id)
        .all()
    )
    assert shadow_runs == []

    live_runs_blocked = (
        db.query(AutoTraderRun)
        .filter(
            AutoTraderRun.breakout_alert_id == alert_live.id,
            AutoTraderRun.decision == "blocked",
        )
        .all()
    )
    assert live_runs_blocked == []


def test_autotrader_routes_shadow_signal_lane_to_observation_only(
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        settings,
        "chili_autotrader_shadow_signal_lane_observation_enabled",
        True,
    )

    u = _make_user(db, name="lane_shadow_user")
    pat = _make_pattern(
        db, name="pilot_lane_shadow", lifecycle_stage="pilot_promoted"
    )
    alert = _make_alert(db, pattern_id=pat.id, ticker="PLANE1")
    alert.indicator_snapshot = {
        "imminent_scorecard": {
            "signal_lane": auto_trader.SHADOW_NEAR_MISS_SIGNAL_LANE,
        },
    }
    db.add(alert)
    db.commit()
    db.refresh(alert)

    out = {"scaled_in": 0, "skipped": 0, "entered": 0}
    runtime = {"live_orders_effective": True, "paper_mode_effective": False}
    exec_calls: list[tuple[int, bool, str | None]] = []

    def _spy(db_, uid, alert_, px, snap, llm_snap, live, out_):
        exec_calls.append((
            int(alert_.id),
            bool(getattr(alert_, "_chili_shadow_observation_only", False)),
            getattr(alert_, "_chili_shadow_observation_reason", None),
        ))
        out_["skipped"] = out_.get("skipped", 0) + 1

    patches = list(_autotrader_scaffold_patches())
    patches.append(patch.object(auto_trader, "_execute_new_entry", side_effect=_spy))
    for p in patches:
        p.start()
    try:
        auto_trader._process_one_alert(db, u.id, alert, out, runtime)
    finally:
        patch.stopall()

    assert exec_calls == [(
        int(alert.id),
        True,
        auto_trader.SHADOW_OBSERVATION_REASON_SIGNAL_LANE,
    )]
    assert out["skipped"] == 1
    assert db.query(Trade).filter(Trade.ticker == "PLANE1").count() == 0


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
