"""Tests for deterministic trade revalidation — the native mechanics that
replace the per-candidate LLM viability call. Mirrors
prompts/auto_trader_revalidation.txt: catch HARD invalidations only (stop hit,
target met, incoherent levels, corrupt data); never second-guess the strategy.
"""
from types import SimpleNamespace

from app.services.trading.auto_trader_revalidation import deterministic_revalidation


def _alert(entry=100.0, stop=95.0, target=110.0, direction="long"):
    return SimpleNamespace(
        entry_price=entry, stop_loss=stop, target_price=target, direction=direction
    )


def test_viable_long_price_near_entry():
    viable, snap = deterministic_revalidation(_alert(), current_price=99.0)
    assert viable is True
    assert snap["reason"] == "ok"
    assert 0.0 <= snap["confidence"] <= 1.0
    assert snap["mode"] == "deterministic"


def test_viable_long_price_slightly_below_entry():
    # Prompt: "Entering SLIGHTLY BELOW entry_price is NOT a reason to reject."
    viable, _ = deterministic_revalidation(_alert(), current_price=96.0)
    assert viable is True


def test_blocks_price_through_stop_long():
    viable, snap = deterministic_revalidation(_alert(), current_price=95.0)  # at stop
    assert viable is False
    assert snap["reason"] == "price_through_stop"
    viable2, _ = deterministic_revalidation(_alert(), current_price=94.0)  # below stop
    assert viable2 is False


def test_blocks_target_already_met_long():
    viable, snap = deterministic_revalidation(_alert(), current_price=110.0)  # at target
    assert viable is False
    assert snap["reason"] == "target_already_met"
    viable2, _ = deterministic_revalidation(_alert(), current_price=112.0)  # beyond
    assert viable2 is False


def test_blocks_incoherent_levels_long():
    # stop above entry for a long.
    viable, snap = deterministic_revalidation(
        _alert(entry=100, stop=105, target=110), current_price=101
    )
    assert viable is False
    assert snap["reason"] == "incoherent_levels"
    # target below entry for a long.
    viable2, snap2 = deterministic_revalidation(
        _alert(entry=100, stop=95, target=98), current_price=99
    )
    assert viable2 is False
    assert snap2["reason"] == "incoherent_levels"


def test_blocks_corrupt_data():
    for bad in (0.0, -1.0, None, float("nan")):
        viable, snap = deterministic_revalidation(_alert(entry=bad), current_price=99.0)
        assert viable is False
        assert snap["reason"] == "data_corrupt"
    viable, snap = deterministic_revalidation(_alert(), current_price=0.0)
    assert viable is False and snap["reason"] == "data_corrupt"


def test_short_direction_mirrored():
    # Short: stop ABOVE entry ABOVE target.
    a = _alert(entry=100, stop=105, target=90, direction="short")
    viable, _ = deterministic_revalidation(a, current_price=101)
    assert viable is True
    v2, s2 = deterministic_revalidation(a, current_price=105)  # through stop (>=)
    assert v2 is False and s2["reason"] == "price_through_stop"
    v3, s3 = deterministic_revalidation(a, current_price=90)  # target met (<=)
    assert v3 is False and s3["reason"] == "target_already_met"


def test_no_momentum_or_caution_rejection():
    # The deterministic gate has NO momentum/caution inputs, so a valid-geometry
    # setup is always viable — parity with the prompt's explicit "do NOT reject
    # for weak momentum / general caution / wait-for-confirmation".
    viable, _ = deterministic_revalidation(_alert(), current_price=97.5)
    assert viable is True
