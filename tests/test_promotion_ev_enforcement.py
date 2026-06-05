"""The realized-EV gate verdict must actually block promotion.

It was computed but discarded in two places: (1) a stale CPCV-only
promotion_gate_passed write before the EV gate runs, and (2) an exact-string
match in _finalize_cpcv_promotion_ready that missed 'realized_ev_gate_failed'.
Result: #1246 (win-rate 85.7%, avg_return -0.14%) graduated despite failing
realized-EV. These tests lock in that the EV verdict now sticks.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.trading import mining_validation as mv
from app.services.trading import promotion_gate as pg


def test_finalize_ready_false_on_realized_ev_block():
    cases = [
        ("realized_ev_gate_failed", False),
        ("cpcv_promotion_gate_failed", False),
        ("cpcv_promotion_gate_failed+realized_ev_gate_failed", False),
        ("existing+realized_ev_gate_failed", False),
        (None, True),
    ]
    for blocked, expect_ready in cases:
        def _fake(detail, filtered, *, n_hypotheses_tested, scan_pattern=None, _b=blocked):
            d = dict(detail)
            if _b:
                d["blocked"] = _b
            return d

        with patch.object(pg, "finalize_promotion_with_cpcv", side_effect=_fake):
            ready, _detail = mv._finalize_cpcv_promotion_ready({}, [], n_hypotheses_tested=1)
        assert ready is expect_ready, f"blocked={blocked} -> ready={ready}"


def test_ev_block_forces_promotion_gate_passed_false(monkeypatch):
    # Force CPCV to PASS, EV gate to FAIL — the exact case the old code let through.
    monkeypatch.setattr(
        pg,
        "evaluate_pattern_cpcv_realized_pnl",
        lambda *a, **k: {
            "cpcv_n_paths": 4,
            "deflated_sharpe": 1.0,
            "pbo": 0.0,
            "cpcv_median_sharpe": 5.0,
            "skipped": False,
        },
    )
    monkeypatch.setattr(pg, "promotion_gate_passes", lambda payload: (True, []))
    monkeypatch.setattr(
        "app.services.trading.realized_ev_gate.check_realized_ev_blocking",
        lambda sp: (True, ["avg_return_not_positive:-0.1400<=0.0"], {"corrected_avg_return_pct": -0.14}),
    )

    detail = pg.finalize_promotion_with_cpcv({}, [], n_hypotheses_tested=1, scan_pattern=None)

    assert "realized_ev_gate_failed" in str(detail.get("blocked"))
    # the persisted flag must now reflect the EV failure, not the stale CPCV pass
    assert detail["cpcv_promotion_gate"]["promotion_gate_passed"] is False
    fields = pg.cpcv_eval_to_scan_pattern_fields(detail["cpcv_promotion_gate"])
    assert fields["promotion_gate_passed"] is False
