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
from datetime import datetime, time as _dtime, timedelta, timezone

try:  # stdlib tz DB (3.9+); used to anchor the IQFeed time-of-day to US/Eastern
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo always present on 3.11, defensive only
    _ET = None

import sqlalchemy as sa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iqfeed_trade_bridge")

HOST, PORT = "127.0.0.1", 5009          # IQConnect Level-1 STREAMING port (:9100=lookup, :9200=L2 depth)
DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
FLUSH_INTERVAL_S = 1.0                   # batch-insert cadence
REFRESH_S = 20.0                         # live-session symbol refresh cadence
STICKY_RESUBSCRIBE = os.environ.get("CHILI_IQFEED_STICKY_RESUBSCRIBE", "1") != "0"
# CAPTURE-G3: event-driven subscribe-on-first-alert. The app container writes a subscribe HINT
# to momentum_bridge_subscribe_requests the instant a name first ignites; this bridge FAST-POLLS
# that table (much shorter than REFRESH_S) and subscribes immediately, additively to the normal
# refresh set — closing the ~2.7-min Gate-0 blind window on sub-2-min squeezes (VWAV 2026-06-30).
SUBSCRIBE_ON_ALERT = os.environ.get("CHILI_MOMENTUM_BRIDGE_SUBSCRIBE_ON_ALERT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
SUBSCRIBE_FAST_POLL_S = float(os.environ.get("IQFEED_SUBSCRIBE_FAST_POLL_S", "3") or 3)   # first-alert -> subscribed target
SUBSCRIBE_FRESH_WINDOW_S = float(os.environ.get("IQFEED_SUBSCRIBE_FRESH_WINDOW_S", "180") or 180)  # honor only recent hints
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
"""

# CAPTURE-G3: the event-driven subscribe-on-first-alert coordination table (created here too so
# the bridge runs standalone; app-side it is migration 313). A pure subscription HINT, never a
# trading table. Idempotent.
SUBSCRIBE_DDL = """
CREATE TABLE IF NOT EXISTS momentum_bridge_subscribe_requests (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    requested_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    reason VARCHAR(32),
    source_node_id VARCHAR(80),
    correlation_id VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS ix_mbsr_requested_at ON momentum_bridge_subscribe_requests (requested_at DESC);
CREATE INDEX IF NOT EXISTS ix_mbsr_requested_id ON momentum_bridge_subscribe_requests (requested_at, id);
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
# OBSERVED_AT TRUTH (default ON): stamp observed_at with the IQFeed Most-Recent-Trade-Time
# (the actual print time, in US/Eastern) converted to UTC — NOT host wall-clock at insert. On
# an IQFeed reconnect the feed replays a BURST of buffered OLD ticks; stamping those with now()
# made stale ticks read as 1-2s-fresh, so the entry gate's stale_bbo freshness check trusted
# minutes-old quotes. Anchoring to the real trade time means a buffered-replay tick correctly
# ages out. Falls back to now() when the trade-time is unparseable. IQFEED_OBSERVED_AT_TRADE_TIME=0
# reverts to the host-clock behavior (byte-identical to pre-fix). Stored naive-UTC like before.
OBSERVED_AT_TRADE_TIME = (
    os.environ.get("IQFEED_OBSERVED_AT_TRADE_TIME", "1").strip().lower() not in ("0", "false", "no")
)
NBBO_INS = sa.text(
    "INSERT INTO momentum_nbbo_spread_tape "
    "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
    "VALUES (:sym, :at, :bid, :ask, :mid, :spread_bps, NULL, 'iqfeed_l1')"
)

def _trade_time_to_naive_utc(last_t: str, now_utc: datetime) -> datetime | None:
    """Convert an IQFeed Most-Recent-Trade-Time field into a NAIVE-UTC datetime (matching the
    table's TIMESTAMP-without-tz, UTC-stored convention). IQFeed's default 6.2 layout sends the
    trade time as a US/Eastern *time-of-day* ('HH:MM:SS' or 'HH:MM:SS.ffffff') with NO date, so
    we anchor it to TODAY in ET. Handles the midnight-rollover edge (a tick whose ET time-of-day
    is far in the future of the current ET time-of-day belongs to the prior ET day). Returns None
    when unparseable / no tz DB so the caller falls back to host wall-clock.
    (operator: data truth — a reconnect burst of OLD ticks must NOT read as fresh.)"""
    if _ET is None:
        return None
    s = (last_t or "").strip()
    if not s:
        return None
    tod: _dtime | None = None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            tod = datetime.strptime(s, fmt).time()
            break
        except ValueError:
            continue
    if tod is None:
        return None
    try:
        now_et = now_utc.astimezone(_ET)
        cand = datetime.combine(now_et.date(), tod, tzinfo=_ET)
        # Rollover guard: if the parsed time-of-day is meaningfully AHEAD of the current ET
        # time-of-day it must be from yesterday's ET session (e.g. 23:59 print read at 00:01).
        if cand - now_et > timedelta(hours=1):
            cand = cand - timedelta(days=1)
        return cand.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


_pending: list[dict] = []
_pending_lock = threading.Lock()
_last_trade: dict[str, str] = {}        # symbol -> last seen Most-Recent-Trade-Time (dedup key)
watched: set[str] = set()
_max_watch = WATCH_HARD_MAX             # adaptive watch cap; halved on an IQFeed limit signal, floored at WATCH_FLOOR
_limit_hit = False                      # set by the reader thread when IQFeed signals a symbol limit
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


def _alert_symbols(fresh_window_s: float) -> list[str]:
    """CAPTURE-G3 fast path: the symbols the app container flagged for IMMEDIATE subscription
    (first-alert hints written to momentum_bridge_subscribe_requests within the fresh window).
    Empty on any error / missing table so the bridge degrades to its normal poll cadence.

    F8 (capture-g fix): NEWEST-FIRST — ordered by each symbol's freshest hint DESC, mirroring
    the unit-tested select_fresh_subscribe_symbols contract (bridge_subscribe.py). The fast
    poll breaks at the adaptive watch cap, so ordering decides WHO gets the last slot: an
    unordered DISTINCT let an arbitrary/stale hint take it while the 2s-old igniting mover
    was skipped. Now the cap keeps the FRESHEST movers."""
    try:
        with engine.connect() as c:
            rows = c.execute(sa.text(
                "SELECT symbol FROM ("
                "  SELECT symbol, max(requested_at) AS freshest "
                "  FROM momentum_bridge_subscribe_requests "
                "  WHERE requested_at > (now() at time zone 'utc') - make_interval(secs => :w) "
                "    AND symbol NOT LIKE '%-USD' "
                "  GROUP BY symbol"
                ") q ORDER BY freshest DESC"
            ), {"w": float(fresh_window_s)}).fetchall()
        return [str(r[0]).upper() for r in rows]
    except Exception as e:
        log.debug("alert-subscribe query failed: %s", e)
        return []


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
        _now_utc = datetime.now(timezone.utc)
        _at = _now_utc.replace(tzinfo=None)
        if OBSERVED_AT_TRADE_TIME:
            _tt = _trade_time_to_naive_utc(last_t, _now_utc)
            if _tt is not None:
                _at = _tt
        row = {"sym": sym, "at": _at,
               "px": px, "sz": sz, "bid": bid, "ask": ask}
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
    last_prune = 0.0
    last_fast_sub = 0.0
    while running and (deadline is None or time.monotonic() < deadline):
        time.sleep(FLUSH_INTERVAL_S)
        # CAPTURE-G3 FAST PATH: subscribe first-alert names IMMEDIATELY (short poll), additive to
        # the slow REFRESH_S set below — closes the ~2.7-min Gate-0 blind window. Runs BEFORE the
        # slow refresh so a fresh mover is watched within ~SUBSCRIBE_FAST_POLL_S of its first alert.
        # Never unwatches (only the slow refresh reconciles the full target set); respects the
        # adaptive watch cap so it can't blow past an IQFeed symbol limit.
        if (
            SUBSCRIBE_ON_ALERT
            and not forced_syms                     # explicit CLI symbols -> no dynamic subscribe
            and time.monotonic() - last_fast_sub >= SUBSCRIBE_FAST_POLL_S
        ):
            last_fast_sub = time.monotonic()
            try:
                for sym in _alert_symbols(SUBSCRIBE_FRESH_WINDOW_S):
                    if sym in watched:
                        continue
                    if len(watched) >= _max_watch:  # respect the adaptive cap (rail-governor)
                        break
                    _send(f"w{sym}")
                    watched.add(sym)
                    log.info("watching trades (fast-alert): %s", sym)
            except Exception as e:
                log.debug("fast-alert subscribe failed: %s", e)
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
            # F7 (capture-g fix): drain the subscribe-hint coordination table too — hints are
            # only meaningful for the ~180s fresh window; without a sweep the table grew
            # unbounded (the mig313 docstring promised a retention sweep — this makes it
            # true). Own transaction + own try so a missing table (pre-mig-313 env) can
            # never roll back the tick prune above.
            try:
                with engine.begin() as c:
                    c.execute(sa.text(
                        "DELETE FROM momentum_bridge_subscribe_requests "
                        "WHERE requested_at < (now() at time zone 'utc') - make_interval(hours => 48)"))
            except Exception as e:
                log.debug("subscribe-requests prune failed: %s", e)
            last_prune = time.monotonic()
        if time.monotonic() - last_refresh >= REFRESH_S:
            if _limit_hit:                          # adaptive: IQFeed signalled its symbol limit -> back off
                _max_watch = max(WATCH_FLOOR, len(watched) // 2)
                _limit_hit = False
                log.warning("IQFeed symbol limit -> capping watch-set to %d", _max_watch)
            if forced_syms:
                target = forced_syms
            else:
                armed = _live_symbols()             # armed/live names: ALWAYS watched (load-bearing for live)
                # F4 (capture-g fix): UNION the fresh first-alert hints into the slow-refresh
                # target. Without this the refresh rebuilt target = armed | eligible-movers
                # ONLY, so a fast-subscribed alert name that missed the eligibility cut was
                # UNWATCHED <=20s after the 3s poll added it — then re-added on the next fast
                # poll (hint fresh 180s): a watch FLAP that left tape gaps mid-squeeze and,
                # via _last_trade.pop on unwatch, DUPLICATE prints on re-add. A fresh-hint
                # name now stays watched for its whole hint window.
                alerts = set(_alert_symbols(SUBSCRIBE_FRESH_WINDOW_S)) if SUBSCRIBE_ON_ALERT else set()
                base = armed | alerts
                room = max(0, _max_watch - len(base))
                movers = [s for s in _eligible_symbols(_max_watch) if s not in base][:room]
                target = base | set(movers)
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
        # CAPTURE-G3: ensure the subscribe-on-alert coordination table exists (standalone-safe).
        if SUBSCRIBE_ON_ALERT:
            for stmt in SUBSCRIBE_DDL.strip().split(";"):
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
