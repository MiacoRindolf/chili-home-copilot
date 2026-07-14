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

import hashlib
import json
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, time as _dtime, timedelta, timezone
from pathlib import Path

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
# One latest quote row per symbol per flush bounds tape growth while still keeping
# the execution-age contract below two seconds. Operators can lower this explicitly.
FLUSH_INTERVAL_S = float(os.environ.get("IQFEED_TRADE_FLUSH_INTERVAL_S", "1.0") or 1.0)
REFRESH_S = 20.0                         # live-session symbol refresh cadence
STALE_NBBO_RECONNECT_S = float(
    os.environ.get("IQFEED_STALE_NBBO_RECONNECT_SECONDS", "45") or 45
)
STICKY_RESUBSCRIBE = os.environ.get("CHILI_IQFEED_STICKY_RESUBSCRIBE", "1") != "0"
IQFEED_NOTIFY_ENABLED = os.environ.get("IQFEED_NOTIFY_ENABLED", "1").strip().lower() not in (
    "0", "false", "no",
)
IQFEED_NOTIFY_CHANNEL = (
    os.environ.get("IQFEED_NOTIFY_CHANNEL", "momentum_iqfeed_l1").strip()
    or "momentum_iqfeed_l1"
)
BRIDGE_VERSION = "iqfeed-l1-quote-provenance-v2"
AUTHORITATIVE_TIMESTAMP_BASIS = "iqfeed_q_receive_trade_reference_fenced"
AUTHORITATIVE_MAX_AGE_S = 2.0
AUTHORITATIVE_FUTURE_TOLERANCE_S = 1.0
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
READER_JOIN_TIMEOUT_S = 5.0


def _bridge_build_id(path: str | Path = __file__) -> str:
    """Runtime-identifiable source build; persisted with every bridge row."""
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        digest = "source-unreadable"
    return f"{BRIDGE_VERSION}+sha256:{digest}"


BRIDGE_BUILD = _bridge_build_id()
BRIDGE_RUN_ID = str(uuid.uuid4())
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
    source VARCHAR(24) NOT NULL DEFAULT 'iqfeed_l1',
    provider_event_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    timestamp_basis VARCHAR(48),
    bridge_version VARCHAR(96),
    provider_trade_reference_at TIMESTAMPTZ,
    message_type VARCHAR(1),
    bridge_run_id VARCHAR(36),
    connection_generation BIGINT
);
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS provider_event_at TIMESTAMPTZ;
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS timestamp_basis VARCHAR(48);
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS bridge_version VARCHAR(96);
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS provider_trade_reference_at TIMESTAMPTZ;
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS message_type VARCHAR(1);
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS bridge_run_id VARCHAR(36);
ALTER TABLE iqfeed_trade_ticks ADD COLUMN IF NOT EXISTS connection_generation BIGINT;
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
    "INSERT INTO iqfeed_trade_ticks "
    "(symbol, observed_at, price, size, bid, ask, provider_event_at, received_at, "
    "timestamp_basis, bridge_version, provider_trade_reference_at, message_type, "
    "bridge_run_id, connection_generation) "
    "VALUES (:sym, :at, :px, :sz, :bid, :ask, :provider_at, :received_at, :basis, "
    ":bridge, :provider_trade_reference_at, :message_type, :bridge_run_id, "
    ":connection_generation)"
)

# Also feed the momentum ENTRY-GATE's NBBO freshness tape with this SAME tick-level IQFeed L1.
# The entry gate reads momentum_nbbo_spread_tape for its stale_bbo freshness check, but that
# table was fed ONLY by the slower/sparser Massive WS recorder — so the fresh tick-level IQFeed
# quotes (this bridge) NEVER reached the entry decision and wide-spread movers false-blocked on
# stale_bbo (VNTG had 1578 IQFeed ticks/5min @1s old, yet the gate saw a 10-270s WS quote).
# Mirror each valid-quote tick into the tape (source='iqfeed_l1') so the gate uses the freshest
# available quote. Default ON; IQFEED_WRITE_NBBO_TAPE=0 reverts.
WRITE_NBBO_TAPE = os.environ.get("IQFEED_WRITE_NBBO_TAPE", "1").strip().lower() not in ("0", "false", "no")
# Research trade rows retain the IQFeed Most-Recent-Trade-Time reference when it can be
# parsed. There is deliberately no receive-time fallback: an unparseable/replayed frame
# must never be made fresh merely because this process just received it.
OBSERVED_AT_TRADE_TIME = (
    os.environ.get("IQFEED_OBSERVED_AT_TRADE_TIME", "1").strip().lower() not in ("0", "false", "no")
)
NBBO_INS = sa.text(
    "INSERT INTO momentum_nbbo_spread_tape "
    "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source, "
    "provider_event_at, received_at, timestamp_basis, bridge_version, "
    "provider_trade_reference_at, message_type, bridge_run_id, connection_generation) "
    "VALUES (:sym, :at, :bid, :ask, :mid, :spread_bps, NULL, 'iqfeed_l1', "
    ":provider_at, :received_at, :basis, :bridge, :provider_trade_reference_at, "
    ":message_type, :bridge_run_id, :connection_generation)"
)
NOTIFY_IQFEED_TICK = sa.text("SELECT pg_notify(:channel, :payload)")

def _trade_time_to_naive_utc(last_t: str, now_utc: datetime) -> datetime | None:
    """Convert an IQFeed Most-Recent-Trade-Time field into a NAIVE-UTC datetime (matching the
    table's TIMESTAMP-without-tz, UTC-stored convention). IQFeed's default 6.2 layout sends the
    trade time as a US/Eastern *time-of-day* ('HH:MM:SS' or 'HH:MM:SS.ffffff') with NO date, so
    we anchor it to TODAY in ET. Handles the midnight-rollover edge (a tick whose ET time-of-day
    is far in the future of the current ET time-of-day belongs to the prior ET day). Returns None
    when unparseable / no tz DB so the caller can fail closed.
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


def _notify_payload(row: dict) -> str:
    """Deterministic v2 authority envelope for the app-side dispatcher."""
    received_at = row.get("received_at")
    provider_at = row.get("provider_at")
    provider_trade_reference_at = row.get("provider_trade_reference_at")
    return json.dumps(
        {
            "symbol": str(row.get("sym") or "").upper(),
            "observed_at": (
                provider_trade_reference_at.isoformat()
                if isinstance(provider_trade_reference_at, datetime)
                else None
            ),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "received_at": (
                received_at.isoformat()
                if isinstance(received_at, datetime)
                else None
            ),
            "provider_event_at": (
                provider_at.isoformat()
                if isinstance(provider_at, datetime)
                else None
            ),
            "provider_trade_reference_at": (
                provider_trade_reference_at.isoformat()
                if isinstance(provider_trade_reference_at, datetime)
                else None
            ),
            "timestamp_basis": str(row.get("basis") or ""),
            "source": "iqfeed_l1",
            "bridge_version": str(row.get("bridge") or BRIDGE_BUILD),
            "message_type": str(row.get("message_type") or ""),
            "bridge_run_id": str(row.get("bridge_run_id") or ""),
            "connection_generation": row.get("connection_generation"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


_pending: list[dict] = []
_pending_nbbo: list[dict] = []
_pending_lock = threading.Lock()
_last_trade: dict[str, str] = {}        # symbol -> last seen Most-Recent-Trade-Time (dedup key)
watched: set[str] = set()
_max_watch = WATCH_HARD_MAX             # adaptive watch cap; halved on an IQFeed limit signal, floored at WATCH_FLOOR
_limit_hit = False                      # set by the reader thread when IQFeed signals a symbol limit
sock_lock = threading.Lock()
_connection_state_lock = threading.Lock()
_active_connection_generation = 0
_last_nbbo_append_monotonic: float | None = None
_connection_generation = 0


class _ReaderQuiescenceError(RuntimeError):
    pass


def _begin_connection_generation() -> int:
    global _connection_generation, _active_connection_generation
    with _connection_state_lock:
        _connection_generation += 1
        _active_connection_generation = _connection_generation
        return _connection_generation


def _activate_connection_generation(connection_generation: int) -> None:
    global _active_connection_generation
    with _connection_state_lock:
        _active_connection_generation = int(connection_generation)


def _connection_generation_active(
    connection_generation: int,
    stop_event: threading.Event,
) -> bool:
    with _connection_state_lock:
        return bool(
            _active_connection_generation == int(connection_generation)
            and not stop_event.is_set()
        )


def _request_connection_stop(
    connection_generation: int,
    stop_event: threading.Event,
) -> bool:
    """Stop only the writer owned by the currently active socket generation."""
    with _connection_state_lock:
        if _active_connection_generation != int(connection_generation):
            return False
        stop_event.set()
        return True


def _retire_connection_generation(connection_generation: int) -> None:
    global _active_connection_generation
    with _connection_state_lock:
        if _active_connection_generation == int(connection_generation):
            _active_connection_generation = 0


def _close_connection_socket(connection_socket: socket.socket) -> None:
    try:
        connection_socket.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    try:
        connection_socket.close()
    except OSError:
        pass


def _send(connection_socket: socket.socket, cmd: str) -> None:
    with sock_lock:
        connection_socket.sendall((cmd + "\r\n").encode())


def _live_symbols() -> set[str]:
    try:
        with engine.connect() as c:
            rows = c.execute(sa.text(
                "SELECT DISTINCT symbol FROM trading_automation_sessions "
                "WHERE mode='live' AND symbol NOT LIKE '%-%' AND state IN "
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
                "  WHERE symbol NOT LIKE '%-%' AND (live_eligible OR paper_eligible) "
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
                "    AND symbol NOT LIKE '%-%' "
                "  GROUP BY symbol"
                ") q ORDER BY freshest DESC"
            ), {"w": float(fresh_window_s)}).fetchall()
        return [str(r[0]).upper() for r in rows]
    except Exception as e:
        log.debug("alert-subscribe query failed: %s", e)
        return []


def _parse_l1(line: str, *, connection_generation: int | None = None) -> None:
    """Parse a Level-1 frame without turning a replay into quote authority.

    Only a post-connect ``Q`` update whose receive time is causally fenced to the
    provider's Most-Recent-Trade-Time reference may enter the authoritative NBBO
    queue. ``P`` summaries and stale/unparseable Q frames are dropped because the
    trade table also has generic live consumers. The reference is a containment
    proxy, not a quote event time, so ``provider_event_at`` remains NULL everywhere.
    """
    global _last_nbbo_append_monotonic
    p = line.split(",")
    if len(p) <= L1_ASK:
        return
    try:
        message_type = str(p[0] or "").strip().upper()
        if message_type not in ("Q", "P"):
            return
        sym = p[1].strip().upper()
        if (
            EQUITY_SYMBOL_RE.fullmatch(sym) is None
            or sym.endswith(".")
            or ".." in sym
        ):
            return
        generation = int(
            _connection_generation
            if connection_generation is None
            else connection_generation
        )
        if generation <= 0:
            return
        last_t = p[L1_TIME].strip()
        bid = float(p[L1_BID]) if p[L1_BID].strip() else None
        ask = float(p[L1_ASK]) if p[L1_ASK].strip() else None
        received_at = datetime.now(timezone.utc)
        parsed_reference = _trade_time_to_naive_utc(last_t, received_at)
        provider_trade_reference_at = (
            parsed_reference.replace(tzinfo=timezone.utc)
            if parsed_reference is not None
            else None
        )
        receive_reference_delta = (
            (received_at - provider_trade_reference_at).total_seconds()
            if provider_trade_reference_at is not None
            else None
        )
        causally_fresh_q = bool(
            message_type == "Q"
            and provider_trade_reference_at is not None
            and receive_reference_delta is not None
            and -AUTHORITATIVE_FUTURE_TOLERANCE_S
            <= receive_reference_delta
            <= AUTHORITATIVE_MAX_AGE_S
        )

        # A P summary, stale replay, or unparseable Q may never create an
        # authoritative tape row or notification. observed_at deliberately uses
        # the provider trade reference so the existing large-tape sort cannot
        # make buffered data look new.
        if (
            causally_fresh_q
            and bid is not None
            and ask is not None
            and math.isfinite(bid)
            and math.isfinite(ask)
            and bid > 0
            and ask >= bid
        ):
            mid = (bid + ask) / 2.0
            with _pending_lock:
                _pending_nbbo.append({
                    "sym": sym,
                    "at": provider_trade_reference_at.replace(tzinfo=None),
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_bps": (ask - bid) / mid * 10_000.0,
                    "provider_at": None,
                    "provider_trade_reference_at": provider_trade_reference_at,
                    "received_at": received_at,
                    "basis": AUTHORITATIVE_TIMESTAMP_BASIS,
                    "bridge": BRIDGE_BUILD,
                    "message_type": message_type,
                    "bridge_run_id": BRIDGE_RUN_ID,
                    "connection_generation": generation,
                })
            _last_nbbo_append_monotonic = time.monotonic()

        # The trade table has generic live recent-window consumers, so P/stale
        # frames cannot safely be labelled "research-only" there. Persist only
        # the same causally fresh post-connect Q class; everything else is dropped.
        if not causally_fresh_q:
            return
        if not last_t or _last_trade.get(sym) == last_t:
            return                              # quote-only update or duplicate trade
        px = float(p[L1_LAST] or 0)
        sz = float(p[L1_SIZE] or 0)
        if not math.isfinite(px) or not math.isfinite(sz) or px <= 0 or sz <= 0:
            return
        # Research trade rows also fail closed on an unparseable reference. A
        # receive-time fallback would allow generic recent-window consumers to
        # treat an old summary as a fresh print.
        if not OBSERVED_AT_TRADE_TIME or provider_trade_reference_at is None:
            return
        _last_trade[sym] = last_t
        row = {
            "sym": sym,
            "at": provider_trade_reference_at.replace(tzinfo=None),
            "px": px,
            "sz": sz,
            "bid": bid,
            "ask": ask,
            "provider_at": None,
            "provider_trade_reference_at": provider_trade_reference_at,
            "received_at": received_at,
            "basis": "iqfeed_trade_reference_date_inferred",
            "bridge": BRIDGE_BUILD,
            "message_type": message_type,
            "bridge_run_id": BRIDGE_RUN_ID,
            "connection_generation": generation,
        }
        with _pending_lock:
            _pending.append(row)
    except (TypeError, ValueError, IndexError):
        return


def reader(
    connection_socket: socket.socket,
    stop_event: threading.Event,
    connection_generation: int,
) -> None:
    global _limit_hit
    buf = b""
    seen = 0
    while _connection_generation_active(connection_generation, stop_event):
        try:
            chunk = connection_socket.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            log.warning("server closed connection")
            break
        if not _connection_generation_active(connection_generation, stop_event):
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            c0 = line[0]
            if c0 in ("Q", "P"):                # Level-1 update / summary -> trade detect
                _parse_l1(line, connection_generation=connection_generation)
                seen += 1
            elif c0 == "T":                     # timestamp heartbeat (NOT a trade)
                continue
            elif line.startswith("S,") or c0 in ("n", "E"):
                if any(k in line.upper() for k in ("SYMBOL LIMIT", "MAX SYMBOL", "LIMIT REACHED", "TOO MANY SYMBOL")):
                    if _connection_generation_active(connection_generation, stop_event):
                        _limit_hit = True            # current writer halves its watch-set on this signal
                    log.warning("IQFeed symbol-limit signal: %s", line[:160])
                else:
                    log.info("feed: %s", line[:160])
    _request_connection_stop(connection_generation, stop_event)


def writer(
    forced_syms: set[str],
    deadline: float | None,
    connection_socket: socket.socket,
    stop_event: threading.Event,
    connection_generation: int,
) -> None:
    global _max_watch, _limit_hit
    last_refresh = 0.0
    last_prune = 0.0
    last_fast_sub = 0.0
    while (
        _connection_generation_active(connection_generation, stop_event)
        and (deadline is None or time.monotonic() < deadline)
    ):
        if stop_event.wait(FLUSH_INTERVAL_S):
            break
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
                    _send(connection_socket, f"w{sym}")
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
                _send(connection_socket, f"w{sym}")  # Level-1 watch -> Q/P updates with Last + Last Size
                watched.add(sym)
                log.info("watching trades: %s", sym)
            if STICKY_RESUBSCRIBE:
                for sym in (target & watched):
                    _send(connection_socket, f"w{sym}")  # re-send so a silent IQFeed drop self-heals
            for sym in watched - target:
                _send(connection_socket, f"r{sym}")  # unwatch
                watched.discard(sym)
                _last_trade.pop(sym, None)
            last_refresh = time.monotonic()

        # A connected socket with watched symbols but no valid BBO frames is not a
        # healthy market-data path. Reconnect so a half-open IQConnect session cannot
        # make an old quote look like the best available execution input indefinitely.
        if (
            STALE_NBBO_RECONNECT_S > 0
            and watched
            and _last_nbbo_append_monotonic is not None
            and time.monotonic() - _last_nbbo_append_monotonic > STALE_NBBO_RECONNECT_S
        ):
            log.warning(
                "no valid IQFeed L1 BBO frames for %.1fs across %d watched symbols; reconnecting",
                time.monotonic() - _last_nbbo_append_monotonic,
                len(watched),
            )
            _request_connection_stop(connection_generation, stop_event)
            _close_connection_socket(connection_socket)
            break

        with _pending_lock:
            rows = _pending[:]
            nbbo_rows = _pending_nbbo[:]
            _pending.clear()
            _pending_nbbo.clear()
        # Keep quote-level freshness without storing every redundant frame in a flush
        # interval: the newest valid frame for each symbol is the causal BBO row.
        if nbbo_rows:
            newest_by_symbol: dict[str, dict] = {}
            for row in nbbo_rows:
                newest_by_symbol[str(row["sym"]).upper()] = row
            nbbo_rows = list(newest_by_symbol.values())
        if rows or nbbo_rows:
            try:
                with engine.begin() as c:
                    if rows:
                        c.execute(INS, rows)
                    if WRITE_NBBO_TAPE and nbbo_rows:
                        c.execute(NBBO_INS, nbbo_rows)
                        if IQFEED_NOTIFY_ENABLED:
                            for row in nbbo_rows:
                                c.execute(
                                    NOTIFY_IQFEED_TICK,
                                    {
                                        "channel": IQFEED_NOTIFY_CHANNEL,
                                        "payload": _notify_payload(row),
                                    },
                                )
            except Exception as e:
                log.warning(
                    "trade/BBO insert failed (%d trade, %d BBO rows): %s",
                    len(rows),
                    len(nbbo_rows),
                    e,
                )
    _request_connection_stop(connection_generation, stop_event)


def _selftest() -> int:
    """Verify the DB path WITHOUT IQFeed: write a synthetic row, read it back."""
    with engine.begin() as c:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))
    now_aware = datetime.now(timezone.utc)
    now = now_aware.replace(tzinfo=None)
    with engine.begin() as c:
        c.execute(INS, [{
            "sym": "_SELFTEST",
            "at": now,
            "px": 1.23,
            "sz": 100.0,
            "bid": 1.22,
            "ask": 1.24,
            "provider_at": None,
            "provider_trade_reference_at": now_aware,
            "received_at": now_aware,
            "basis": "iqfeed_trade_reference_date_inferred",
            "bridge": BRIDGE_BUILD,
            "message_type": "Q",
            "bridge_run_id": BRIDGE_RUN_ID,
            "connection_generation": 1,
        }])
    with engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM iqfeed_trade_ticks WHERE symbol='_SELFTEST'")).scalar()
        c2 = c.execute(sa.text("DELETE FROM iqfeed_trade_ticks WHERE symbol='_SELFTEST'"))  # noqa: F841
    log.info("selftest: wrote+read %s synthetic row(s), table OK", n)
    return 0 if n and n >= 1 else 1


def _run_connection(forced: set[str], deadline: float | None) -> None:
    """Own one socket generation through close and reader quiescence.

    A subsequent connection may not be created until this function has closed the
    concrete socket and joined its reader. If close cannot retire the reader within
    the bounded interval, raise a terminal error so ``main`` refuses to rebind.
    """
    global _last_nbbo_append_monotonic
    connection_socket = socket.create_connection((HOST, PORT), timeout=10)
    stop_event = threading.Event()
    reader_thread: threading.Thread | None = None
    connection_generation = _begin_connection_generation()
    try:
        connection_socket.settimeout(2.0)
        watched.clear()
        _last_trade.clear()
        _last_nbbo_append_monotonic = time.monotonic()
        _send(
            connection_socket,
            "S,SET PROTOCOL,6.2",
        )  # default update layout (no SELECT -> see L1_* indices)
        log.info(
            "connected to IQConnect %s:%s (L1 trades, protocol 6.2) run=%s generation=%d",
            HOST,
            PORT,
            BRIDGE_RUN_ID,
            connection_generation,
        )
        reader_thread = threading.Thread(
            target=reader,
            args=(connection_socket, stop_event, connection_generation),
            daemon=True,
            name=f"iqfeed-trade-reader-g{connection_generation}",
        )
        reader_thread.start()
        writer(
            forced,
            deadline,
            connection_socket,
            stop_event,
            connection_generation,
        )
    finally:
        _request_connection_stop(connection_generation, stop_event)
        _close_connection_socket(connection_socket)
        if reader_thread is not None:
            reader_thread.join(timeout=READER_JOIN_TIMEOUT_S)
        reader_quiesced = bool(
            reader_thread is None or not reader_thread.is_alive()
        )
        _retire_connection_generation(connection_generation)
        if not reader_quiesced:
            raise _ReaderQuiescenceError(
                "IQFeed reader did not quiesce after socket close; refusing reconnect"
            )


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    forced = {a.upper() for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()}
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.monotonic() + float(sys.argv[sys.argv.index("--seconds") + 1])
    log.info(
        "IQFeed L1 bridge build=%s flush=%.3fs nbbo_tape=%s notify=%s channel=%s",
        BRIDGE_BUILD,
        FLUSH_INTERVAL_S,
        WRITE_NBBO_TAPE,
        IQFEED_NOTIFY_ENABLED,
        IQFEED_NOTIFY_CHANNEL,
    )
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
            _run_connection(forced, deadline)
        except KeyboardInterrupt:
            break
        except _ReaderQuiescenceError as e:
            log.critical("bridge terminal reconnect refusal: %s", e)
            break
        except Exception as e:
            log.warning("bridge error: %s", e)
        if deadline is not None and time.monotonic() >= deadline:
            break
        log.info("reconnecting in 10s…")
        time.sleep(10)
    log.info("trade bridge stopped")


if __name__ == "__main__":
    main()
