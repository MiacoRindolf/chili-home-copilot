"""IQFeed Level 2 (port 9200) shadow probe — capture raw TotalView frames.

Connects to the local IQConnect L2 port, negotiates protocol 6.2, tries the
known depth-watch dialects (legacy market-maker `w`, MBO `WOR`, price-level
`WPL`), and logs EVERY raw line with a timestamp. Run on the Windows host
(IQConnect binds 127.0.0.1 only). Output lands on the shared CHILI data mount
so containers/analysis can read it.

Usage:  python scripts/_iqfeed_depth_probe.py [SYM ...] [--seconds N]
"""
from __future__ import annotations

import os
import socket
import sys
import time
from datetime import datetime, timezone

HOST, PORT = "127.0.0.1", 9200
OUT_DIR = os.environ.get("IQFEED_DEPTH_LOG_DIR", r"D:\CHILI-Docker\chili-data\iqfeed_depth")

syms = [a.upper() for a in sys.argv[1:] if not a.startswith("--")] or ["AAPL", "TSLA"]
secs = 60.0
if "--seconds" in sys.argv:
    secs = float(sys.argv[sys.argv.index("--seconds") + 1])

os.makedirs(OUT_DIR, exist_ok=True)
log_path = os.path.join(OUT_DIR, datetime.now(timezone.utc).strftime("%Y-%m-%d") + "_l2_raw.log")
log = open(log_path, "a", encoding="utf-8")


def w(line: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    log.write(f"{stamp} {line}\n")
    print(f"{stamp} {line}"[:200])


s = socket.create_connection((HOST, PORT), timeout=10)
s.settimeout(2.0)
w(f">>> connected {HOST}:{PORT}; symbols={syms}; capture={secs}s; log={log_path}")


def send(cmd: str) -> None:
    s.sendall((cmd + "\r\n").encode())
    w(f">>> SENT: {cmd}")


send("S,SET PROTOCOL,6.2")
time.sleep(0.3)
for sym in syms:
    send(f"WOR,{sym}")   # MBO order-level watch (6.2)
    send(f"WPL,{sym}")   # price-level watch (6.2)
    send(f"w{sym}")      # legacy market-maker depth watch

deadline = time.monotonic() + secs
buf = b""
frames = 0
try:
    while time.monotonic() < deadline:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            w(">>> server closed connection")
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            frames += 1
            # log every distinct message type fully; throttle floods per type
            w(line.decode(errors="replace").rstrip())
finally:
    try:
        for sym in syms:
            s.sendall((f"ROR,{sym}\r\nRPL,{sym}\r\nr{sym}\r\n").encode())
        s.close()
    except Exception:
        pass
    w(f">>> done — {frames} frames captured")
    log.close()
