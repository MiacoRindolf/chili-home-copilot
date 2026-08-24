"""Ang uri-5 na L2 frame ay nagbubura ng level — kung hindi, MULTO ang natitira.

SINUKAT SA LIVE MARKET (2026-08-24, sariling probe sa port 9200). Tatlong uri ng
frame ang dumarating sa depth feed::

    6,AAPL,,EDGX,A,312.8500,40,,4,10:22:21.212553,2026-08-24,   larawan (12 field)
    4,AAPL,,EDGX,A,312.8500,40,,4,10:22:21.774268,2026-08-24,   streaming (12 field)
    5,AAPL,,WCHV,B,,2026-08-24,                                  DELETE (8 field)

Ang uri-5 ay walang presyo at walang laki: tinatanggal ng venue ang quote nito.
Kung hindi ito hahawakan, ang level ay nananatili sa libro hanggang sa
``STALE_VENUE_ROW_S`` (**900 segundo**) -- isang multong venue na nagpapalaki ng
``bid5_size``/``ask5_size`` at maaaring magpabaluktot sa mismong top-of-book.

Bihira (**1 sa 12s** na nasukat) pero permanenteng mali habang nananatili ito.

Runnable: pytest tests/test_depth_level_delete.py -v
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
    name = "_iqfeed_depth_bridge_del"
    spec = importlib.util.spec_from_file_location(name, _BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


def _drive(bridge, chunks):
    bridge.books.clear()
    bridge._capture_hot_symbols.clear()
    gen = bridge._begin_connection_generation()
    bridge.running = True
    bridge.reader(_FakeSocket(chunks), threading.Event(), gen)


# Eksaktong mga byte mula sa live capture, palitan lang ang simbolo.
B_EDGX = b"4,ZDEL,,EDGX,B,10.0000,100,,4,10:22:21.1,2026-08-24,\n"
B_ARCX = b"4,ZDEL,,ARCX,B,9.9000,300,,4,10:22:21.2,2026-08-24,\n"
A_EDGX = b"4,ZDEL,,EDGX,A,10.1000,200,,4,10:22:21.3,2026-08-24,\n"
DEL_B_ARCX = b"5,ZDEL,,ARCX,B,,2026-08-24,\n"
DEL_B_EDGX = b"5,ZDEL,,EDGX,B,,2026-08-24,\n"


def test_a_delete_removes_the_level(bridge):
    """ANG PANGUNAHING KASO: nawawala ang venue sa libro."""
    _drive(bridge, [B_EDGX, B_ARCX, A_EDGX, DEL_B_ARCX])
    levels = bridge.books["ZDEL"].levels
    assert ("ARCX", "B") not in levels, "dapat naalis na ang binurang venue"
    assert ("EDGX", "B") in levels, "ang ibang venue ay dapat manatili"
    assert ("EDGX", "A") in levels


def test_without_the_delete_the_ghost_survives(bridge):
    """Kontrol: kung walang uri-5, nananatili ang level (ang lumang gawi)."""
    _drive(bridge, [B_EDGX, B_ARCX, A_EDGX])
    assert ("ARCX", "B") in bridge.books["ZDEL"].levels


def test_the_aggregates_shrink_after_a_delete(bridge):
    """Ang multo ay nagpapalaki ng bid5 -- iyon ang tunay na pinsala."""
    _drive(bridge, [B_EDGX, B_ARCX, A_EDGX])
    before = bridge.books["ZDEL"].snapshot()
    assert before["bid5_size"] == pytest.approx(400.0)

    _drive(bridge, [B_EDGX, B_ARCX, A_EDGX, DEL_B_ARCX])
    after = bridge.books["ZDEL"].snapshot()
    assert after["bid5_size"] == pytest.approx(100.0), "dapat nawala ang 300 ng ARCX"
    assert after["bid_top"] == pytest.approx(10.0), "hindi apektado ang top-of-book"


def test_deleting_the_top_of_book_repoints_it(bridge):
    """Kapag ang PINAKAMAGANDANG bid ang tinanggal, ang susunod ang pumapalit."""
    _drive(bridge, [B_EDGX, B_ARCX, A_EDGX, DEL_B_EDGX])
    snap = bridge.books["ZDEL"].snapshot()
    assert snap is not None
    assert snap["bid_top"] == pytest.approx(9.9), "ang ARCX na ngayon ang top bid"
    assert snap["bid_top_size"] == pytest.approx(300.0)


def test_a_delete_for_an_unknown_venue_is_a_noop(bridge):
    """Ang delete bago ang anumang quote ay hindi dapat sumabog o gumawa ng level."""
    _drive(bridge, [B_EDGX, A_EDGX, b"5,ZDEL,,NOPE,B,,2026-08-24,\n"])
    levels = bridge.books["ZDEL"].levels
    assert ("NOPE", "B") not in levels
    assert ("EDGX", "B") in levels


def test_a_malformed_delete_is_ignored(bridge):
    """Maikli/basurang uri-5 ⇒ laktawan, huwag sirain ang reader."""
    _drive(bridge, [B_EDGX, A_EDGX, b"5,ZDEL\n", b"5,,,,,\n", DEL_B_EDGX])
    # ang huling maayos na delete ay dapat pa ring gumana
    assert ("EDGX", "B") not in bridge.books["ZDEL"].levels


def test_deleting_a_whole_side_makes_the_snapshot_none(bridge):
    """Ang isang panig na libro ay hindi maaaring maging snapshot -- fail-closed."""
    _drive(bridge, [B_EDGX, A_EDGX, DEL_B_EDGX])
    assert bridge.books["ZDEL"].snapshot() is None


def test_the_dispatch_handles_type_5(bridge):
    """Bantayan ang dispatch -- huwag payagang tahimik na mawala."""
    import inspect

    src = inspect.getsource(bridge.reader)
    assert 'line[0] == "5"' in src, "ang uri-5 ay dapat may sariling branch"
    assert "level_delete" in src, "ang delete ay dapat may sariling condition code"
