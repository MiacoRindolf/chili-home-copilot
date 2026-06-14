"""B1 — premarket tick-break confirmation (the CUPR false-pop guard).

Deterministic unit proof of ``_premarket_tickbreak_confirmed`` on CUPR's REAL
06/12 numbers. CUPR exploded 2.95→7.80; CHILI's sim entered 4.07 on a premarket
FAILED pop (a tick poking 1¢ through the 4.04 pullback high), was stopped −15% in
the 3.2↔4.5 chop, THEN the name ran +92%. B1 must:
  * REJECT that 09:14 premarket wick (4.07 does not clear the ATR thrust buffer);
  * ACCEPT a real premarket THRUST (4.49 clears it);
  * be a NO-OP in RTH (the 09:31 breakout enters via the normal path), for crypto,
    and when the flag is off — byte-unchanged there;
  * fail OPEN on missing volatility (never block on thin data).

The full-replay PnL reproduction is config-fragile (selection depends on ~350
deployed knobs); this tests the MECHANISM directly, which is the robust proof.
"""
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import _premarket_tickbreak_confirmed

# CUPR 06/12 reality: pullback_high (level) ~4.04, the false-pop wick to 4.07,
# the real thrust to 4.49, and a high premarket ATR (the name swung 2.95↔4.52).
LEVEL = 4.04
WICK = 4.07     # 0.7% over the level — the false pop
THRUST = 4.49   # 11% over the level — the real break
ATR_PCT = 0.15  # premarket vol; buffer = 4.04 * 0.15 * 0.10 = ~0.06 => clears at ~4.10


def _utc(h, m):
    return datetime(2026, 6, 12, h, m, tzinfo=timezone.utc)


# 09:14 ET = 13:14 UTC (premarket, < 09:30); 09:20 ET premarket; 09:31 ET = RTH.
PRE_0914 = _utc(13, 14)
PRE_0920 = _utc(13, 20)
RTH_0931 = _utc(13, 31)


@pytest.fixture(autouse=True)
def _knobs():
    old_c = settings.chili_momentum_premarket_tickbreak_confirm
    old_m = settings.chili_momentum_premarket_tickbreak_atr_mult
    old_p = settings.chili_momentum_premarket_start_et
    settings.chili_momentum_premarket_tickbreak_confirm = True
    settings.chili_momentum_premarket_tickbreak_atr_mult = 0.10
    settings.chili_momentum_premarket_start_et = "04:00"
    yield
    settings.chili_momentum_premarket_tickbreak_confirm = old_c
    settings.chili_momentum_premarket_tickbreak_atr_mult = old_m
    settings.chili_momentum_premarket_start_et = old_p


def _confirm(live_price, *, symbol="CUPR", now, atr_pct=ATR_PCT):
    return _premarket_tickbreak_confirmed(
        live_price=live_price, level=LEVEL, atr_pct=atr_pct, symbol=symbol, now=now)


# ── the CUPR fix ─────────────────────────────────────────────────────────────

def test_premarket_false_pop_rejected():
    """09:14 premarket wick (4.07, 0.7% over 4.04) does NOT clear the ATR buffer."""
    assert _confirm(WICK, now=PRE_0914) is False


def test_premarket_real_thrust_accepted():
    """A real premarket thrust (4.49, 11% over) clears the buffer => fires."""
    assert _confirm(THRUST, now=PRE_0920) is True


# ── no-op everywhere it must not touch ──────────────────────────────────────

def test_rth_is_noop_even_for_the_wick():
    """In RTH (09:31, the real breakout) B1 is a no-op — the wick price is accepted
    (the existing RTH tick-break is byte-unchanged; the 09:31 entry is via it)."""
    assert _confirm(WICK, now=RTH_0931) is True


def test_crypto_is_noop():
    """Crypto is 24/7 'regular' => never premarket => always accepted (unchanged)."""
    assert _confirm(WICK, symbol="BTC-USD", now=PRE_0914) is True


def test_flag_off_is_old_behavior():
    """Kill-switch off => fire on any poke (the pre-B1 behavior)."""
    settings.chili_momentum_premarket_tickbreak_confirm = False
    assert _confirm(WICK, now=PRE_0914) is True


def test_missing_atr_fails_open():
    """No volatility read => fail-open (never block an entry on thin data)."""
    assert _confirm(WICK, now=PRE_0914, atr_pct=None) is True


def test_buffer_scales_with_atr():
    """The buffer is ATR-relative: a LOWER ATR lets a smaller poke through; a HIGHER
    ATR demands a bigger thrust (the adaptive single-knob, no fixed cents)."""
    # low premarket ATR (3%): buffer ~ 4.04*1.003 = 4.052 => the 4.07 wick clears it
    assert _confirm(WICK, now=PRE_0914, atr_pct=0.03) is True
    # high ATR (30%): buffer ~ 4.04*1.03 = 4.16 => even 4.10 is rejected
    assert _confirm(4.10, now=PRE_0914, atr_pct=0.30) is False
