"""Ang IQFeed L2 depth bridge ay dapat kumonsumo ng STREAMING updates (2026-08-24).

SINUKAT SA LIVE MARKET (10:22 ET, AAPL at SGLY, sariling probe sa port 9200):

    uri '6'  =  larawan-sa-pag-subscribe LAMANG
                AAPL: lahat ng 122 frame sa UNANG SEGUNDO, tapos ZERO sa loob ng 19s.
    uri '4'  =  ang TUNAY na streaming update
                AAPL: 2,760 frame sa 12s (~230/s), eksaktong PAREHONG 12-field na hugis.
    uri '5'  =  pagbura ng level (8 field) -- hindi pa hinahawakan; bihira (1 sa 12s).

Ang bridge ay nagpa-parse LANG ng ``line[0] == "6"``, kaya ang libro ay nagbabago
LAMANG kapag pumuputok ang re-subscribe (~kada 21s). Mga bunga, nasukat sa
``iqfeed_depth_snapshots``:

  * ang buong 26-39 venue na libro ay nagbago lang sa 9.9% ng 2s na sample --
    imposible habang bukas ang market;
  * ``depth_imbal_pctile`` naka-pin sa 1.0, ``bid_refill``/``ask_build`` sa 0.0;
  * patay ang OFI;
  * 97.3% ng mga row ay duplicate ng nauna.

Isang degenerate na signal na nagsasabing "1.0" imbes na "wala ako" ay MAS
MASAHOL kaysa sa walang signal -- pinapakain nito ang exit state machine ng
kasinungalingan. Ang test na ito ay nagbabantay sa dispatch.

Runnable: pytest tests/test_iqfeed_depth_streaming_updates.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading

import pytest

_BRIDGE = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_depth_bridge.py"
)


@pytest.fixture(scope="module")
def bridge():
    name = "_iqfeed_depth_bridge"
    spec = importlib.util.spec_from_file_location(name, _BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    # Kailangang nasa sys.modules BAGO ang exec: ang dataclass ay nagre-resolve
    # ng mga annotation sa pamamagitan ng sys.modules[cls.__module__].
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeSocket:
    """Isinusuka ang mga naka-script na chunk, tapos nagsasara."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def recv(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""  # sarado ang server -> lumalabas ang reader


# Eksaktong mga byte mula sa live capture, palitan lang ang simbolo.
_IMAGE = b"6,ZTEST,,EDGX,B,10.0000,100,,4,10:22:21.212553,2026-08-24,\n"
_STREAM = b"4,ZTEST,,EDGX,B,10.5000,700,,4,10:22:21.774268,2026-08-24,\n"
_STREAM_ASK = b"4,ZTEST,,EDGX,A,10.6000,300,,4,10:22:21.774268,2026-08-24,\n"


def _drive(bridge, chunks: list[bytes]) -> None:
    """Patakbuhin ang reader hanggang maubos ang chunks, sa isang sariwang libro."""
    bridge.books.clear()
    bridge._capture_hot_symbols.clear()  # panatilihing tahimik ang capture handoff
    generation = bridge._begin_connection_generation()
    stop = threading.Event()
    bridge.running = True
    bridge.reader(_FakeSocket(chunks), stop, generation)


def test_type_4_streaming_update_reaches_the_book(bridge):
    """ANG REGRESSION: ang uri-4 ay dapat pumasok sa libro, hindi maitapon."""
    _drive(bridge, [_IMAGE, _STREAM])
    level = bridge.books["ZTEST"].levels[("EDGX", "B")]
    assert level.price == 10.5, "ang uri-4 ay dapat NAG-OVERWRITE ng larawan"
    assert level.size == 700.0


def test_type_6_image_alone_still_populates_the_book(bridge):
    """Ang larawan-sa-pag-subscribe ay dapat gumana pa rin nang mag-isa."""
    _drive(bridge, [_IMAGE])
    level = bridge.books["ZTEST"].levels[("EDGX", "B")]
    assert (level.price, level.size) == (10.0, 100.0)


def test_a_pure_type_4_stream_builds_a_snapshot(bridge):
    """Ang libro ay dapat maging kumpleto MULA LANG sa streaming frames.

    Ito ang buong punto: kapag naitapon ang uri-4, ang libro ay nagyeyelo sa
    huling larawan at ang snapshot ay nagiging duplicate magpakailanman.
    """
    _drive(bridge, [_STREAM, _STREAM_ASK])
    snap = bridge.books["ZTEST"].snapshot()
    assert snap is not None, "ang uri-4 lang ay dapat sapat na para sa snapshot"
    assert snap["bid_top"] == 10.5
    assert snap["ask_top"] == 10.6
    assert snap["bid_top_size"] == 700.0


def test_dispatch_accepts_both_frame_types(bridge):
    """Bantayan ang dispatch mismo -- huwag payagang bumalik sa '6' lang."""
    import inspect

    src = inspect.getsource(bridge.reader)
    assert 'line[0] in ("4", "6")' in src, (
        "ang reader ay dapat tumanggap ng uri-4 (streaming) AT uri-6 (larawan)"
    )
    assert 'line[0] == "6"' not in src, "ang '6'-lang na dispatch ay bumalik"


def test_uncaptured_diagnostic_is_throttled(bridge):
    """Sa ~230 frame/s, ang hubad na per-frame na log.error ay pumapatay ng bridge.

    Ang parehong hubad na log sa TRADE bridge ay gumawa ng 275 MB na log,
    kumain ng 66% ng isang core, at HUMINTO ang tick writes nang 5 minuto sa
    09:30 open. Ang uri-4 ay nagpaparami ng frame rate ng ~100x, kaya ang
    throttle ay kondisyon para sa pag-ship.
    """
    import inspect

    src = inspect.getsource(bridge._publish_capture_delta_locked)
    assert "_uncaptured_should_log(" in src

    # Isang log kada window, ang natitira ay binibilang -- walang tahimik na pagkawala.
    bridge._uncaptured_log_state.update({"next_at": 0.0, "suppressed": 0, "lost_rows": 0})
    first, sup, agg = bridge._uncaptured_should_log(1)
    assert first is True and agg == 1
    for _ in range(500):
        allowed, _s, _a = bridge._uncaptured_should_log(1)
        assert allowed is False, "isang linya lang kada window"
    assert bridge._uncaptured_log_state["suppressed"] == 500
    assert bridge._uncaptured_log_state["lost_rows"] == 500, "buo ang pagbibilang"
