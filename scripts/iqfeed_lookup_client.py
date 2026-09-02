"""IQFeed LOOKUP-port (:9100) historical client — read-only, lane-safe.

WHY A SEPARATE CLIENT
---------------------
``scripts/iqfeed_trade_bridge.py`` speaks the Level-1 STREAMING port (:5009) and
``scripts/iqfeed_depth_bridge.py`` speaks Level-2 depth (:9200).  Neither of them
ever opens :9100.  Historical tick/interval/daily data lives ONLY on the lookup
port, so hydrating history for a symbol the live bridge was never subscribed to
requires a client that did not previously exist in this repository.

LANE SAFETY CONTRACT (non-negotiable)
-------------------------------------
A live trading lane is running.  IQConnect (one process) listens on :5009,
:9100, :9200, :9300 and :9400 simultaneously; the streaming and lookup ports are
SEPARATE listening sockets.  This client therefore:

  * connects ONLY to the lookup port, never to :5009 or :9200;
  * issues ONLY read-only historical commands (H* family).  It never sends a
    watch/unwatch, never sends ``S,SET`` anything except the per-connection
    protocol negotiation, and never touches the :9300 admin port -- so it cannot
    reconfigure IQConnect or alter the live subscription set;
  * caps every request with an explicit datapoint ceiling AND an independent
    byte ceiling, so a mis-typed request cannot pull an unbounded stream through
    the shared IQConnect process;
  * holds ONE connection at a time and reuses it across requests, because
    IQFeed's limit is on simultaneous lookup connections, not on request count;
  * closes cleanly.  Memory records that IQConnect exits ~5s after its LAST
    client disconnects -- that is not a hazard here because the trade bridge and
    depth bridge hold :5009 and :9200 open continuously, so this client is never
    the last one out.  ``assert_lane_clients_present()`` verifies exactly that
    BEFORE the first connect and refuses to run if the lane is not holding the
    stream.

Protocol: IQFeed 6.2, CRLF-terminated commands, ``!ENDMSG!,`` terminates a
successful lookup response, ``E,`` prefixes an error line.

Usage:
  python scripts/iqfeed_lookup_client.py --smoke
  python scripts/iqfeed_lookup_client.py --htx AAPL 10
  python scripts/iqfeed_lookup_client.py --htt CANF 20260902 093000 20260902 093010
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Iterator

LOOKUP_HOST = "127.0.0.1"
LOOKUP_PORT = 9100
PROTOCOL = "6.2"

# Ports this client must NEVER open. The live lane owns them.
FORBIDDEN_PORTS = (5009, 9200, 9300, 9400)

END_MSG = "!ENDMSG!"
ERROR_PREFIX = "E,"

# Independent ceilings. `max_datapoints` is what we ask IQFeed for; `byte_cap`
# is what we are willing to receive regardless of what it actually sends.
DEFAULT_BYTE_CAP = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S = 15.0


class LookupError(RuntimeError):
    """IQFeed returned an ``E,`` error line for a lookup request."""


class ByteCapExceeded(RuntimeError):
    """A response exceeded the client-side byte ceiling and was aborted."""


@dataclass
class RequestResult:
    request_id: str
    command: str
    lines: list[str] = field(default_factory=list)
    bytes_received: int = 0
    elapsed_s: float = 0.0
    error: str | None = None
    no_data: bool = False

    @property
    def n_records(self) -> int:
        return len(self.lines)


def assert_lane_clients_present() -> dict[str, object]:
    """Refuse to run unless the live lane is holding the streaming ports.

    This is the safety interlock: if the bridges are NOT connected, then this
    client could become IQConnect's last client, and its disconnect would take
    IQConnect (and therefore the lane's ability to reconnect) down with it.
    """
    import subprocess

    ps = (
        "Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | "
        "Where-Object { $_.RemotePort -in 5009,9200 -or $_.LocalPort -in 5009,9200 } | "
        "Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess | "
        "ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
    )
    raw = (out.stdout or "").strip()
    conns: list[dict] = []
    if raw:
        parsed = json.loads(raw)
        conns = parsed if isinstance(parsed, list) else [parsed]
    # A client-side connection is one whose REMOTE port is the IQConnect port.
    client_side = [c for c in conns if int(c.get("RemotePort", 0)) in (5009, 9200)]
    holders = {int(c["RemotePort"]): int(c["OwningProcess"]) for c in client_side}
    if 5009 not in holders:
        raise RuntimeError(
            "REFUSING TO CONNECT: no client is holding the IQConnect Level-1 "
            "stream (:5009). This client must never be IQConnect's last "
            "connection. Start/verify the trade bridge first."
        )
    return {"stream_holders": holders, "connections": client_side}


class IQFeedLookupClient:
    """One pooled connection to the IQFeed lookup port."""

    def __init__(
        self,
        host: str = LOOKUP_HOST,
        port: int = LOOKUP_PORT,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        byte_cap: int = DEFAULT_BYTE_CAP,
        verify_lane: bool = True,
    ) -> None:
        if port in FORBIDDEN_PORTS:
            raise ValueError(
                f"port {port} belongs to the live lane; this client is "
                "lookup-only and must never open it"
            )
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.byte_cap = byte_cap
        self.verify_lane = verify_lane
        self._sock: socket.socket | None = None
        self._buf = b""
        self._req_seq = 0
        self._poisoned = False
        self.protocol_ack: str | None = None
        self.lane_state: dict[str, object] | None = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "IQFeedLookupClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self.verify_lane:
            self.lane_state = assert_lane_clients_present()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        sock.connect((self.host, self.port))
        self._sock = sock
        self._send(f"S,SET PROTOCOL,{PROTOCOL}")
        # The lookup port answers protocol negotiation with a system line.
        for line in self._read_lines_until(
            lambda ln: ln.startswith("S,CURRENT PROTOCOL")
            or ln.startswith(ERROR_PREFIX),
            deadline=time.monotonic() + self.timeout_s,
        ):
            self.protocol_ack = line
            break
        if not self.protocol_ack or not self.protocol_ack.startswith(
            "S,CURRENT PROTOCOL"
        ):
            raise RuntimeError(
                f"IQFeed lookup did not acknowledge protocol {PROTOCOL}: "
                f"{self.protocol_ack!r}"
            )

    def reconnect(self) -> None:
        """Drop and re-establish the lookup connection (clears poisoning)."""
        self.close()
        self._buf = b""
        self._poisoned = False
        self.connect()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # -- transport ---------------------------------------------------------
    def _send(self, cmd: str) -> None:
        assert self._sock is not None, "not connected"
        self._sock.sendall((cmd + "\r\n").encode("ascii"))

    def _read_lines_until(self, predicate, *, deadline: float) -> Iterator[str]:
        """Yield decoded lines until ``predicate(line)`` is true (inclusive)."""
        assert self._sock is not None, "not connected"
        received = 0
        while True:
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").rstrip("\r")
                if not line:
                    continue
                yield line
                if predicate(line):
                    return
            if time.monotonic() > deadline:
                raise TimeoutError("IQFeed lookup response timed out")
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                if time.monotonic() > deadline:
                    raise TimeoutError("IQFeed lookup response timed out")
                continue
            if not chunk:
                raise ConnectionError("IQFeed lookup closed the connection")
            received += len(chunk)
            if received > self.byte_cap:
                # The server is still streaming the rest of this response. The
                # connection is now desynchronized: any bytes we read next
                # belong to the aborted request, not to the next one. Reusing it
                # silently corrupts the FOLLOWING request (observed: a 1-tick
                # HTX inheriting a 200k-tick backlog and tripping the cap
                # again). Poison the connection so callers must reconnect.
                self._poisoned = True
                raise ByteCapExceeded(
                    f"response exceeded byte cap {self.byte_cap} after "
                    f"{received} bytes; connection poisoned, reconnect required"
                )
            self._buf += chunk

    # -- requests ----------------------------------------------------------
    def _next_request_id(self) -> str:
        self._req_seq += 1
        return f"chili{self._req_seq:05d}"

    def request(self, command_body: str, *, timeout_s: float | None = None) -> RequestResult:
        """Issue one lookup command whose trailing field is the RequestID.

        Returns every data line, with the ``RequestID,`` prefix stripped.
        Raises ``LookupError`` if IQFeed answers with an ``E,`` line.
        """
        if self._poisoned:
            raise ConnectionError(
                "connection is poisoned by a previously aborted response; "
                "call reconnect() before issuing further requests"
            )
        rid = self._next_request_id()
        cmd = f"{command_body},{rid}"
        started = time.monotonic()
        deadline = started + (timeout_s or self.timeout_s)
        result = RequestResult(request_id=rid, command=cmd)
        self._send(cmd)
        prefix = f"{rid},"
        for line in self._read_lines_until(
            lambda ln: ln.startswith(f"{prefix}{END_MSG}")
            or ln.startswith(f"{prefix}{ERROR_PREFIX}"),
            deadline=deadline,
        ):
            result.bytes_received += len(line) + 2
            if not line.startswith(prefix):
                continue
            payload = line[len(prefix):]
            if payload.startswith(END_MSG):
                break
            if payload.startswith(ERROR_PREFIX):
                err = payload[len(ERROR_PREFIX):].strip().rstrip(",")
                # `!NO_DATA!` is not a failure: it is IQFeed's way of saying the
                # requested window is empty (symbol not traded, date outside
                # retention, or a filter that excluded everything). Retention
                # probing depends on telling this apart from a real error.
                if err.upper().strip("!") == "NO_DATA":
                    result.no_data = True
                else:
                    result.error = err
                break
            result.lines.append(payload)
        result.elapsed_s = time.monotonic() - started
        if result.error:
            raise LookupError(f"{cmd} -> {result.error}")
        return result

    # -- typed helpers -----------------------------------------------------
    def htx(self, symbol: str, max_datapoints: int, *, direction: int = 0) -> RequestResult:
        """HTX: the most recent ``max_datapoints`` ticks for ``symbol``."""
        return self.request(f"HTX,{symbol},{int(max_datapoints)},{int(direction)}")

    def htd(
        self,
        symbol: str,
        days: int,
        max_datapoints: int = 100,
        *,
        begin_filter: str = "",
        end_filter: str = "",
        direction: int = 0,
    ) -> RequestResult:
        """HTD: ticks for the last ``days`` calendar days."""
        return self.request(
            f"HTD,{symbol},{int(days)},{int(max_datapoints)},"
            f"{begin_filter},{end_filter},{int(direction)}"
        )

    def htt(
        self,
        symbol: str,
        begin: str,
        end: str,
        max_datapoints: int = 100,
        *,
        begin_filter: str = "",
        end_filter: str = "",
        direction: int = 0,
        timeout_s: float | None = None,
    ) -> RequestResult:
        """HTT: ticks in [begin, end]; timestamps are ``CCYYMMDD HHmmSS``."""
        return self.request(
            f"HTT,{symbol},{begin},{end},{int(max_datapoints)},"
            f"{begin_filter},{end_filter},{int(direction)}",
            timeout_s=timeout_s,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_result(res: RequestResult, *, head: int = 12) -> None:
    print(f"cmd={res.command}")
    print(
        f"records={res.n_records} bytes={res.bytes_received} "
        f"elapsed={res.elapsed_s:.3f}s"
    )
    for line in res.lines[:head]:
        print(f"  {line}")
    if res.n_records > head:
        print(f"  ... ({res.n_records - head} more)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true", help="connect + protocol ack only")
    ap.add_argument("--htx", nargs=2, metavar=("SYMBOL", "N"))
    ap.add_argument("--htt", nargs=5, metavar=("SYMBOL", "BDATE", "BTIME", "EDATE", "ETIME"))
    ap.add_argument("--max-datapoints", type=int, default=100)
    ap.add_argument("--no-verify-lane", action="store_true")
    args = ap.parse_args(argv)

    client = IQFeedLookupClient(verify_lane=not args.no_verify_lane)
    t0 = time.monotonic()
    client.connect()
    print(f"connected {LOOKUP_HOST}:{LOOKUP_PORT} in {time.monotonic() - t0:.3f}s")
    print(f"protocol_ack={client.protocol_ack}")
    if client.lane_state:
        print(f"lane_stream_holders={client.lane_state['stream_holders']}")
    try:
        if args.htx:
            _print_result(client.htx(args.htx[0].upper(), int(args.htx[1])))
        if args.htt:
            sym, bd, bt, ed, et = args.htt
            _print_result(
                client.htt(
                    sym.upper(),
                    f"{bd} {bt}",
                    f"{ed} {et}",
                    args.max_datapoints,
                )
            )
    finally:
        client.close()
        print("closed cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
