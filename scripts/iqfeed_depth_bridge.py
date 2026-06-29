"""IQFeed Level 2 depth bridge — host-side daemon feeding CHILI real order-book depth.

IQConnect binds 127.0.0.1 only, so this runs ON THE WINDOWS HOST (chili-env
python) and bridges into CHILI through Postgres (the boundary containers
already share). Flow:

  IQConnect :9200 --(type-6 per-venue price-level frames)--> in-memory books
      --> top-of-book + 5-level aggregates + signed imbalance
      --> iqfeed_depth_snapshots rows every SNAP_INTERVAL_S per symbol

Symbols tracked = the momentum lane's runnable LIVE equity sessions (same
query the in-app trackers use), polled every REFRESH_S. The app-side consumer
(`_live_book_imbalance`) prefers a fresh row here over L1 displayed sizes —
same Phase 4a viability rules, deeper data.

Frame format observed 2026-06-11 (protocol 6.2, legacy `w` watch):
  6,SYMBOL,,MMID,SIDE,PRICE,SIZE,,4,HH:MM:SS.ffffff,YYYY-MM-DD,
MMID = venue (ARCX/BATS/EDGX/MEMX/...); SIDE = A|B; one row per venue+side
(each venue shows its best level) — the aggregate across venues approximates
the consolidated top of book; depth-N = best N venue levels per side.

Usage: python scripts/iqfeed_depth_bridge.py [--seconds N] [SYM ...]
  (no SYM args -> track live-lane symbols from the DB; N for bounded soaks)
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iqfeed_bridge")

HOST, PORT = "127.0.0.1", 9200
DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
SNAP_INTERVAL_S = 2.0     # per-symbol snapshot write cadence
REFRESH_S = 20.0          # live-session symbol refresh cadence
# STICKY RE-SUBSCRIBE (2026-06-16, the LION dead-L2 bug): IQFeed silently drops a
# per-symbol depth subscription. The loop only sent WOR/WPL on FIRST-SEEN, so once a
# held name was in ``watched`` it was never re-sent — LION's book went dark at 18:17
# while the position was held to 20:17 (33 other names kept streaming), blinding the
# exit (n_snaps=0 -> sell_into_strength gated 'stale_or_thin' 1111x). Re-send the depth
# subscription for every live-session name each refresh so a silent drop self-heals.
# Idempotent (deduped by the venue book). =0 reverts to first-seen-only.
STICKY_RESUBSCRIBE = os.environ.get("CHILI_IQFEED_STICKY_RESUBSCRIBE", "1") != "0"
DEPTH_LEVELS = 5
STALE_VENUE_ROW_S = 900.0  # drop venue levels not refreshed in 15min (overnight ghosts)

# BROADEN COVERAGE (2026-06-29, the 6/390 L2-starvation fix): the bridge used to watch ONLY the
# live-session names (~6) -> OFI/micro-price/L2-confirm/exit-ladder no-opped on ~98% of the universe.
# Mirror the trade bridge: also watch the fresh ELIGIBLE-MOVER universe (ranked by viability_score)
# so depth exists DURING candidate eval + entry, not just after arming. The L2 watch cap is
# SELF-DISCOVERED (start at HARD_MAX, HALVE on an IQFeed symbol-limit signal, floored at FLOOR) —
# no need to know the plan's L2 limit up front (the rail-governor pattern). L2 is ~3 subscriptions
# per name (the WOR/WPL/w trio) so FLOOR/HARD_MAX are set below the L1 trade bridge's. One documented
# base (DEPTH_WATCH_FLOOR); the cap is adaptive. HARD_MAX=0 reverts to session-only (byte-identical).
ELIGIBLE_FRESH_S = float(os.environ.get("CHILI_IQFEED_DEPTH_ELIGIBLE_FRESH_SECONDS", "1800") or 1800)
DEPTH_WATCH_FLOOR = int(os.environ.get("CHILI_IQFEED_DEPTH_WATCH_FLOOR", "48") or 48)   # the ONE documented base
DEPTH_WATCH_HARD_MAX = int(os.environ.get("CHILI_IQFEED_DEPTH_WATCH_MAX", "128") or 128)

engine = sa.create_engine(DB_URL, pool_pre_ping=True)

DDL = """
CREATE TABLE IF NOT EXISTS iqfeed_depth_snapshots (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    bid_top DOUBLE PRECISION, ask_top DOUBLE PRECISION,
    bid_top_size DOUBLE PRECISION, ask_top_size DOUBLE PRECISION,
    bid5_size DOUBLE PRECISION, ask5_size DOUBLE PRECISION,
    imbalance5 DOUBLE PRECISION,
    venues INT,
    source VARCHAR(24) NOT NULL DEFAULT 'iqfeed_l2'
);
CREATE INDEX IF NOT EXISTS ix_iqfeed_depth_sym_at ON iqfeed_depth_snapshots (symbol, observed_at DESC);
"""


class Book:
    """Per-symbol venue book: {(venue, side): (price, size, mono_ts)}."""

    def __init__(self) -> None:
        self.levels: dict[tuple[str, str], tuple[float, float, float]] = {}

    def update(self, venue: str, side: str, price: float, size: float) -> None:
        if price <= 0 or size < 0:
            return
        self.levels[(venue, side)] = (price, size, time.monotonic())

    def snapshot(self) -> dict | None:
        now = time.monotonic()
        bids, asks = [], []
        for (venue, side), (px, sz, ts) in self.levels.items():
            if now - ts > STALE_VENUE_ROW_S or sz <= 0:
                continue
            (bids if side == "B" else asks).append((px, sz))
        if not bids or not asks:
            return None
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        if bids[0][0] >= asks[0][0] * 1.05:  # crossed >5% = ghost venue rows
            return None
        b5 = sum(sz for _, sz in bids[:DEPTH_LEVELS])
        a5 = sum(sz for _, sz in asks[:DEPTH_LEVELS])
        tot = b5 + a5
        return {
            "bid_top": bids[0][0], "ask_top": asks[0][0],
            "bid_top_size": bids[0][1], "ask_top_size": asks[0][1],
            "bid5_size": b5, "ask5_size": a5,
            "imbalance5": round((b5 - a5) / tot, 4) if tot > 0 else None,
            "venues": len({v for (v, _s) in self.levels}),
        }


books: dict[str, Book] = defaultdict(Book)
books_lock = threading.Lock()
watched: set[str] = set()
sock_lock = threading.Lock()
sock: socket.socket | None = None
running = True
_max_watch = DEPTH_WATCH_HARD_MAX   # adaptive L2 watch cap; halved on an IQFeed symbol-limit signal, floored at DEPTH_WATCH_FLOOR
_limit_hit = False                  # set by the reader thread when IQFeed signals a depth-symbol limit


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
    """The fresh ELIGIBLE-MOVER universe ranked by explosiveness (viability_score), up to ``limit``,
    most-explosive first — the names being evaluated for entry, so OFI/micro-price/L2 signals have
    depth DURING eval, not only after arming. Mirrors the trade bridge. Empty outside market hours."""
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


def reader() -> None:
    global running, _limit_hit
    buf = b""
    frames = 0
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
            if frames < 12 and line and line[0] not in ("T",):
                log.info("raw[%d]: %s", frames, line[:140])
            if not line or line[0] in ("T",):
                continue
            if line[0] == "6":
                p = line.split(",")
                if len(p) >= 7:
                    try:
                        with books_lock:
                            books[p[1].upper()].update(p[3], p[4], float(p[5] or 0), float(p[6] or 0))
                        frames += 1
                    except (ValueError, IndexError):
                        pass
            elif line.startswith("S,") or line[0] in ("n", "E"):
                if any(k in line.upper() for k in ("SYMBOL LIMIT", "MAX SYMBOL", "LIMIT REACHED", "TOO MANY SYMBOL")):
                    _limit_hit = True   # adaptive cap: the writer halves the depth watch-set on this signal
                    log.warning("IQFeed depth symbol-limit signal: %s", line[:160])
                else:
                    log.info("feed: %s", line[:160])
    running = False


def writer(forced_syms: set[str], deadline: float | None) -> None:
    global running, _max_watch, _limit_hit
    last_refresh = 0.0
    last_prune = 0.0
    ins = sa.text(
        "INSERT INTO iqfeed_depth_snapshots (symbol, observed_at, bid_top, ask_top, "
        "bid_top_size, ask_top_size, bid5_size, ask5_size, imbalance5, venues) "
        "VALUES (:sym, :at, :bt, :at2, :bts, :ats, :b5, :a5, :imb, :v)"
    )
    while running and (deadline is None or time.monotonic() < deadline):
        time.sleep(SNAP_INTERVAL_S)
        if time.monotonic() - last_prune >= 3600.0:
            # retention prune (the exit_parity_log bloat lesson): depth snapshots
            # are a rolling research window, not an archive — keep 7 days
            try:
                with engine.begin() as c:
                    c.execute(sa.text(
                        "DELETE FROM iqfeed_depth_snapshots "
                        "WHERE observed_at < (now() at time zone 'utc') - interval '7 days'"))
            except Exception as e:
                log.debug("retention prune failed: %s", e)
            last_prune = time.monotonic()
        if time.monotonic() - last_refresh >= REFRESH_S:
            # adaptive L2 watch cap: self-discover the plan's depth-symbol limit (halve on an IQFeed
            # symbol-limit signal, floored) — mirror the trade bridge, no magic up-front limit.
            if _limit_hit:
                _max_watch = max(DEPTH_WATCH_FLOOR, _max_watch // 2)
                _limit_hit = False
                log.warning("depth watch cap halved -> %d (IQFeed L2 symbol-limit)", _max_watch)
            # ALWAYS watch the live-session names (held positions need exit L2 — they must never go
            # dark); fill the rest of the budget with the most-explosive ELIGIBLE movers so depth
            # exists DURING candidate eval, not only after arming.
            if forced_syms:
                sticky = forced_syms
                target = forced_syms
            else:
                sticky = _live_symbols()
                elig = set(_eligible_symbols(max(0, _max_watch - len(sticky))))
                target = sticky | elig
            fresh = target - watched
            for sym in fresh:
                # all three dialects like the validated probe: MBO (WOR), price-level
                # (WPL), legacy market-maker (w) — the burst observed 2026-06-11 came
                # off this trio; harmless duplicates are deduped by the venue book
                _send(f"WOR,{sym}")
                _send(f"WPL,{sym}")
                _send(f"w{sym}")
                watched.add(sym)
                log.info("watching depth: %s", sym)
            # STICKY RE-SUBSCRIBE (LION dead-L2 fix): re-send the depth subscription for every
            # live-SESSION name each refresh (not just first-seen) so a silent IQFeed per-symbol
            # drop self-heals — a held position's exit must not go blind. The broader eligible set
            # uses first-seen-only (no re-subscribe storm / extra budget pressure on non-held names).
            if STICKY_RESUBSCRIBE and sticky:
                with books_lock:
                    _dark = [s for s in sticky if s not in books or books[s].snapshot() is None]
                for sym in (sticky - fresh):
                    _send(f"WOR,{sym}")
                    _send(f"WPL,{sym}")
                    _send(f"w{sym}")
                if _dark:
                    log.warning("depth re-subscribe (book empty): %s", ",".join(sorted(_dark)))
            for sym in watched - target:
                _send(f"ROR,{sym}")
                _send(f"RPL,{sym}")
                _send(f"r{sym}")
                watched.discard(sym)
                with books_lock:
                    books.pop(sym, None)
            last_refresh = time.monotonic()
        rows = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with books_lock:
            items = [(s, b.snapshot()) for s, b in books.items()]
        for sym, snap in items:
            if snap is None:
                continue
            rows.append({"sym": sym, "at": now, "bt": snap["bid_top"], "at2": snap["ask_top"],
                         "bts": snap["bid_top_size"], "ats": snap["ask_top_size"],
                         "b5": snap["bid5_size"], "a5": snap["ask5_size"],
                         "imb": snap["imbalance5"], "v": snap["venues"]})
        if rows:
            try:
                with engine.begin() as c:
                    c.execute(ins, rows)
            except Exception as e:
                log.warning("snapshot insert failed (%d rows): %s", len(rows), e)
    running = False


def main() -> None:
    global sock, running
    forced = {a.upper() for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()}
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.monotonic() + float(sys.argv[sys.argv.index("--seconds") + 1])
    with engine.begin() as c:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))
    # Reconnect loop: IQConnect restarts (relogin, updates) must not kill the
    # bridge — drop state, reconnect, re-watch (watched set resets so the
    # refresh pass re-subscribes everything).
    while deadline is None or time.monotonic() < deadline:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=10)
            sock.settimeout(2.0)
            running = True
            watched.clear()
            with books_lock:
                books.clear()
            _send("S,SET PROTOCOL,6.2")
            log.info("connected to IQConnect %s:%s (protocol 6.2)", HOST, PORT)
            t = threading.Thread(target=reader, daemon=True, name="iqfeed-reader")
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
    log.info("bridge stopped")


if __name__ == "__main__":
    main()
