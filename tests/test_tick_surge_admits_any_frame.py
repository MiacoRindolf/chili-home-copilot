"""Ang tick-speed na volume gate ay DEAD CODE sa buhay na landas.

ANG DEPEKTO (2026-08-27). Ang ``tick_stream_volume_confirmation`` -- isang 60s
dollar-volume surge + 10s print-rate surge na sinusukat MISMO sa
``iqfeed_trade_ticks`` laban sa 300s na self-baseline -- ay umiiral, ligtas,
replay-aware, at **naabot lamang mula sa cold-start branch**::

    if df is None or len(df) < 25:
        tick_stream_volume_confirmation(...)

Ang frame na ipinapasa ng buhay na entry path ay ``_entry_df``: isang
**15-MINUTONG** provider frame na kinuha para sa adaptive spread cap
(``live_runner.py:28268``) at ginamit ulit dito (``:29608``). Laging kumpleto
ang 5-araw na frame ng provider, kaya ``len(df) >= 25`` ay LAGING totoo at ang
tick path ay **patay na kodigo sa buhay na landas** -- kahit para sa simbolong
30 segundo lang ang sariling tape.

NASUKAT (2026-08-26), kasaysayan ng tape sa sandali ng ARM::

    XPON 166.9 min   DAIC 171.9   RDIB 133.2
    VCIG  13.8 min   MSS    1.9   CRE    0.5 min   <- 30 SEGUNDO

At ang ignition mismo, sa TAMANG granularity (XPON)::

    segundo    volume   presyo   ratio vs 20s
    13:46:51      990   7.5500      --
    13:46:52    7,470   7.5801    7.55x   <- natutuklasan DITO
    13:46:54    5,581   7.7057    1.71    <- ang tuktok, 2 SEGUNDO lang
    13:47:00              7.5793          <- naibalik na
    13:47:19              7.4750          <- mas mababa pa sa simula

**+1.65% sa DALAWANG SEGUNDO.** Walang bar frame ang makakakita niyan. Ang tape
ay nakakakita sa loob ng isang segundo -- at nandoon na ang tape: may **5.8 ORAS**
na naitalang tick ang XPON bago ang ignition. Hindi kakulangan ng datos; maling
pinagmulan.

⚠️ MAHIGPIT NA ADMIT-ONLY. Kinakalkula ang desisyon ng bar nang EKSAKTONG gaya
ng dati at ibinabalik nang buo kapag pumayag ito. Tumatakbo lang ang tick consult
PAGKATAPOS ng pagtanggi at ang kaya lang nitong gawin ay gawing pagpayag ang
pagtanggi. Hindi ito makakadagdag ng harang, hindi makakapagpalit ng pumasa na,
at hindi puputok sa manipis o hindi mabasang tape.

Runnable: pytest tests/test_tick_surge_admits_any_frame.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG

_SRC = pathlib.Path(EG.__file__)


def _full_frame(n=40, vol=1000.0, last_vol=1000.0, rising=True):
    """Isang MALALIM na frame (>= 25 bar) -- ang eksaktong hugis na dumadating sa
    buhay na landas, kaya hindi kailanman naaabot ang cold-start branch."""
    idx = pd.date_range("2026-08-26 09:00", periods=n, freq="15min", tz="UTC")
    close = [10.0 + (i * 0.01 if rising else -i * 0.01) for i in range(n)]
    vols = [vol] * (n - 1) + [last_vol]
    return pd.DataFrame({
        "Open": [c - 0.005 for c in close],
        "High": [c + 0.01 for c in close],
        "Low": [c - 0.01 for c in close],
        "Close": close,
        "Volume": vols,
    }, index=idx)


@pytest.fixture
def _no_tick(monkeypatch):
    """Tape na tumatanggi -- ang baseline."""
    monkeypatch.setattr(
        EG, "tick_stream_volume_confirmation",
        lambda *a, **k: (False, "tick_fallback_no_tape", {}), raising=True)


@pytest.fixture
def _tick_fires(monkeypatch):
    """Tape na nakakakita ng ignition."""
    monkeypatch.setattr(
        EG, "tick_stream_volume_confirmation",
        lambda *a, **k: (True, "tick_stream_volume_surge", {"ratio": 7.55}),
        raising=True)


# ── Ang depekto mismo ────────────────────────────────────────────────────────


def test_a_deep_frame_can_now_reach_the_tick_path(_tick_fires):
    """ANG PANGUNAHING KASO. Isang 40-bar na frame -- ang eksaktong hugis mula sa
    provider -- na tinatanggihan ng bar gate. Bago ang 2026-08-27 ay imposibleng
    marating nito ang tape."""
    df = _full_frame(last_vol=100.0)  # patay na volume => bar gate tumanggi
    ok, reason = EG.momentum_volume_confirmation(df, symbol="XPON", db=object())
    assert ok is True
    assert reason == "momentum_ok_tick_surge"


def test_without_the_fix_the_same_frame_is_refused(_no_tick):
    """Ang parehong frame, tape na walang nakikita => ang lumang sagot, buo."""
    df = _full_frame(last_vol=100.0)
    ok, reason = EG.momentum_volume_confirmation(df, symbol="XPON", db=object())
    assert ok is False
    assert reason in ("volume_below_1p5x_avg", "price_below_ema9")


# ── Ang direksyon ng kaligtasan ──────────────────────────────────────────────


def test_it_is_admit_only_a_passing_bar_gate_is_never_touched(monkeypatch):
    """⚠️ ANG PINAKAMAHALAGANG BANTAY. Kung pumayag ang bar gate ay HINDI dapat
    tinatawag ang tape kahit minsan -- ang lunas ay hindi kailanman makakadagdag
    ng harang at hindi kailanman makakapagpalit ng pumasa na."""
    called = []

    def _boom(*a, **k):
        called.append(1)
        return (False, "should_not_be_consulted", {})

    monkeypatch.setattr(EG, "tick_stream_volume_confirmation", _boom, raising=True)
    df = _full_frame(vol=1000.0, last_vol=9999.0)  # malakas na volume => pumasa
    ok, reason = EG.momentum_volume_confirmation(df, symbol="XPON", db=object())
    assert ok is True
    assert reason != "momentum_ok_tick_surge"
    assert called == [], "hindi dapat tinatanong ang tape kapag pumasa na ang bar"


def test_a_declining_tape_returns_the_ORIGINAL_bar_reason(_no_tick):
    """⚠️ Ang dahilan ng bar ay hindi dapat mapalitan ng dahilan ng tape -- ang
    ulat sa gabi ay nakasalalay sa `reason` na naglalarawan ng gate na tumanggi."""
    df = _full_frame(last_vol=100.0)
    _, reason = EG.momentum_volume_confirmation(df, symbol="XPON", db=object())
    assert not reason.startswith("tick_")


def test_a_raising_tape_helper_cannot_break_the_entry(monkeypatch):
    """⚠️ FAIL-CLOSED. Ang pagsabog sa loob ng tape consult ay dapat magbalik ng
    orihinal na desisyon ng bar, hindi magpalaganap."""
    def _raise(*a, **k):
        raise RuntimeError("tape down")

    monkeypatch.setattr(EG, "tick_stream_volume_confirmation", _raise, raising=True)
    df = _full_frame(last_vol=100.0)
    ok, reason = EG.momentum_volume_confirmation(df, symbol="XPON", db=object())
    assert ok is False
    assert not reason.startswith("tick_")


def test_insufficient_bars_does_not_query_the_tape_twice(monkeypatch):
    """Ang cold-start branch sa loob ay TINANONG NA ang tape. Ang pagtanong ulit
    ay dobleng query kada tick sa mismong simbolong pinakamanipis ang tape."""
    calls = []

    def _count(*a, **k):
        calls.append(1)
        return (False, "tick_fallback_no_tape", {})

    monkeypatch.setattr(EG, "tick_stream_volume_confirmation", _count, raising=True)
    ok, reason = EG.momentum_volume_confirmation(
        pd.DataFrame(), symbol="CRE", db=object())
    assert ok is False
    assert reason == "insufficient_bars"
    assert len(calls) == 1, "isang query lang, hindi dalawa"


# ── Ang knob ─────────────────────────────────────────────────────────────────


def test_the_fix_ships_ON_with_a_revert_knob():
    name = "chili_momentum_tick_surge_admits_any_frame"
    assert getattr(settings, name) is True
    fields = type(settings).model_fields
    assert fields[name].validation_alias is not None
    desc = str(fields[name].description or "")
    assert "2026-08-27" in desc
    assert "XPON" in desc and "13:46:52" in desc, (
        "dapat nakatala ang nasukat na kaso, hindi lang ang layunin")


def test_the_knob_reverts_it(monkeypatch, _tick_fires):
    monkeypatch.setattr(
        settings, "chili_momentum_tick_surge_admits_any_frame", False,
        raising=False)
    df = _full_frame(last_vol=100.0)
    ok, reason = EG.momentum_volume_confirmation(df, symbol="XPON", db=object())
    assert ok is False, "OFF => byte-identical sa lumang gawi"
    assert reason != "momentum_ok_tick_surge"


# ── Bantay sa istruktura ─────────────────────────────────────────────────────


def test_the_bar_logic_was_not_edited_only_wrapped():
    """⚠️ Ang lumang function ay dapat buo pa rin sa ilalim ng bagong pangalan.
    Ang pag-edit ng lohika ng bar habang idinadagdag ang tape ay gagawing
    imposibleng patunayan na admit-only ito."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    assert "_momentum_volume_confirmation_bars" in names
    assert "momentum_volume_confirmation" in names


def test_the_wrapper_returns_the_bar_result_on_every_path_but_one():
    """⚠️ Ang wrapper ay dapat may EKSAKTONG ISANG return na hindi nagmumula sa
    resulta ng bar. Ang pangalawa ay isang bagong paraan para makaapekto ang tape
    sa desisyon nang hindi napapansin."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "momentum_volume_confirmation"
    )
    novel = 0
    for n in ast.walk(fn):
        if not isinstance(n, ast.Return) or not isinstance(n.value, ast.Tuple):
            continue
        src = ast.unparse(n.value)
        if "_bar_ok" not in src:
            novel += 1
    assert novel == 1, (
        "inaasahang eksaktong isang bagong return (ang tick admit), nakita: %d" % novel)


def test_the_tick_helper_query_is_still_bounded():
    """⚠️ ANG PANUNTUNAN NG TAPE. Ang iqfeed_trade_ticks ay ~73 GB. Ang query na
    walang symbol filter AT time bound ay humaharang sa buhay na lane. Ngayong
    tatakbo ito para sa BAWAT simbolo at hindi lang sa cold-start, ang hangganan
    ay hindi na opsyonal."""
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "tick_stream_volume_confirmation"
    )
    body = ast.unparse(fn)
    assert "symbol = :s" in body, "kailangan ng symbol filter"
    assert "observed_at >" in body and "observed_at <=" in body, "kailangan ng time bound"
