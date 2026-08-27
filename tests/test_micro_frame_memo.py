"""Apat na site ang nagre-rebuild ng parehong micro frame kada tick.

ANG SUKAT (2026-08-27, tick-first restructure audit). Ang isang full rebuild ng
micro-bar frame sa dense tape ay 3,554ms (44,317 row, 134MB laban sa dating
128MB na shared_buffers -- HINDI KAILANMAN umiinit). Apat na call site kada
pre-entry tick. Ang L8b (2026-08-02, py-spy sa JEM replay) ay sumukat na ng
28.7% ng runtime sa parehong rebuild at nilagyan ng memo ANG ISANG tumatawag
(``_latest_rvol``) -- ang tatlong iba ay hindi kailanman nakisama.

ANG LUNAS: parehong kombensyon, ibinaba sa ``_build_micro_bar_df`` mismo --
sim-anchored clock bucket (replay-correct), None kine-cache, at KOPYA sa hit
para ang mutation ng isang consumer ay hindi makahawa sa iba.

Runnable: pytest tests/test_micro_frame_memo.py -v
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with LR._MICRO_FRAME_MEMO_LOCK:
        LR._MICRO_FRAME_MEMO.clear()
    yield
    with LR._MICRO_FRAME_MEMO_LOCK:
        LR._MICRO_FRAME_MEMO.clear()


def _frame(n=12):
    idx = pd.date_range("2026-08-27 13:30", periods=n, freq="10s", tz="UTC")
    return pd.DataFrame({
        "Open": [1.0] * n, "High": [1.1] * n, "Low": [0.9] * n,
        "Close": [1.05] * n, "Volume": [100.0] * n,
    }, index=idx)


def _count_builds(monkeypatch, results):
    calls = []

    def _fake(db, symbol, *, bar_seconds, lookback_minutes=30.0):
        calls.append(symbol)
        return results.pop(0) if results else None

    monkeypatch.setattr(LR, "_micro_bar_df_from_session", _fake, raising=True)
    return calls


def test_four_consumers_one_build(monkeypatch):
    """ANG PANGUNAHING KASO. Ang apat na tawag sa loob ng iisang bucket ay
    IISANG build lang."""
    calls = _count_builds(monkeypatch, [_frame()])
    outs = [LR._build_micro_bar_df(object(), "XPON", bar_seconds=10) for _ in range(4)]
    assert len(calls) == 1, "isang build lang ang inaasahan, nakita: %d" % len(calls)
    assert all(o is not None and len(o) == 12 for o in outs)


def test_a_hit_returns_a_COPY_not_the_cached_object(monkeypatch):
    """⚠️ ANG BANTAY LABAN SA CROSS-CONSUMER MUTATION. Kapag binago ng unang
    consumer ang frame, ang pangalawa ay dapat makakita ng malinis."""
    _count_builds(monkeypatch, [_frame()])
    a = LR._build_micro_bar_df(object(), "XPON", bar_seconds=10)
    a.loc[a.index[0], "Close"] = 999.0
    b = LR._build_micro_bar_df(object(), "XPON", bar_seconds=10)
    assert b is not a
    assert float(b["Close"].iloc[0]) != 999.0, "nahawa ng mutation ang cache"


def test_none_is_cached_too(monkeypatch):
    """Ang manipis na pangalan ay hindi dapat paulit-ulit na i-query sa loob ng
    bucket -- parehong panuntunan ng _latest_rvol."""
    calls = _count_builds(monkeypatch, [None])
    for _ in range(3):
        assert LR._build_micro_bar_df(object(), "THIN", bar_seconds=10) is None
    assert len(calls) == 1


def test_different_symbols_do_not_share(monkeypatch):
    calls = _count_builds(monkeypatch, [_frame(), _frame(8)])
    a = LR._build_micro_bar_df(object(), "AAA", bar_seconds=10)
    b = LR._build_micro_bar_df(object(), "BBB", bar_seconds=10)
    assert len(calls) == 2
    assert len(a) == 12 and len(b) == 8


def test_different_bar_seconds_do_not_share(monkeypatch):
    calls = _count_builds(monkeypatch, [_frame(), _frame()])
    LR._build_micro_bar_df(object(), "AAA", bar_seconds=10)
    LR._build_micro_bar_df(object(), "AAA", bar_seconds=15)
    assert len(calls) == 2


def test_zero_disables_the_memo(monkeypatch):
    """0 ⇒ byte-identical legacy — bawat tawag ay build."""
    monkeypatch.setattr(
        settings, "chili_momentum_micro_frame_memo_seconds", 0.0, raising=False)
    calls = _count_builds(monkeypatch, [_frame(), _frame(), _frame()])
    for _ in range(3):
        LR._build_micro_bar_df(object(), "XPON", bar_seconds=10)
    assert len(calls) == 3


def test_a_build_error_still_falls_through_untouched(monkeypatch):
    """⚠️ Ang error path (F1/F2 retry) ay hindi dapat magalaw ng memo — ang
    exception ay dapat pa ring umabot sa umiiral na handler."""
    def _boom(db, symbol, *, bar_seconds, lookback_minutes=30.0):
        raise RuntimeError("tape read failed")

    monkeypatch.setattr(LR, "_micro_bar_df_from_session", _boom, raising=True)
    monkeypatch.setattr(
        settings, "chili_momentum_micro_fallback_1m_from_ticks_enabled", False,
        raising=False)
    meta: dict = {}
    out = LR._build_micro_bar_df(object(), "ERR", bar_seconds=10, meta=meta)
    assert out is None
    assert "micro_error_detail" in meta, "dapat buo pa rin ang F1 na error surfacing"


def test_the_flag_exists_with_the_convention():
    fields = type(settings).model_fields
    name = "chili_momentum_micro_frame_memo_seconds"
    assert name in fields
    assert float(getattr(settings, name)) == 1.0
    desc = str(fields[name].description or "")
    assert "2026-08-27" in desc and "3.5s" in desc or "3,5" in desc or "3.5" in desc
