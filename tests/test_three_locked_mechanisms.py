"""Three mechanisms that were written, shipped, and never allowed to run (2026-09-07).

The operator's question was the right one: why keep describing these instead of enabling
them? Each was found the same way -- the code exists, it looks correct, and the receipts
say it produced nothing.

  1. The L10 monster structure floor: 143 rejects across 143 cases, ALL `leg_age_unknown`,
     ZERO candidates ever -- because `_parse_dt` is a name that does not exist. The string
     occurred exactly ONCE in the 48,073-line module: at the call site. No import, no def.
     The NameError was swallowed, so `leg_age_seconds` arrived as None and the candidate
     function returned before evaluating anything. Not inert -- broken, on every corpus.

  2. The L2 big-seller entry veto: `depth_imbal_pctile` is computed at pipeline.py:1249
     as `count(v <= now) / len(window)` with `now` inside the window, so its smallest
     possible value is `1/len(window)` = 0.1667 at the live K=6. The default floor is
     0.15 -- BELOW that -- so `pct <= floor` was arithmetically unsatisfiable for any
     book, any symbol, any window size. And the test that "proved" the veto worked
     fabricated a pctile of 0.05 with n_snaps=6, a value the real reader cannot produce.
     That is why the bad default survived review.

  3. The G4 ignition bypass: gated on `is_day_leader`, which was satisfied on NONE of the
     158 baseline replays. Its own comment argues the case without ever mentioning the
     leader board -- "Ross re-enters on the NEW structure's break, not the old failure's
     price" -- and its sibling leader gate in the same function was already generalised,
     returning +$424.79 on Ross winners with $0.00 and zero worsened across nine losers.

Runnable: pytest tests/test_three_locked_mechanisms.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import pipeline as pl


# ── 1. the NameError ─────────────────────────────────────────────────────────

def test_the_structure_floor_no_longer_calls_a_name_that_does_not_exist():
    src = inspect.getsource(lr)
    # the only surviving mentions are in the comment that records the incident
    calls = [
        ln for ln in src.splitlines()
        if "_parse_dt(" in ln and not ln.lstrip().startswith("#")
    ]
    assert not calls, calls


def test_it_parses_the_fill_time_with_the_same_idiom_the_function_already_uses():
    """Two readings of one field must not drift. `held` is derived from
    `pos["opened_at_utc"]` with `datetime.fromisoformat` at the top of the tick; the
    structure floor now reads it the same way."""
    src = inspect.getsource(lr.tick_live_session)
    i = src.find("_sf_opened_raw = pos.get(\"opened_at_utc\")")
    assert i > 0
    block = src[i:i + 800]
    assert "datetime.fromisoformat(" in block
    assert 'replace("Z", "+00:00")' in block
    # an unreadable stamp still yields None -> `leg_age_unknown`, the fail-safe that has
    # been the block's ONLY behaviour until today, on both sides of the try
    assert "_sf_age = None" in src[max(0, i - 1400):i]
    assert "_sf_age = None" in block


# ── 2. a floor the reader can never produce ──────────────────────────────────

def test_the_percentile_reader_cannot_return_less_than_one_over_the_window():
    """This is the arithmetic the floor was set below. It is not an opinion."""
    src = inspect.getsource(pl)
    i = src.find("pctile = sum(1 for v in imbs if v <= imb_now)")
    assert i > 0, "the percentile formula moved; re-derive the minimum before trusting it"
    # count(v <= now) includes `now` itself, so the numerator is at least 1
    for k in (3, 6, 10):
        imbs = [float(x) for x in range(k)]
        now = min(imbs)
        assert sum(1 for v in imbs if v <= now) / float(k) == 1.0 / k


def test_the_veto_floor_is_clamped_to_what_the_reader_can_express():
    src = inspect.getsource(eg)
    i = src.find("chili_momentum_entry_l2_bigseller_pctile_floor")
    assert i > 0
    block = src[i:i + 1600]
    assert "floor = max(floor, 1.0 / float(_l2_k))" in block
    # derived from the window, not typed -- so it stays right if K changes
    assert 'getattr(lr, "n_snaps", None)' in block


def test_no_fixture_feeds_the_veto_a_percentile_the_reader_cannot_produce():
    """The test that 'proved' this veto worked was built on an impossible book."""
    import pathlib

    p = pathlib.Path(inspect.getfile(eg)).resolve().parents[4] / "tests" / "test_dipbuy_quality_gates.py"
    text = p.read_text(encoding="utf-8")
    assert "depth_imbal_pctile=0.05," not in text
    assert "depth_imbal_pctile=1.0 / 6.0," in text


# ── 3. the bypass nobody could reach ─────────────────────────────────────────

def test_the_ignition_bypass_is_not_gated_on_being_the_days_number_one_name():
    from app.services.trading.momentum_neural import risk_policy as rp

    src = inspect.getsource(rp)
    assert "if int(escalation_level or 0) <= 1 and structural_trigger and _tape_positive():" in src
    assert "if is_day_leader and structural_trigger and _tape_positive():" not in src
    # the grant still says which evidence carried it, as its sibling does
    assert '"structural_tape_any_name"' in src
