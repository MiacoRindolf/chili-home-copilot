"""Pure unit tests for the IQFeed lookup client's safety contract.

These bind the invariants that keep a historical hydrator from disturbing the
live trading lane. They use a fake socket -- no IQConnect, no database, no
network -- so they are safe to run while the lane is live.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "iqfeed_lookup_client",
    Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_lookup_client.py",
)
assert _SPEC and _SPEC.loader
iqlc = importlib.util.module_from_spec(_SPEC)
sys.modules["iqfeed_lookup_client"] = iqlc
_SPEC.loader.exec_module(iqlc)


class FakeSocket:
    """Minimal socket stand-in that replays scripted server bytes."""

    def __init__(self, script: list[bytes]) -> None:
        self.script = list(script)
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _n: int) -> bytes:
        if not self.script:
            return b""
        return self.script.pop(0)

    def settimeout(self, _t: float) -> None:
        pass

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _client(script: list[bytes]) -> "iqlc.IQFeedLookupClient":
    c = iqlc.IQFeedLookupClient(verify_lane=False)
    c._sock = FakeSocket(script)  # type: ignore[assignment]
    return c


# --- the ports the live lane owns -------------------------------------------
@pytest.mark.parametrize("port", [5009, 9200, 9300, 9400])
def test_refuses_to_construct_against_live_lane_ports(port: int) -> None:
    """The lane's Level-1 stream (:5009) and depth feed (:9200) must be
    unreachable through this client by construction, not by convention."""
    with pytest.raises(ValueError, match="lookup-only"):
        iqlc.IQFeedLookupClient(port=port, verify_lane=False)


def test_lookup_port_is_accepted() -> None:
    c = iqlc.IQFeedLookupClient(port=9100, verify_lane=False)
    assert c.port == 9100


# --- response parsing --------------------------------------------------------
def test_request_strips_request_id_and_stops_at_endmsg() -> None:
    c = _client(
        [
            b"chili00001,LH,2026-09-02 19:07:44.107328,324.7000,50,1,324.70,324.79,1,E,19,17,0,2,\r\n"
            b"chili00001,LH,2026-09-02 19:07:36.734824,324.7800,160,2,324.70,324.79,2,E,11,17,0,2,\r\n"
            b"chili00001,!ENDMSG!,\r\n"
            b"chili00002,LH,SHOULD-NOT-BE-READ\r\n"
        ]
    )
    res = c.request("HTX,AAPL,2,0")
    assert res.n_records == 2
    assert res.lines[0].startswith("LH,2026-09-02 19:07:44.107328")
    assert "SHOULD-NOT-BE-READ" not in "".join(res.lines)
    assert c._sock.sent == [b"HTX,AAPL,2,0,chili00001\r\n"]  # type: ignore[union-attr]


def test_no_data_is_not_an_error() -> None:
    """Retention probing depends on telling an empty window apart from a
    genuine failure; `!NO_DATA!` is the empty case."""
    c = _client([b"chili00001,E,!NO_DATA!,\r\n"])
    res = c.request("HTT,AAPL,20250101 093000,20250101 093100,1")
    assert res.no_data is True
    assert res.error is None
    assert res.n_records == 0


def test_real_error_still_raises() -> None:
    c = _client([b"chili00001,E,Invalid symbol,\r\n"])
    with pytest.raises(iqlc.LookupError, match="Invalid symbol"):
        c.request("HTX,NOPE,1,0")


# --- the desync bug the entitlement battery exposed --------------------------
def test_byte_cap_poisons_the_connection() -> None:
    """Aborting mid-response leaves unread bytes of the ABORTED request in the
    socket. Reusing that connection silently corrupts the NEXT request, so the
    client must refuse to reuse it until reconnect()."""
    c = iqlc.IQFeedLookupClient(verify_lane=False, byte_cap=64)
    c._sock = FakeSocket([b"chili00001,LH," + b"x" * 200 + b"\r\n"])  # type: ignore[assignment]
    with pytest.raises(iqlc.ByteCapExceeded):
        c.request("HTT,AAPL,1,2,3")
    assert c._poisoned is True
    with pytest.raises(ConnectionError, match="poisoned"):
        c.request("HTX,AAPL,1,0")


def test_request_ids_are_unique_per_connection() -> None:
    c = _client(
        [
            b"chili00001,!ENDMSG!,\r\n",
            b"chili00002,!ENDMSG!,\r\n",
            b"chili00003,!ENDMSG!,\r\n",
        ]
    )
    ids = [c.request("HTX,AAPL,1,0").request_id for _ in range(3)]
    assert ids == ["chili00001", "chili00002", "chili00003"]
    assert len(set(ids)) == 3


# --- the lane interlock ------------------------------------------------------
def test_connect_refuses_when_nothing_holds_the_l1_stream(monkeypatch) -> None:
    """If no client holds :5009, this client would become IQConnect's last
    connection and its disconnect would take IQConnect down (~5s), breaking the
    live lane. It must refuse rather than connect."""
    monkeypatch.setattr(
        iqlc, "assert_lane_clients_present",
        lambda: (_ for _ in ()).throw(RuntimeError("REFUSING TO CONNECT: no client")),
    )
    c = iqlc.IQFeedLookupClient(verify_lane=True)
    with pytest.raises(RuntimeError, match="REFUSING TO CONNECT"):
        c.connect()
