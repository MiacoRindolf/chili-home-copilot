"""Reap-cooldown leader exemption (2026-08-17, IVF premarket starvation).

Live evidence: IVF (+190%, RV ~11.7k, ang SOLONG 5-Pillars name at board #1) ay
na-reap ng 1800s watch clock, tapos ang reap cooldown (300–1800s) ang humarang sa
re-arm sa BAWAT pass — habang mas mahihinang incumbent (FIEE/JLHL/CRGO) ang may
hawak ng slots. Ang symbol-of-the-day guarantee (hoist + displacement victim-veto)
ay kulang ng ikatlong binti: cooldown immunity para sa board #1.

Ang fix: ``_reap_cooldown_blocks(sym, now, exempt_sym=board_top)`` — ang #1 ay
hindi kailanman kina-cooldown-skip; lahat ng iba ay nananatili sa damper.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.services.trading.momentum_neural.auto_arm as AA


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clear_cooldowns():
    AA._REAP_COOLDOWN.clear()
    AA._REAP_OSCILLATION.clear()


def test_active_cooldown_blocks_non_leader():
    _clear_cooldowns()
    now = _now()
    AA._write_reap_cooldown("FIEE", now)
    assert AA._reap_cooldown_blocks("FIEE", now, exempt_sym="IVF") is True


def test_active_cooldown_does_not_block_board_leader():
    """Ang IVF case mismo: naka-cooldown pero siya ang board #1 -> hindi haharangin."""
    _clear_cooldowns()
    now = _now()
    AA._write_reap_cooldown("IVF", now)
    assert AA._reap_cooldown_active("IVF", now) is True
    assert AA._reap_cooldown_blocks("IVF", now, exempt_sym="IVF") is False


def test_no_cooldown_never_blocks():
    _clear_cooldowns()
    now = _now()
    assert AA._reap_cooldown_blocks("IVF", now, exempt_sym="") is False
    assert AA._reap_cooldown_blocks("IVF", now, exempt_sym="SLE") is False


def test_empty_exempt_sym_keeps_damper():
    """Walang kilalang #1 (empty exempt) -> ang damper ay buo pa rin."""
    _clear_cooldowns()
    now = _now()
    AA._write_reap_cooldown("TRUG", now)
    assert AA._reap_cooldown_blocks("TRUG", now, exempt_sym="") is True


def test_expired_cooldown_does_not_block():
    _clear_cooldowns()
    base = float(AA._reap_cooldown_seconds("MYSZ", _now()) or 300.0)
    stale = _now() - timedelta(seconds=base + 60.0)
    AA._write_reap_cooldown("MYSZ", stale)
    # ang oscillation bump sa stale stamp ay maaaring magpahaba; i-reset para
    # ang test ay tumingin lang sa expiry semantics ng plain base window
    AA._REAP_OSCILLATION.clear()
    now = _now()
    assert AA._reap_cooldown_active("MYSZ", now) is False
    assert AA._reap_cooldown_blocks("MYSZ", now, exempt_sym="IVF") is False


def test_exemption_is_exact_symbol_match():
    """Ang exemption ay para LANG sa eksaktong #1 — hindi prefix/fuzzy."""
    _clear_cooldowns()
    now = _now()
    AA._write_reap_cooldown("IVFB", now)
    assert AA._reap_cooldown_blocks("IVFB", now, exempt_sym="IVF") is True
