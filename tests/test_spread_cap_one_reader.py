"""One reader for the two live spread caps (ported 2026-09-07).

WHY IT MOVED. JWEL 2026-08-10, the $13,000 / 3% canon. At 11:07:22 the entry gate emitted
``live_entry_final_bbo {"reason": "execution_bbo_ok", "bid": 5.64}``; the fill came back at
5.73 -- 158 bps above the bid it had just read -- and ONE SECOND LATER the risk gate was
rejecting the same book as ``wide_bbo_spread`` on every tick, while the bid fell 5.56 -> 5.30
in two seconds. ``max_loss_circuit`` cut the position four seconds after the fill for
-$269.55. Two gates, two answers about one book, one second apart.

The two settings were read at six sites with THREE different fallbacks: ``live_runner.py``
used the config defaults 12.0 / 300.0, this module used 60.0 / 800.0 -- five times and 2.7
times looser, on the largest veto in the system. Those fallbacks bind whenever ``settings``
is a mock, which is exactly the replay and captured-paper paths.

Worse, they were written as ``float(getattr(...) or 800.0)``. ``or`` treats 0.0 as absent, so
an operator who set a cap of zero -- "tolerate no spread at all" -- got 800 instead: the
tightest possible setting failed OPEN.

The fix was written on 2026-09-07 and never merged. It is ported here ALONE, without the G4
bypass change that shares its original branch, so the bench can price one lever at a time.

Runnable: pytest tests/test_spread_cap_one_reader.py -v   (DB-free)
"""
from __future__ import annotations

import inspect


def test_the_two_spread_caps_have_exactly_one_reader():
    from app.services.trading.momentum_neural import risk_policy as rp

    assert rp.resolve_spread_cap_bps("live") == 12.0
    assert rp.resolve_spread_cap_bps("abs_cap") == 300.0
    src = inspect.getsource(rp)
    for gone in ('"chili_momentum_risk_max_spread_bps_abs_cap", 800.0',
                 '"chili_momentum_risk_max_spread_bps_live", 60.0'):
        assert gone not in src, gone


def test_a_zero_spread_cap_is_preserved_not_treated_as_unset():
    """The tightest possible setting must not fail OPEN. `live_runner.py` already documents
    the correct contract: a 0.0 cap is a deliberate 'block all'; only None / NaN / inf /
    unparseable values fall back to the documented default."""
    from app.config import settings
    from app.services.trading.momentum_neural import risk_policy as rp

    original = settings.chili_momentum_risk_max_spread_bps_live
    try:
        object.__setattr__(settings, "chili_momentum_risk_max_spread_bps_live", 0.0)
        assert rp.resolve_spread_cap_bps("live") == 0.0
        for bad in (None, float("nan"), float("inf"), -1.0, "x"):
            object.__setattr__(settings, "chili_momentum_risk_max_spread_bps_live", bad)
            assert rp.resolve_spread_cap_bps("live") == 12.0, bad
    finally:
        object.__setattr__(settings, "chili_momentum_risk_max_spread_bps_live", original)


def test_the_defaults_are_the_config_field_defaults_and_nothing_else():
    """The whole defect was a second set of numbers living next to the first. If the Field
    defaults ever move, they move in config.py and the reader follows -- this pins them
    together so a drift cannot reopen the gap silently."""
    from app.config import Settings
    from app.services.trading.momentum_neural import risk_policy as rp

    fields = Settings.model_fields
    assert fields["chili_momentum_risk_max_spread_bps_live"].default == rp.resolve_spread_cap_bps("live")
    assert fields["chili_momentum_risk_max_spread_bps_abs_cap"].default == rp.resolve_spread_cap_bps("abs_cap")


def test_only_the_spread_cap_came_across_not_the_g4_bypass():
    """The original branch also generalised the G4 ignition bypass off `is_day_leader`. That
    is a BEHAVIOUR lever with its own measurement (the bench found it fires MORE at high
    escalation levels, and it has zero test coverage). Porting both at once would make the
    A/B unreadable, so this file asserts the bypass is untouched here."""
    from app.services.trading.momentum_neural import risk_policy as rp

    src = inspect.getsource(rp)
    assert "if is_day_leader and structural_trigger and _tape_positive():" in src
    assert '"structural_tape_any_name"' not in src
