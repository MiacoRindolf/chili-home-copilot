from __future__ import annotations

from app.services.trading.breadth_relstr_model import _pick_leader_laggard


def test_pick_leader_laggard_matches_full_sort_with_ties() -> None:
    sector_map = {
        "XLK": {"rs_vs_spy_20d": 0.05},
        "XLB": {"rs_vs_spy_20d": 0.05},
        "XLE": {"rs_vs_spy_20d": -0.03},
        "XLV": {"rs_vs_spy_20d": -0.03},
        "XLF": {"rs_vs_spy_20d": 0.01},
    }

    candidates = sorted(
        (
            (sym, float(rec["rs_vs_spy_20d"]))
            for sym, rec in sector_map.items()
        ),
        key=lambda kv: (kv[1], kv[0]),
    )
    laggard_sym, laggard_rs = candidates[0]
    leader_sym, leader_rs = candidates[-1]

    assert _pick_leader_laggard(sector_map) == (
        leader_sym,
        laggard_sym,
        leader_rs,
        laggard_rs,
    )


def test_pick_leader_laggard_ignores_missing_and_bad_values() -> None:
    assert _pick_leader_laggard(
        {
            "XLK": {"rs_vs_spy_20d": None},
            "XLB": {"rs_vs_spy_20d": "bad"},
            "XLE": {"rs_vs_spy_20d": 0.02},
        }
    ) == ("XLE", "XLE", 0.02, 0.02)


def test_pick_leader_laggard_empty_when_no_valid_candidates() -> None:
    assert _pick_leader_laggard({"XLK": {"rs_vs_spy_20d": None}}) == (
        None,
        None,
        None,
        None,
    )
