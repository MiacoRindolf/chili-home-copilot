"""ANG ILLR ACCEPTANCE TEST — ang `halt_resume_dip` ay dapat tumanggi sa flush at
pumutok sa reclaim.

BAKIT ITO UMIIRAL. Ang `halt_resume_dip_trigger` ay **0 sa 521 candidate / 30 araw**.
Inayos ng #1127/#1128 ang lifecycle (napatunayan: `chain=1`, `chain=2` sa
produksyon; wala na ang flip-flop) pero hindi pa ito pumuputok kahit minsan.

ANG BALAK NA ACCEPTANCE TEST AY HINDI RUNNABLE. Ang orihinal na plano ay i-replay
ang **ILLR 2026-06-25** (`A7Gnw1CMExI::ILLR::2026-06-25::multiwave-spacex`).
Sinuri 2026-08-24 at **hindi ito kayang patakbuhin**, dahil sa dalawang
independiyenteng harang::

    halt windows -> momentum_nbbo_spread_tape  (nagsisimula lang sa 07-25;
                                                ILLR sa 06-25 = 0 row)
    bars         -> fetch_ohlcv_df(period="5d") (5 ARAW mula ngayon;
                                                hindi maaabot ang Hunyo)

Kaya ang KONTRATA ang sinusubok dito, hindi ang tape. Iyon naman ang tunay na
tanong ng acceptance: *tumatanggi ba ito sa unstabilized na flush at pumuputok sa
stabilized na reclaim?*

ANG DALAWANG BINTI NI ROSS SA ILLR:

  FLUSH   -- ang resume ay bumabagsak sa bagong low. DITO SIYA NA-STOP OUT.
             Ang trigger ay dapat TUMANGGI: `resume_dip_no_reclaim`.
  RECLAIM -- humahawak ang dip at nagsasara nang malakas sa itaas ng nakaraang
             high. ANG +R LEG NIYA. Ang trigger ay dapat PUMUTOK.

⚠️ ISANG DOKTRINAL NA DETALYE NA NADISKUBRE SA PAGSULAT NITO. Ang reclaim bar ay
HINDI dapat gumawa ng bagong high sa itaas ng post-resume reference high. Kung
gagawa ito, ang `ref_pos = high.values.argmax()` ay tuturo sa HULING bar at ang
trigger ay magbabalik ng `resume_dip_forming` -- "pumapataas pa, walang dip".
Tama iyon: ang dip-at-reclaim ay ibang hugis sa isang tuluy-tuloy na pag-akyat.

Runnable: pytest tests/test_halt_resume_dip_acceptance.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import halt_resume_dip_trigger

_START = datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc)
_RESUME = _START + timedelta(minutes=20)

# Tahimik na warmup para may ATR na masusukat ang trigger.
_WARM = [[10.0, 10.15, 9.85, 10.0, 100_000] for _ in range(20)]

# Ang post-resume na pag-akyat: reference high = 11.00
_POP = [10.0, 11.00, 9.95, 10.90, 400_000]
# Ang unang dip: low = 10.20 (lalim 7.3%, nasa loob ng ATR-scaled na banda)
_DIP = [10.90, 10.95, 10.20, 10.30, 300_000]

# Binti 1 -- ang flush: bagong low sa ilalim ng dip. Dito na-stop out si Ross.
_FLUSH = [10.30, 10.40, 9.90, 10.00, 300_000]
# Binti 2 -- ang reclaim: humahawak sa itaas ng dip low, nagsasara sa itaas ng
# high ng nakaraang bar, at HINDI lumalampas sa reference high.
_RECLAIM = [10.30, 10.99, 10.25, 10.97, 350_000]


def _frame(rows: list[list[float]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([_START + timedelta(minutes=i) for i in range(len(rows))])
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


def _fire(rows: list[list[float]], *, now=None):
    df = _frame(rows)
    return halt_resume_dip_trigger(
        df, entry_interval="1m", halt_resumed_at_utc=_RESUME, now=now or df.index[-1]
    )


def test_the_unstabilized_flush_is_DECLINED():
    """ANG UNANG BINTI. Isang bagong low sa ilalim ng dip = walang stabilization.
    Dito eksaktong na-stop out si Ross; ang trigger ay dapat tumanggi."""
    ok, reason, _dbg = _fire(_WARM + [_POP, _DIP, _FLUSH])
    assert ok is False
    assert reason == "resume_dip_no_reclaim"


def test_the_stabilized_reclaim_FIRES():
    """ANG PANGALAWANG BINTI, AT ANG BUONG PUNTO. Humahawak ang dip at nagsasara
    nang malakas sa itaas ng nakaraang high — ang +R leg ni Ross."""
    ok, reason, dbg = _fire(_WARM + [_POP, _DIP, _RECLAIM])
    assert ok is True, f"tinanggihan ang reclaim: {reason}"
    assert reason == "halt_resume_dip_ok"
    # ang debug ay dapat magdala ng geometry na ginagamit ng sizing at stops
    assert dbg["pullback_low"] == pytest.approx(10.20)
    assert dbg["pullback_high"] == pytest.approx(11.00)


def test_the_dip_depth_lands_inside_the_adaptive_band():
    """Ang lalim ay dapat nasa pagitan ng ATR-scaled na noise floor at deep cap --
    hindi jitter, hindi pagbagsak. Ang mga hangganan ay hango sa instrumento."""
    _ok, _reason, dbg = _fire(_WARM + [_POP, _DIP, _RECLAIM])
    depth = dbg["dip_depth_pct"]
    assert dbg["noise_floor_pct"] < depth < dbg["deep_cap_pct"]


def test_a_reclaim_that_makes_a_NEW_HIGH_is_still_forming_not_an_entry():
    """⚠️ ANG DOKTRINAL NA DETALYE. Ang paglampas sa reference high ay hindi
    dip-at-reclaim — tuluy-tuloy na pag-akyat iyon, at ang `ref_pos` ay lilipat sa
    huling bar. Ang trigger ay tama sa pagtawag ditong `forming`."""
    new_high = [10.30, 11.20, 10.25, 11.15, 350_000]
    ok, reason, _dbg = _fire(_WARM + [_POP, _DIP, new_high])
    assert ok is False
    assert reason == "resume_dip_forming"


def test_a_still_pumping_tape_has_no_dip_yet():
    """Wala pang dip pagkatapos ng resume — walang ibibigay na entry."""
    ok, reason, _dbg = _fire(_WARM + [_POP, [10.90, 11.40, 10.85, 11.35, 400_000]])
    assert ok is False
    assert reason == "resume_dip_forming"


def test_too_few_post_resume_bars_declines():
    ok, reason, _dbg = _fire(_WARM + [_POP])
    assert ok is False
    assert reason in {"resume_dip_insufficient_bars", "resume_dip_forming"}


def test_past_the_recency_window_the_normal_ladder_owns_the_tape():
    """RECENCY. Lampas sa `halt_resume_dip_window_seconds` ay hindi na ito
    post-resume na entry — pag-aari na iyon ng karaniwang trigger ladder."""
    rows = _WARM + [_POP, _DIP, _RECLAIM]
    late = _RESUME + timedelta(hours=3)
    ok, reason, _dbg = _fire(rows, now=late)
    assert ok is False
    assert reason == "resume_dip_window_passed"


def test_a_bad_resume_timestamp_declines_cleanly():
    """Hindi dapat sumabog sa basurang input — dapat tumanggi nang may dahilan."""
    df = _frame(_WARM + [_POP, _DIP, _RECLAIM])
    ok, reason, _dbg = halt_resume_dip_trigger(
        df, entry_interval="1m", halt_resumed_at_utc="hindi-petsa", now=df.index[-1]
    )
    assert ok is False
    assert reason == "resume_dip_bad_resume_ts"
