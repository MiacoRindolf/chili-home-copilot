"""IQFeed Level 1 TRADE-tape bridge — host-side daemon feeding CHILI the equity trade tape.

The equity Massive WS feed carries quotes (bid/ask/last PRICE) but NO per-trade size/side, so
the aggressor / signed-volume trade-flow signal (the research's #2 micro signal; Ross's "ask
getting eaten") was DATA-BLOCKED for equities (the size+side TapeTrade ring is crypto-only). IQFeed
Level 1 DOES carry Most-Recent-Trade price + SIZE + time, so this bridge captures it.

Mirrors iqfeed_depth_bridge.py (the proven :9200 depth daemon) but on the Level-1 port (:9100) and
its OWN table — the depth bridge is left UNTOUCHED (isolation; depth is load-bearing). Flow:

  IQConnect :9100 --(L1 Q/P update frames: Last, Last Size, Bid, Ask, Last Time)--> new-trade detect
      --> iqfeed_trade_ticks rows (one per genuinely-new trade per symbol)

The app-side consumer is pipeline._live_trade_flow (tick/quote-rule signed-volume aggressor imbalance
in [-1,1]) -> captured as the meta-label feature `trade_flow`. Symbols = the momentum lane's runnable
LIVE equity sessions (same query as the depth bridge), polled every REFRESH_S; sticky re-subscribe so
a silent IQFeed drop self-heals.

Usage: python scripts/iqfeed_trade_bridge.py [--seconds N] [--selftest] [SYM ...]
  --selftest writes one synthetic row + reads it back (verifies the DB path WITHOUT IQFeed).
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone

import sqlalchemy as sa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iqfeed_trade_bridge")

HOST, PORT = "127.0.0.1", 5009          # IQConnect Level-1 STREAMING port (:9100=lookup, :9200=L2 depth)
DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
FLUSH_INTERVAL_S = 1.0                   # batch-insert cadence
REFRESH_S = 20.0                         # live-session symbol refresh cadence
STICKY_RESUBSCRIBE = os.environ.get("CHILI_IQFEED_STICKY_RESUBSCRIBE", "1") != "0"
# We use IQFeed's DEFAULT 6.2 update layout (NO `SELECT UPDATE FIELDS` — it raised !SYNTAX_ERROR! and
# is unnecessary). Verified live layout (S,CURRENT UPDATE FIELDNAMES): Symbol, Most Recent Trade,
# Most Recent Trade Size, Most Recent Trade Time, Most Recent Trade Market Center, Total Volume, Bid,
# Bid Size, Ask, Ask Size, ... -> p[1]=symbol p[2]=last p[3]=size p[4]=time p[7]=bid p[9]=ask.
L1_LAST, L1_SIZE, L1_TIME, L1_BID, L1_ASK = 2, 3, 4, 7, 9

engine = sa.create_engine(DB_URL, pool_pre_ping=True)

DDL = """
CREATE TABLE IF NOT EXISTS iqfeed_trade_ticks (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    bid DOUBLE PRECISION, ask DOUBLE PRECISION,
    source VARCHAR(24) NOT NULL DEFAULT 'iqfeed_l1'
);
CREATE INDEX IF NOT EXISTS ix_iqfeed_trades_sym_at ON iqfeed_trade_ticks (symbol, observed_at DESC);
"""

INS = sa.text(
    "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask) "
    "VALUES (:sym, :at, :px, :sz, :bid, :ask)"
)

# Also feed the momentum ENTRY-GATE's NBBO freshness tape with this SAME tick-level IQFeed L1.
# The entry gate reads momentum_nbbo_spread_tape for its stale_bbo freshness check, but that
# table was fed ONLY by the slower/sparser Massive WS recorder — so the fresh tick-level IQFeed
# quotes (this bridge) NEVER reached the entry decision and wide-spread movers false-blocked on
# stale_bbo (VNTG had 1578 IQFeed ticks/5min @1s old, yet the gate saw a 10-270s WS quote).
# Mirror each valid-quote tick into the tape (source='iqfeed_l1') so the gate uses the freshest
# available quote. Default ON; IQFEED_WRITE_NBBO_TAPE=0 reverts.
WRITE_NBBO_TAPE = os.environ.get("IQFEED_WRITE_NBBO_TAPE", "1").strip().lower() not in ("0", "false", "no")
NBBO_INS = sa.text(
    "INSERT INTO momentum_nbbo_spread_tape "
    "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
    "VALUES (:sym, :at, :bid, :ask, :mid, :spread_bps, NULL, 'iqfeed_l1')"
)

_pending: list[dict] = []
_pending_lock = threading.Lock()
_last_trade: dict[str, str] = {}        # symbol -> last seen Most-Recent-Trade-Time (dedup key)
watched: set[str] = set()
sock_lock = threading.Lock()
sock: socket.socket | None = None
running = True


def _send(cmd: str) -> None:
    with sock_lock:
        if sock is not None:
            sock.sendall((cmd + "\r\n").encode())


def _live_symbols() -> set[str]:
    try:
        with engine.connect() as c:
            rows = c.execute(sa.text(
                "SELECT DISTINCT symbol FROM trading_automation_sessions "
                "WHERE mode='live' AND symbol NOT LIKE '%-USD' AND state IN "
                "('armed_pending_runner','queued_live','watching_live','live_entry_candidate',"
                "'live_pending_entry','live_entered','live_scaling_out','live_trailing','live_bailout')"
            )).fetchall()
        return {str(r[0]).upper() for r in rows}
    except Exception as e:
        log.warning("symbol query failed: %s", e)
        return set()


def _parse_l1(line: str) -> None:
    """Parse one L1 update frame ('Q'/'P') under IQFeed's DEFAULT 6.2 layout; record a row only on a
    GENUINELY NEW trade (Most-Recent-Trade-Time advanced) with a positive size (quote-only updates
    that don't move the trade time are skipped)."""
    p = line.split(",")
    if len(p) <= L1_ASK:
        return
    try:
        sym = p[1].strip().upper()
        if not sym:
            return
        last_t = p[L1_TIME].strip()
        if not last_t or _last_trade.get(sym) == last_t:
            return                              # not a new trade (quote-only update or dup)
        px = float(p[L1_LAST] or 0)
        sz = float(p[L1_SIZE] or 0)
        if px <= 0 or sz <= 0:
            return
        bid = float(p[L1_BID]) if p[L1_BID].strip() else None
        ask = float(p[L1_ASK]) if p[L1_ASK].strip() else None
        _last_trade[sym] = last_t
        row = {"sym": sym, "at": datetime.now(timezone.utc).replace(tzinfo=None),
               "px": px, "sz": sz, "bid": bid, "ask": ask}
        with _pending_lock:
            _pending.append(row)
    except (ValueError, IndexError):
        return


def reader() -> None:
    global running
    buf = b""
    seen = 0
    while running:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            log.warning("server closed connection")
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            c0 = line[0]
            if c0 in ("Q", "P"):                # Level-1 update / summary -> trade detect
                _parse_l1(line)
                seen += 1
            elif c0 == "T":                     # timestamp heartbeat (NOT a trade)
                continue
            elif line.startswith("S,") or c0 in ("n", "E"):
                log.info("feed: %s", line[:160])
    running = False


def writer(forced_syms: set[str], deadline: float | None) -> None:
    global running
    last_refresh = 0.0
    last_prune = 0.0
    while running and (deadline is None or time.monotonic() < deadline):
        time.sleep(FLUSH_INTERVAL_S)
        # retention prune (the exit_parity_log bloat lesson): a rolling research window, not an archive
        if time.monotonic() - last_prune >= 3600.0:
            try:
                with engine.begin() as c:
                    c.execute(sa.text(
                        "DELETE FROM iqfeed_trade_ticks "
                        "WHERE observed_at < (now() at time zone 'utc') - interval '3 days'"))
            except Exception as e:
                log.debug("retention prune failed: %s", e)
            last_prune = time.monotonic()
        if time.monotonic() - last_refresh >= REFRESH_S:
            target = forced_syms or _live_symbols()
            for sym in target - watched:
                _send(f"w{sym}")                # Level-1 watch -> Q/P updates with Last + Last Size
                watched.add(sym)
                log.info("watching trades: %s", sym)
            if STICKY_RESUBSCRIBE:
                for sym in (target & watched):
                    _send(f"w{sym}")            # re-send so a silent IQFeed drop self-heals
            for sym in watched - target:
                _send(f"r{sym}")                # unwatch
                watched.discard(sym)
                _last_trade.pop(sym, None)
            last_refresh = time.monotonic()
        with _pending_lock:
            rows = _pending[:]
            _pending.clear()
        if rows:
            try:
                with engine.begin() as c:
                    c.execute(INS, rows)
                    if WRITE_NBBO_TAPE:
                        nbbo = []
                        for r in rows:
                            b, a = r.get("bid"), r.get("ask")
                            if b and a and b > 0 and a > 0 and a >= b:
                                mid = (b + a) / 2.0
                                nbbo.append({
                                    "sym": r["sym"], "at": r["at"], "bid": b, "ask": a,
                                    "mid": mid, "spread_bps": (a - b) / mid * 10_000.0,
                                })
                        if nbbo:
                            c.execute(NBBO_INS, nbbo)
            except Exception as e:
                log.warning("trade insert failed (%d rows): %s", len(rows), e)
    running = False


def _selftest() -> int:
    """Verify the DB path WITHOUT IQFeed: write a synthetic row, read it back."""
    with engine.begin() as c:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as c:
        c.execute(INS, [{"sym": "_SELFTEST", "at": now, "px": 1.23, "sz": 100.0, "bid": 1.22, "ask": 1.24}])
    with engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM iqfeed_trade_ticks WHERE symbol='_SELFTEST'")).scalar()
        c2 = c.execute(sa.text("DELETE FROM iqfeed_trade_ticks WHERE symbol='_SELFTEST'"))  # noqa: F841
    log.info("selftest: wrote+read %s synthetic row(s), table OK", n)
    return 0 if n and n >= 1 else 1


def main() -> None:
    global sock, running
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    forced = {a.upper() for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()}
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.monotonic() + float(sys.argv[sys.argv.index("--seconds") + 1])
    with engine.begin() as c:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))
    while deadline is None or time.monotonic() < deadline:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=10)
            sock.settimeout(2.0)
            running = True
            watched.clear()
            _last_trade.clear()
            _send("S,SET PROTOCOL,6.2")          # default update layout (no SELECT -> see L1_* indices)
            log.info("connected to IQConnect %s:%s (L1 trades, protocol 6.2)", HOST, PORT)
            t = threading.Thread(target=reader, daemon=True, name="iqfeed-trade-reader")
            t.start()
            writer(forced, deadline)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning("bridge error: %s", e)
        finally:
            running = False
            try:
                sock.close()
            except Exception:
                pass
        if deadline is not None and time.monotonic() >= deadline:
            break
        log.info("reconnecting in 10s…")
        time.sleep(10)
    log.info("trade bridge stopped")


if __name__ == "__main__":
    main()
