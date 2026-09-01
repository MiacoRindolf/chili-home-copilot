"""Premarket RVOL stale guard (#1260) — SINUKAT 2026-09-01 10:20Z.

Ang provider snapshot ay nagbibigay ng ``day.v = 0`` para sa BAWAT pangalan sa
premarket (napatunayan sabay-sabay sa LABT/AEHL/FLYE/WETO), at ang rvol ay
``today_shares / prevDay.v`` — kaya ang buong premarket ay may rvol na
zero-o-halos-zero. Sa A-setup quality floor, ang maliit-pero-hindi-zero na
basa ay binibilang na "affirmatively low" ⇒ HARD REJECT: LABT 0.064 (totoo
350), AEHL 1.35 kahapon (totoo 146) — dalawang tunay na A-setup sa dalawang
magkasunod na araw.

Ang hukom ay ang SARILING TAPE: kung aktibong nagta-trade ang pangalan sa
``iqfeed_trade_ticks``, ang mababang provider rvol ay STALE FIELD, hindi
ebidensya ng katahimikan ⇒ ginagawang UNKNOWN (None) para dumaan sa umiiral
nang missing-rvol na landas (genuine-explosive + risk-bounded sizing).
HINDI ito nagtataas ng rvol kailanman.

Runnable: pytest tests/test_premarket_rvol_stale_guard.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text


def _tick(db, sym, *, shares, age_s):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.execute(text(
        "INSERT INTO iqfeed_trade_ticks "
        "(symbol, observed_at, price, size, source) "
        "VALUES (:s, :o, 1.0, :z, 'iqfeed_l1')"
    ), dict(s=sym, o=now - timedelta(seconds=age_s), z=float(shares)))
    db.commit()


def _resolve(db, sym, *, rvol):
    """Patakbuhin ang resolver na may pinilit na provider rvol sa signal."""
    from datetime import datetime as _dt
    from app.services.trading.momentum_neural.context import (
        build_momentum_regime_context,
    )
    from app.services.trading.momentum_neural.features import (
        ExecutionReadinessFeatures,
    )
    from app.services.trading.momentum_neural.variants import get_family
    from app.services.trading.momentum_neural.viability import (
        ViabilitySettingsProjection,
        _resolve_viability_external_inputs,
    )
    from app.config import settings as _s

    fam = get_family("impulse_breakout")
    feats = ExecutionReadinessFeatures(
        spread_bps=50.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08,
        meta={"ross_signals": {sym: {
            "ticker": sym, "rvol": rvol, "daily_change_pct": 40.0,
            "change_pct": 40.0, "float_shares": 1_540_000, "price": 3.0,
        }}},
    )
    ctx = build_momentum_regime_context(
        now=_dt(2026, 9, 1, 10, 20, 0, tzinfo=timezone.utc), atr_pct=0.05,
        meta={"spread_regime": "normal"},
    )
    proj = ViabilitySettingsProjection.from_runtime(_s)
    return _resolve_viability_external_inputs(
        sym, fam, ctx, feats, db=db, settings_projection=proj,
    )


def test_low_rvol_with_active_own_tape_becomes_unknown(db):
    """LABT-class: provider 0.064 pero buhay ang tape ⇒ rvol = None."""
    _tick(db, "LABTX", shares=60_000, age_s=120)
    out = _resolve(db, "LABTX", rvol=0.064)
    assert out.ross_rvol is None


def test_low_rvol_with_quiet_tape_stays_low(db):
    """Tunay na tahimik na pangalan: nananatiling affirmatively-low (reject)."""
    _tick(db, "QUIETX", shares=100, age_s=120)
    out = _resolve(db, "QUIETX", rvol=0.064)
    assert out.ross_rvol == 0.064


def test_no_tape_at_all_stays_low(db):
    out = _resolve(db, "NOTAPEX", rvol=0.5)
    assert out.ross_rvol == 0.5


def test_stale_tape_outside_window_stays_low(db):
    """Volume 2 oras na ang nakaraan ⇒ hindi patunay ng kasalukuyang buhay."""
    _tick(db, "OLDX", shares=90_000, age_s=7200)
    out = _resolve(db, "OLDX", rvol=0.064)
    assert out.ross_rvol == 0.064


def test_healthy_rvol_untouched(db):
    """Hindi kailanman ginagalaw ang rvol na nasa itaas ng floor."""
    _tick(db, "HOTX", shares=500_000, age_s=60)
    out = _resolve(db, "HOTX", rvol=42.0)
    assert out.ross_rvol == 42.0


def test_guard_never_raises_rvol(db):
    """Ang guard ay nag-aalis lamang ng maling low — walang bagong mataas."""
    _tick(db, "RAISEX", shares=500_000, age_s=60)
    out = _resolve(db, "RAISEX", rvol=1.0)
    assert out.ross_rvol is None  # unknown, HINDI isang mataas na numero
