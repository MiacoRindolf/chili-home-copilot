"""Vertical-contamination relief sa break volume gate (HUIZ 08-20 second leg).

Ang rolling-MEAN denominator ng vol_ratio ay pinapalobo ng vertical (monster
bars sa trailing 20) kaya ang malaki-pa-ring shelf-break bar ay nagre-reject ng
break_low_volume. Ang median ng parehong window ay immune — relief LANG.
Runnable: pytest tests/test_break_volume_median_relief.py -v
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import entry_gates


def _gate_seg() -> str:
    src = inspect.getsource(entry_gates.pullback_break_confirmation)
    lo = src.index("break_volume_median_relief")
    hi = src.index('"break_low_volume"', lo)
    return src[src.rindex("if not _tick_break", 0, lo):hi]


def test_relief_runs_before_the_reject():
    src = inspect.getsource(entry_gates.pullback_break_confirmation)
    relief_at = src.index("break_volume_median_relief")
    reject_at = src.index('return False, "break_low_volume"')
    assert relief_at < reject_at


def test_relief_uses_median_denominator():
    seg = _gate_seg()
    assert ".median()" in seg
    assert "tail(21)" in seg


def test_relief_never_tightens():
    """Ang relief ay tumatakbo LANG kapag bagsak na sa mean-based floor, at ang
    median ratio ay dapat pumasa sa PAREHONG floor — hindi mas mababa."""
    seg = _gate_seg()
    assert "_robust_ratio >= _vol_floor" in seg
    # Ang relief block ay nasa loob ng parehong below-floor condition.
    assert seg.count("vol_ratio < _vol_floor") >= 1


def test_relief_fails_open():
    seg = _gate_seg()
    assert "except Exception" in seg
