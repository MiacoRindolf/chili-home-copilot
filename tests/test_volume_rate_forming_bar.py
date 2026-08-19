"""FORMING-BAR RATE NORMALISATION — ang YJ measurement bug (2026-08-19).

Ang volume gate ay kinukumpara ang HULING bar laban sa 20 KUMPLETONG bar bago
nito — pero ang huling bar ay karaniwang BUMUBUO pa. Ang bar na 27 segundo pa
lang sa loob ng minuto nito ay may ~45% ng volume na matatapos niya. Kaya ang
paghahambing ay sistematikong nagmamaliit ng volume nang eksakto sa bahaging
natitira — at pinakamalala ito sa SIMULA ng bar, kung saan mismo pumuputok ang
momentum entry.

NAPATUNAYAN sa naitalang IQFeed tape para sa YJ (ang simbolong kinitaan ni Ross
habang tumatanggi ang CHILI nang tatlong beses):

    bar     buong-bar ratio   nalipas   nakita ng gate
    09:10       2.60x          27/60        1.17x  -> tanggi
    09:12       1.51x          36/60        0.91x  -> tanggi
    09:07       0.45x          43/60        0.32x  -> tanggi

Tumutugma ang LAHAT ng tatlong naka-log na pagtanggi. At nasa tape ang volume:
ang 09:11 lang ay 3,346,414 share sa 6.36x.

⚠️ HINDI ito pagluluwag ng 1.5x na floor. Nananatili ang floor. Ang inaalis ay
ang paglalapat ng floor sa bar na kalahati pa lang nasusukat.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import (
    _forming_bar_elapsed_fraction,
    momentum_volume_confirmation,
)


def _frame(*, last_bar_volume: float, avg_volume: float, n: int = 30,
           last_start: datetime, price: float = 10.0, rising: bool = True):
    """1-minutong frame: n-1 kumpletong bar sa avg_volume, tapos ang bumubuong bar."""
    idx = [last_start - timedelta(minutes=(n - 1 - i)) for i in range(n)]
    vols = [avg_volume] * (n - 1) + [last_bar_volume]
    # Tumataas na presyo para malinis na nasa ITAAS ng EMA-9 ang huli.
    closes = [price * (0.90 + 0.004 * i) for i in range(n)] if rising else [price] * n
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": vols},
        index=pd.DatetimeIndex(idx),
    )


# ─────────────────────── ang elapsed-fraction helper ───────────────────────


def test_elapsed_fraction_mid_bar():
    now = datetime.utcnow()
    df = _frame(last_bar_volume=1.0, avg_volume=1.0, last_start=now - timedelta(seconds=27))
    f = _forming_bar_elapsed_fraction(df)
    assert f is not None
    assert 0.40 < f < 0.50, f  # 27/60


def test_elapsed_fraction_none_when_bar_complete():
    now = datetime.utcnow()
    df = _frame(last_bar_volume=1.0, avg_volume=1.0, last_start=now - timedelta(seconds=95))
    assert _forming_bar_elapsed_fraction(df) is None


def test_elapsed_fraction_none_on_short_frame():
    now = datetime.utcnow()
    df = _frame(last_bar_volume=1.0, avg_volume=1.0, n=2, last_start=now - timedelta(seconds=20))
    assert _forming_bar_elapsed_fraction(df) is None


# ─────────────────── ang TUNAY na sandali ng YJ (09:10:27) ───────────────────


def test_yj_0910_admits_after_normalisation(monkeypatch):
    """Tunay na numero: ang 09:10 bar ay 1,162,018 laban sa 446,930 na average
    (2.60x sa buong bar). Sa 27s, ang partial ay 522,908 = 1.17x -> DATING
    TANGGI. Ang rate-normalised ay 2.60x -> DAPAT PUMASA."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_normalization_enabled", True,
        raising=False,
    )
    now = datetime.utcnow()
    partial = 1_162_018 * (27.0 / 60.0)
    df = _frame(last_bar_volume=partial, avg_volume=446_930,
                last_start=now - timedelta(seconds=27))
    ok, reason = momentum_volume_confirmation(df)
    assert ok is True, reason
    assert reason.endswith("_rate"), reason


def test_yj_0910_still_refused_with_flag_off(monkeypatch):
    """PARITY: naka-OFF ang flag -> ang dating pag-uugali (tanggi) ay bumabalik."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_normalization_enabled", False,
        raising=False,
    )
    now = datetime.utcnow()
    partial = 1_162_018 * (27.0 / 60.0)
    df = _frame(last_bar_volume=partial, avg_volume=446_930,
                last_start=now - timedelta(seconds=27))
    ok, reason = momentum_volume_confirmation(df)
    assert ok is False
    assert reason == "volume_below_1p5x_avg", reason


# ───────────────── ang floor mismo ay HINDI niluluwagan ─────────────────


def test_genuinely_thin_bar_is_still_refused(monkeypatch):
    """ANG MAHALAGA: ang tunay na manipis na bar ay tinatanggihan PA RIN.
    Buong-bar ratio 0.45x (ang 09:07 ng YJ) -> kahit i-normalise, 0.45x pa rin."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_normalization_enabled", True,
        raising=False,
    )
    now = datetime.utcnow()
    partial = 211_620 * (43.0 / 60.0)
    df = _frame(last_bar_volume=partial, avg_volume=470_000,
                last_start=now - timedelta(seconds=43))
    ok, reason = momentum_volume_confirmation(df)
    assert ok is False, reason
    assert reason == "volume_below_1p5x_avg"


def test_sliver_is_not_extrapolated(monkeypatch):
    """GUARD: dalawang print sa unang segundo ay hindi dapat maging malaking rate.
    Sa ilalim ng min_elapsed_fraction, ang hilaw na paghahambing ang namamayani."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_normalization_enabled", True,
        raising=False,
    )
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_min_elapsed_fraction", 0.25,
        raising=False,
    )
    now = datetime.utcnow()
    # 3 segundo lang (0.05 ng bar) na may maliit na volume na mag-e-extrapolate
    # sa 20x kung pinayagan.
    df = _frame(last_bar_volume=50_000, avg_volume=500_000,
                last_start=now - timedelta(seconds=3))
    ok, reason = momentum_volume_confirmation(df)
    assert ok is False, reason
    assert reason == "volume_below_1p5x_avg"


def test_complete_bar_path_is_unchanged(monkeypatch):
    """Ang kumpletong bar (walang forming fraction) ay dumadaan sa legacy path."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_normalization_enabled", True,
        raising=False,
    )
    now = datetime.utcnow()
    df = _frame(last_bar_volume=1_000_000, avg_volume=400_000,
                last_start=now - timedelta(seconds=95))
    ok, reason = momentum_volume_confirmation(df)
    assert ok is True, reason
    assert not reason.endswith("_rate"), reason


@pytest.mark.parametrize("elapsed_s,should_admit", [(20, True), (40, True), (55, True)])
def test_admits_consistently_across_the_bar(monkeypatch, elapsed_s, should_admit):
    """Ang KAPAREHONG tunay na bar ay dapat pare-pareho ang hatol saanmang
    bahagi ng minuto — iyon ang buong punto ng rate."""
    monkeypatch.setattr(
        settings, "chili_momentum_entry_volume_rate_normalization_enabled", True,
        raising=False,
    )
    now = datetime.utcnow()
    partial = 1_162_018 * (elapsed_s / 60.0)
    df = _frame(last_bar_volume=partial, avg_volume=446_930,
                last_start=now - timedelta(seconds=elapsed_s))
    ok, _ = momentum_volume_confirmation(df)
    assert ok is should_admit, elapsed_s
