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
import json
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
FLUSH_INTERVAL_S = float(os.environ.get("IQFEED_TRADE_FLUSH_INTERVAL_S", "0.25") or 0.25)
REFRESH_S = 20.0                         # live-session symbol refresh cadence
STALE_NBBO_RECONNECT_S = float(os.environ.get("IQFEED_STALE_NBBO_RECONNECT_SECONDS", "45") or 45)
STICKY_RESUBSCRIBE = os.environ.get("CHILI_IQFEED_STICKY_RESUBSCRIBE", "1") != "0"
IQFEED_NOTIFY_ENABLED = os.environ.get("IQFEED_NOTIFY_ENABLED", "1").strip().lower() not in ("0", "false", "no")
IQFEED_NOTIFY_CHANNEL = os.environ.get("IQFEED_NOTIFY_CHANNEL", "momentum_iqfeed_l1").strip() or "momentum_iqfeed_l1"
SKIP_STARTUP_DDL = os.environ.get("IQFEED_TRADE_SKIP_STARTUP_DDL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# --- Version-agnostic-backtest coverage (STEP 0): watch the ELIGIBLE-MOVER universe (the names ANY momentum
# version could pick — ranked by explosiveness), not just armed names, so a backtest of a NEW version has
# prints to fill against. The working cap is SELF-DISCOVERED: start at WATCH_HARD_MAX, HALVE on an IQFeed
# symbol-limit signal (the rail-governor pattern), floored at WATCH_FLOOR — no need to know the plan's limit
# up front. The fresh-eligible set is the natural ceiling (usually a few hundred), so the cap rarely binds.
# One documented base (WATCH_FLOOR); the cap is adaptive. Retention raised 3d->30d so we can backtest N days.
RETENTION_DAYS = float(os.environ.get("IQFEED_TRADE_RETENTION_DAYS", "30") or 30)
WATCH_FLOOR = int(os.environ.get("IQFEED_WATCH_FLOOR", "64") or 64)          # the ONE documented base
WATCH_HARD_MAX = int(os.environ.get("IQFEED_WATCH_HARD_MAX", "1000") or 1000)  # backstop only
ELIGIBLE_FRESH_S = float(os.environ.get("IQFEED_ELIGIBLE_FRESH_SECONDS", "1800") or 1800)  # mover-freshness window
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
CREATE INDEX IF NOT EXISTS ix_nbbo_tape_source_symbol_observed
    ON momentum_nbbo_spread_tape (source, symbol, observed_at DESC);
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
NOTIFY_IQFEED_TICK = sa.text("SELECT pg_notify(:channel, :payload)")

_pending: list[dict] = []
_pending_nbbo: list[dict] = []
_pending_lock = threading.Lock()
_last_trade: dict[str, str] = {}        # symbol -> last seen Most-Recent-Trade-Time (dedup key)
watched: set[str] = set()
_max_watch = WATCH_HARD_MAX             # adaptive watch cap; halved on an IQFeed limit signal, floored at WATCH_FLOOR
_limit_hit = False                      # set by the reader thread when IQFeed signals a symbol limit
sock_lock = threading.Lock()
sock: socket.socket | None = None
running = True
_last_nbbo_append_monotonic: float | None = None


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


def _eligible_symbols(limit: int) -> list[str]:
    """The fresh ELIGIBLE-MOVER universe ranked by explosiveness (viability_score) — the names ANY momentum
    version could pick, so a backtested new version has prints to fill against. Up to `limit`, most-explosive
    first. Empty outside market hours (no fresh viability) — fine, nothing to watch then."""
    if limit <= 0:
        return []
    try:
        with engine.connect() as c:
            rows = c.execute(sa.text(
                "SELECT symbol FROM ("
                "  SELECT DISTINCT ON (symbol) symbol, viability_score FROM momentum_symbol_viability "
                "  WHERE symbol NOT LIKE '%-USD' AND (live_eligible OR paper_eligible) "
                "    AND freshness_ts > (now() at time zone 'utc') - make_interval(secs => :fresh) "
                "  ORDER BY symbol, freshness_ts DESC"
                ") q ORDER BY viability_score DESC NULLS LAST LIMIT :lim"
            ), {"fresh": ELIGIBLE_FRESH_S, "lim": int(limit)}).fetchall()
        return [str(r[0]).upper() for r in rows]
    except Exception as e:
        log.warning("eligible query failed: %s", e)
        return []


def _ross_universe_symbols(limit: int) -> list[str]:
    """Ross-profile small-cap movers from the market snapshot.

    This removes the circular dependency where IQFeed only watched symbols that
    were already live/paper eligible. Event-driven admission needs raw tape on
    fresh Ross-profile movers before a viability row exists.
    """
    if limit <= 0:
        return []
    try:
        from app.services.trading.momentum_neural.universe import (
            EQUITY_ROSS_SMALLCAP,
            build_equity_universe,
        )

        symbols = build_equity_universe(EQUITY_ROSS_SMALLCAP) or []
        out: list[str] = []
        seen: set[str] = set()
        for sym in symbols:
            s = str(sym or "").strip().upper()
            if not s or s in seen or "-USD" in s:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log.debug("ross universe query failed: %s", e)
        return []


def _target_symbols(forced_syms: set[str], max_watch: int) -> set[str]:
    if forced_syms:
        return set(forced_syms)
    armed = _live_symbols()  # armed/live names: ALWAYS watched (load-bearing for live)
    room = max(0, int(max_watch) - len(armed))
    eligible = [s for s in _eligible_symbols(room) if s not in armed][:room]
    room = max(0, int(max_watch) - len(armed) - len(eligible))
    ross = [s for s in _ross_universe_symbols(room) if s not in armed and s not in eligible][:room]
    return armed | set(eligible) | set(ross)


def _parse_l1(line: str) -> None:
    """Parse one L1 update frame ('Q'/'P') under IQFeed's DEFAULT 6.2 layout; record a row only on a
    GENUINELY NEW trade (Most-Recent-Trade-Time advanced) with a positive size (quote-only updates
    that don't move the trade time are skipped)."""
    global _last_nbbo_append_monotonic
    p = line.split(",")
    if len(p) <= L1_ASK:
        return
    try:
        sym = p[1].strip().upper()
        if not sym:
            return
        last_t = p[L1_TIME].strip()
        px = float(p[L1_LAST] or 0)
        sz = float(p[L1_SIZE] or 0)
        bid = float(p[L1_BID]) if p[L1_BID].strip() else None
        ask = float(p[L1_ASK]) if p[L1_ASK].strip() else None
        observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if bid and ask and bid > 0 and ask > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            with _pending_lock:
                _pending_nbbo.append({
                    "sym": sym,
                    "at": observed_at,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_bps": (ask - bid) / mid * 10_000.0,
                })
            _last_nbbo_append_monotonic = time.monotonic()
        if not last_t or _last_trade.get(sym) == last_t:
            return                              # not a new trade (quote-only update or dup)
        if px <= 0 or sz <= 0:
            return
        _last_trade[sym] = last_t
        row = {"sym": sym, "at": observed_at, "px": px, "sz": sz, "bid": bid, "ask": ask}
        with _pending_lock:
            _pending.append(row)
    except (ValueError, IndexError):
        return


def reader() -> None:
    global running, _limit_hit
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
                if any(k in line.upper() for k in ("SYMBOL LIMIT", "MAX SYMBOL", "LIMIT REACHED", "TOO MANY SYMBOL")):
                    _limit_hit = True               # adaptive cap: writer halves the watch-set on this signal
                    log.warning("IQFeed symbol-limit signal: %s", line[:160])
                else:
                    log.info("feed: %s", line[:160])
    running = False


def writer(forced_syms: set[str], deadline: float | None) -> None:
    global running, _max_watch, _limit_hit
    last_refresh = 0.0
    # Subscribe first. The trade table can hold tens of millions of rows; doing the
    # retention delete before the first watch command leaves the live lane with no
    # IQFeed ticks during the exact morning window this bridge exists to cover.
    last_prune = time.monotonic()
    while running and (deadline is None or time.monotonic() < deadline):
        time.sleep(FLUSH_INTERVAL_S)
        # retention prune (the exit_parity_log bloat lesson): a rolling research window, not an archive
        if time.monotonic() - last_prune >= 3600.0:
            try:
                with engine.begin() as c:
                    c.execute(sa.text(
                        "DELETE FROM iqfeed_trade_ticks "
                        "WHERE observed_at < (now() at time zone 'utc') - make_interval(days => :d)"),
                        {"d": int(RETENTION_DAYS)})
            except Exception as e:
                log.debug("retention prune failed: %s", e)
            last_prune = time.monotonic()
        if time.monotonic() - last_refresh >= REFRESH_S:
            if _limit_hit:                          # adaptive: IQFeed signalled its symbol limit -> back off
                _max_watch = max(WATCH_FLOOR, len(watched) // 2)
                _limit_hit = False
                log.warning("IQFeed symbol limit -> capping watch-set to %d", _max_watch)
            target = _target_symbols(forced_syms, _max_watch)
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
        if (
            STALE_NBBO_RECONNECT_S > 0
            and watched
            and _last_nbbo_append_monotonic is not None
            and time.monotonic() - _last_nbbo_append_monotonic > STALE_NBBO_RECONNECT_S
        ):
            log.warning(
                "no valid IQFeed L1 NBBO frames for %.1fs across %d watched symbols; reconnecting",
                time.monotonic() - _last_nbbo_append_monotonic,
                len(watched),
            )
            running = False
            try:
                with sock_lock:
                    if sock is not None:
                        sock.close()
            except Exception:
                pass
            break
        with _pending_lock:
            rows = _pending[:]
            nbbo_rows = _pending_nbbo[:]
            _pending.clear()
            _pending_nbbo.clear()
        if nbbo_rows:
            latest_by_symbol = {}
            for r in nbbo_rows:
                latest_by_symbol[str(r["sym"]).upper()] = r
            nbbo_rows = list(latest_by_symbol.values())
        if rows or nbbo_rows:
            try:
                with engine.begin() as c:
                    notify_by_symbol = {}
                    if rows:
                        c.execute(INS, rows)
                        for r in rows:
                            notify_by_symbol[str(r["sym"]).upper()] = r["at"]
                    if WRITE_NBBO_TAPE and nbbo_rows:
                        c.execute(NBBO_INS, nbbo_rows)
                        for r in nbbo_rows:
                            notify_by_symbol[str(r["sym"]).upper()] = r["at"]
                    if IQFEED_NOTIFY_ENABLED and notify_by_symbol:
                        for sym, at in notify_by_symbol.items():
                            c.execute(
                                NOTIFY_IQFEED_TICK,
                                {
                                    "channel": IQFEED_NOTIFY_CHANNEL,
                                    "payload": json.dumps(
                                        {
                                            "symbol": sym,
                                            "observed_at": at.isoformat() if hasattr(at, "isoformat") else str(at),
                                            "source": "iqfeed_l1",
                                        },
                                        separators=(",", ":"),
                                    ),
                                },
                            )
            except Exception as e:
                log.warning("trade/nbbo insert failed (%d trade rows, %d nbbo rows): %s", len(rows), len(nbbo_rows), e)
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
    global sock, running, _last_nbbo_append_monotonic
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    forced = {a.upper() for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()}
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.monotonic() + float(sys.argv[sys.argv.index("--seconds") + 1])
    log.info(
        "IQFeed L1 bridge config: flush=%.3fs nbbo_tape=%s notify=%s channel=%s skip_startup_ddl=%s",
        FLUSH_INTERVAL_S,
        WRITE_NBBO_TAPE,
        IQFEED_NOTIFY_ENABLED,
        IQFEED_NOTIFY_CHANNEL,
        SKIP_STARTUP_DDL,
    )
    if not SKIP_STARTUP_DDL:
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
            _last_nbbo_append_monotonic = None
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
