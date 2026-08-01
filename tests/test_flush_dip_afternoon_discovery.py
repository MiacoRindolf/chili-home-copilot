"""L6 (2026-07-31): flush-dip afternoon DISCOVERY window — fresh-HOD test.

Scorecard v2 finding: ang morning-only gate ay PROXY lang ng discovery phase —
binench nito ang mga hapon-igniter na monster (JLHL +393%, JEM +224%; top
reject = flush_dip_past_morning_window) habang ang tunay na pinoprotektahan
nito (JZXN/SPHL/GCDT/DBGI midday fades) ay lahat STALE ang session HOD. Ang L6
ay direct discovery test: past the morning cutoff, tuloy ang flush-dip
evaluation KUNG ang session HOD ay naka-print sa loob ng
``chili_momentum_flush_dip_fresh_hod_minutes`` (base 30, verbatim binding);
stale HOD ⇒ nananatili ang morning reject. Kill-switch
``chili_momentum_flush_dip_fresh_hod_afternoon_enabled`` (OFF ⇒ legacy
morning-only, byte-identical).

Fixtures mula sa tests/test_momentum_mock_fire_reversal.py (canonical flush-dip
geometry); ang afternoon frames ay nire-reindex sa DatetimeIndex para kontrolado
ang HOD bar age (1-min spacing = sariwa; 30-min spacing = lipas).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from app.services.trading.momentum_neural.entry_gates import flush_dip_buy_confirmation
from tests.test_momentum_mock_fire_reversal import (
    _CANDLES,
    _FLUSH_NOW,
    _GATES,
    _flush_arrays,
    _flush_dip_df,
    _flush_settings,
)

# 4 oras pagkatapos ng frozen 10:00-ET morning clock = 14:00 ET (lampas sa
# 10:30 morning cutoff, nasa loob pa rin ng RTH).
_AFTERNOON_NOW = pd.Timestamp(_FLUSH_NOW) + pd.Timedelta(hours=4)


def _afternoon_df(bar_spacing_minutes: int) -> pd.DataFrame:
    """Canonical flush-dip frame na may DatetimeIndex na nagtatapos sa NOW.

    Ang HOD ng fixture ay nasa unang bahagi ng frame (run-up bago ang flush),
    kaya ang bar spacing ang nagdidikta ng HOD age: 1-min bars ⇒ HOD ~ilang
    minuto lang ang tanda (sariwa); 30-min bars ⇒ HOD oras-oras nang lipas.
    """
    df = _flush_dip_df()
    end = pd.Timestamp(_AFTERNOON_NOW)
    idx = pd.date_range(
        end=end, periods=len(df), freq=f"{bar_spacing_minutes}min"
    )
    df.index = idx
    return df


def _fire(ms, df, *, now, afternoon_on=True, fresh_minutes=30.0):
    _flush_settings(ms)
    ms.chili_momentum_flush_dip_volume_gate_enabled = True
    ms.chili_momentum_flush_dip_fresh_hod_afternoon_enabled = afternoon_on
    ms.chili_momentum_flush_dip_fresh_hod_minutes = fresh_minutes
    with patch(f"{_GATES}.compute_all_from_df", return_value=_flush_arrays(len(df))), \
            patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
        return flush_dip_buy_confirmation(
            df, entry_interval="1m", symbol="TEST", db=MagicMock(), now=now,
        )


def test_afternoon_fresh_hod_fires():
    # 1-min bars: ang HOD bar ay ~ilang minuto lang ang tanda ⇒ discovery pa ⇒
    # lumalagpas sa morning gate at ganap na pumuputok ang canonical curl.
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, dbg = _fire(ms, _afternoon_df(1), now=_AFTERNOON_NOW)
    assert ok is True, f"fresh-HOD afternoon dapat pumutok, got {reason} dbg={dbg}"
    assert reason == "flush_dip_buy"


def test_afternoon_stale_hod_keeps_morning_reject():
    # 30-min bars: HOD ~5+ oras nang lipas ⇒ hindi discovery ⇒ nananatili ang
    # morning reject (ang JZXN/SPHL midday-fade class).
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, dbg = _fire(ms, _afternoon_df(30), now=_AFTERNOON_NOW)
    assert ok is False
    assert reason == "flush_dip_past_morning_window"
    assert dbg.get("mins_since_hod") is not None
    assert dbg["mins_since_hod"] > 30.0


def test_flag_off_restores_legacy_morning_only():
    # Sariwang HOD pero OFF ang L6 flag ⇒ legacy morning-only reject.
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, _ = _fire(
            ms, _afternoon_df(1), now=_AFTERNOON_NOW, afternoon_on=False,
        )
    assert ok is False
    assert reason == "flush_dip_past_morning_window"


def test_window_zero_binds_verbatim():
    # 0 minuto = sariwa lang kung ang HOD ang kasalukuyang bar — ang HOD na
    # ~10 minuto ang tanda ay reject (pinipin ang `or`-swallow-zero na klase).
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, _ = _fire(
            ms, _afternoon_df(1), now=_AFTERNOON_NOW, fresh_minutes=0.0,
        )
    assert ok is False
    assert reason == "flush_dip_past_morning_window"


def test_morning_path_unchanged():
    # Sa loob ng morning window, hindi kinokonsulta ang L6 — pumuputok gaya ng dati.
    with patch(f"{_GATES}.settings") as ms:
        ok, reason, _ = _fire(ms, _flush_dip_df(), now=_FLUSH_NOW)
    assert ok is True
    assert reason == "flush_dip_buy"
