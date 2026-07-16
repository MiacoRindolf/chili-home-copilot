from __future__ import annotations

from scripts.verify_ross_live_eligible_hygiene import evaluate_live_eligible_rows


def _snapshot(symbol: str, *, price: float, change_pct: float, volume: int) -> dict:
    return {
        "ticker": symbol,
        "lastTrade": {"p": price},
        "todaysChangePerc": change_pct,
        "day": {"v": volume},
    }


def test_hygiene_accepts_profile_proven_smallcap() -> None:
    offenders = evaluate_live_eligible_rows(
        [{"id": 1, "symbol": "JEM", "explain_json": {}}],
        snapshot_by_symbol={"JEM": _snapshot("JEM", price=8.82, change_pct=123.0, volume=2_000_000)},
    )

    assert offenders == []


def test_hygiene_flags_broad_price_above_profile() -> None:
    offenders = evaluate_live_eligible_rows(
        [{"id": 2, "symbol": "META", "explain_json": {}}],
        snapshot_by_symbol={"META": _snapshot("META", price=700.0, change_pct=8.0, volume=1_000_000)},
    )

    assert len(offenders) == 1
    assert offenders[0]["symbol"] == "META"
    assert offenders[0]["reason"] == "ross_universe_price_above_profile"


def test_hygiene_flags_faded_smallcap_below_change_floor() -> None:
    offenders = evaluate_live_eligible_rows(
        [{"id": 3, "symbol": "DXST", "explain_json": {}}],
        snapshot_by_symbol={"DXST": _snapshot("DXST", price=1.9, change_pct=-8.0, volume=4_000_000)},
    )

    assert len(offenders) == 1
    assert offenders[0]["reason"] == "ross_universe_change_below_profile"
