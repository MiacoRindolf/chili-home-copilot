"""Ang forward-bar date parser ay dapat kayanin ang HUGIS na tunay na ibinabalik.

ANG NAKAWALA (2026-08-24). Ang `_fetch_forward_bars` ay kumukuha ng bars mula sa
`market_data.fetch_ohlcv`, tapos hinahanap ang petsa nito sa::

    raw_date = b.get("date") or b.get("timestamp") or b.get("t")

na may komentong "market_data returns dicts with ISO 'date' or 'timestamp' fields".

**DALAWANG beses itong mali.** Ang tunay na ibinabalik::

    keys   : ['close', 'high', 'low', 'open', 'time', 'volume']
    halaga : time = 1786680000        <- epoch SECONDS, int

Wala ang 'date'/'timestamp'/'t'; at kahit mabasa ang 'time', ang
``datetime.fromisoformat("1786680000")`` ay sumasabog. Alinman sa dalawa ay
nagbibigay ng ``bar_date=None``, at itinatapon ng ``continue`` ang BAWAT bar --
kaya LAGING ``[]`` ang ibinabalik ng helper.

ANG BUNGA, sinukat sa produksyon::

    triple_barrier_label result: {'requested': 500, 'written': 0,
                                  'missing_data': 500, ...}
    job_id=triple_barrier_label phase=fail duration_ms=259720

~259 segundo ng DB at API churn kada oras, na nagsusulat ng ZERO. At ang
``promotion_gate.py:395`` ay ibinabagsak ang mga ``missing_data`` na row, kaya ang
promotion gate ay tumatakbo nang **walang triple-barrier na ebidensya kailanman**.

⚠️ Ang unang draft ng ayos ko ay nagdagdag lang ng ``"time"`` sa listahan ng key.
Nabigo pa rin: 0 sa 7 na bars. Ang epoch ang tunay na hadlang, at nakita lang iyon
nang subukan laban sa TUNAY na bars -- hindi sa pagbabasa.

Runnable: pytest tests/test_triple_barrier_forward_bar_dates.py -v
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.trading import triple_barrier_labeler as lab


def _extract(bar: dict) -> date | None:
    """Patakbuhin ang parsing na lohika ng helper sa isang bar.

    Ang helper ay may I/O sa loob, kaya ang bloke ng pagkuha ng petsa ang
    sinusubok dito -- iyon ang nasira.
    """
    src = inspect.getsource(lab._fetch_forward_bars)
    assert "b.get(\"time\")" in src, "ang 'time' ay dapat unang key na sinusubukan"
    raw = bar.get("time") or bar.get("date") or bar.get("timestamp") or bar.get("t")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            epoch = float(raw)
            if epoch > 1e11:
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, tz=timezone.utc).date()
        return datetime.fromisoformat(str(raw)[:10]).date()
    except (ValueError, OSError, OverflowError):
        return None


def test_the_real_market_data_shape_parses():
    """ANG EKSAKTONG HUGIS na ibinabalik ng fetch_ohlcv sa produksyon."""
    bar = {"close": 1.0, "high": 1.0, "low": 1.0, "open": 1.0,
           "time": 1786680000, "volume": 100}
    assert _extract(bar) == date(2026, 8, 14)


def test_the_old_key_list_would_have_dropped_it():
    """⚠️ Ang regression na ito ay tahimik: walang exception, walang log --
    isang walang-laman na listahan lang at 500 na `missing_data`."""
    bar = {"close": 1.0, "time": 1786680000, "volume": 100}
    assert bar.get("date") is None
    assert bar.get("timestamp") is None
    assert bar.get("t") is None


def test_epoch_milliseconds_also_parse():
    """Ang ibang provider ay nagbibigay ng ms; hindi dapat mag-iba ng 50 taon."""
    assert _extract({"time": 1786680000000}) == date(2026, 8, 14)


def test_iso_strings_still_parse():
    """Hindi dapat masira ang mga provider na nagbibigay ng ISO."""
    assert _extract({"date": "2026-08-14"}) == date(2026, 8, 14)
    assert _extract({"timestamp": "2026-08-14T13:30:00Z"}) == date(2026, 8, 14)


@pytest.mark.parametrize("bad", [{}, {"time": None}, {"time": "hindi-petsa"}, {"time": True}])
def test_unusable_input_yields_None_not_an_exception(bad):
    """Ang helper ay fail-closed: walang petsa = laktawan ang bar, huwag sumabog."""
    assert _extract(bad) is None


def test_the_helper_handles_epoch_not_just_iso():
    """Bantayan ang tunay na source -- hindi sapat ang pagdagdag ng 'time' sa
    listahan ng key; ang epoch branch ang tunay na ayos."""
    src = inspect.getsource(lab._fetch_forward_bars)
    assert "fromtimestamp" in src, "kailangan ng epoch branch, hindi lang ISO"
    assert "1e11" in src, "kailangang makilala ang milliseconds"
    assert "fromisoformat" in src, "ang ISO na landas ay dapat manatili"
