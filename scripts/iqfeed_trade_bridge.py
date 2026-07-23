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
in [-1,1]) -> captured as the meta-label feature `trade_flow`. Symbols include execution-relevant
LIVE and PAPER equity sessions (same query as the depth bridge), polled every REFRESH_S; sticky re-subscribe so
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
from typing import Any, Iterable

try:  # stdlib tz DB (3.9+); used to anchor the IQFeed time-of-day to US/Eastern
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo always present on 3.11, defensive only
    _ET = None

import sqlalchemy as sa

# Host invocation is normally ``python scripts/iqfeed_trade_bridge.py``. Python
# then places only ``scripts/`` (not the repository root) on sys.path; add the
# fixed source root so the existing Ross-universe package import is real rather
# than a silently failed optional path.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.iqfeed_subscription_policy import (
        ACTIVE_EXECUTION_SESSION_SQL,
        CoverageGap,
        SourceRead,
        SubscriptionConnectionIndeterminate,
        TargetCause,
        TargetResolution,
        active_capture_symbols,
        require_complete_source_inventory,
        resolve_subscription_target,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` host invocation
    from iqfeed_subscription_policy import (  # type: ignore[no-redef]
        ACTIVE_EXECUTION_SESSION_SQL,
        CoverageGap,
        SourceRead,
        SubscriptionConnectionIndeterminate,
        TargetCause,
        TargetResolution,
        active_capture_symbols,
        require_complete_source_inventory,
        resolve_subscription_target,
    )

try:
    from scripts.iqfeed_ignition_detector import (
        IgnitionConfig,
        IgnitionDetector,
        IgnitionFire,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` host invocation
    from iqfeed_ignition_detector import (  # type: ignore[no-redef]
        IgnitionConfig,
        IgnitionDetector,
        IgnitionFire,
    )

# ``ACTIVE_EXECUTION_SESSION_SQL`` owns the active-query equity exclusion
# (``symbol NOT LIKE '%-%'``); the eligible/hint queries retain their local
# copies below. The pure policy test binds all three defenses.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iqfeed_trade_bridge")


def _capture_bc(step: str) -> None:
    """Fsync a supervised-lane flight-recorder line to the captured-paper
    breadcrumb file. STRICTLY env-gated: a no-op in the standalone production
    bridge (env absent). Bare try/except + pure syscalls so it survives any
    lane death; the LAST line on disk names the exact sub-step reached
    (a69: service stderr was 0 bytes and readiness never completed, with no
    surviving evidence of where the lanes actually got to)."""
    try:
        path = os.environ.get("CHILI_CAPTURED_PAPER_BREADCRUMB_PATH")
        if not path:
            return
        line = f"{time.time()} pid={os.getpid()} lane[trade] {step}\n".encode(
            "utf-8", "replace"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass

HOST, PORT = "127.0.0.1", 5009          # IQConnect Level-1 STREAMING port (:9100=lookup, :9200=L2 depth)
DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
# One latest quote row per symbol per flush bounds tape growth while still keeping
# the execution-age contract below two seconds. Operators can lower this explicitly.
FLUSH_INTERVAL_S = float(os.environ.get("IQFEED_TRADE_FLUSH_INTERVAL_S", "1.0") or 1.0)
REFRESH_S = 20.0                         # execution-session symbol refresh cadence
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
BRIDGE_VERSION = "iqfeed-l1-exact-print-provenance-v3"
AUTHORITATIVE_TIMESTAMP_BASIS = "iqfeed_q_receive_trade_reference_fenced"
EXACT_PRINT_TIMESTAMP_BASIS = "iqfeed_selected_trade_date_timems_exact"
AUTHORITATIVE_MAX_AGE_S = 2.0
AUTHORITATIVE_FUTURE_TOLERANCE_S = 1.0
EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
READER_JOIN_TIMEOUT_S = 5.0
SELECTED_FIELDS_ACK_TIMEOUT_S = 2.0
UNCAPTURED_DIAGNOSTIC_FLAG = "--allow-uncaptured-diagnostic"


def _bridge_build_id(path: str | Path = __file__) -> str:
    """Runtime-identifiable source build; persisted with every bridge row."""
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        digest = "source-unreadable"
    return f"{BRIDGE_VERSION}+sha256:{digest}"


def _bridge_source_sha256(path: str | Path = __file__) -> str:
    """Full content address used by replay capture (never the display prefix)."""

    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        # A missing source file makes every attempted envelope invalid and thus
        # emits explicit coverage gaps; never synthesize a plausible digest.
        return "source-unreadable"


BRIDGE_BUILD = _bridge_build_id()
BRIDGE_SOURCE_SHA256 = _bridge_source_sha256()
BRIDGE_RUN_ID = str(uuid.uuid4())
# CAPTURE-G3: event-driven subscribe-on-first-alert. The app container writes a subscribe HINT
# to momentum_bridge_subscribe_requests the instant a name first ignites; this bridge FAST-POLLS
# that table (much shorter than REFRESH_S) and subscribes immediately, additively to the normal
# refresh set — closing the ~2.7-min Gate-0 blind window on sub-2-min squeezes (VWAV 2026-06-30).
SUBSCRIBE_ON_ALERT = os.environ.get("CHILI_MOMENTUM_BRIDGE_SUBSCRIBE_ON_ALERT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
SUBSCRIBE_FAST_POLL_S = float(os.environ.get("IQFEED_SUBSCRIBE_FAST_POLL_S", "3") or 3)   # first-alert -> subscribed target
SUBSCRIBE_FRESH_WINDOW_S = float(os.environ.get("IQFEED_SUBSCRIBE_FRESH_WINDOW_S", "180") or 180)  # honor only recent hints
# ── IGNITION DETECTOR (tick-based early-mover nomination; 2026-07-17) ────────────────
# Computes rolling %change / $-volume / print-rate on the Q frames this bridge ALREADY
# parses and pg_notify's a minimal nomination on its OWN channel. The v3 authority
# envelope on IQFEED_NOTIFY_CHANNEL is UNTOUCHED (captured_paper_iqfeed_trigger does
# exact key-set matching on it). Live+ON by default (operator rule: no dark flags);
# thresholds live in iqfeed_ignition_detector.IgnitionConfig (adaptive, floors).
IGNITION_ENABLED = os.environ.get("IQFEED_IGNITION_ENABLED", "1").strip().lower() not in ("0", "false", "no")
IGNITION_CHANNEL = (
    os.environ.get("IQFEED_IGNITION_CHANNEL", "momentum_iqfeed_ignition").strip()
    or "momentum_iqfeed_ignition"
)
IGNITION_SCHEMA_VERSION = "chili.iqfeed-ignition-nominate.v1"
# --- Version-agnostic-backtest coverage (STEP 0): watch the ELIGIBLE-MOVER universe (the names ANY momentum
# version could pick — ranked by explosiveness), not just armed names, so a backtest of a NEW version has
# prints to fill against. The working cap is SELF-DISCOVERED: start at WATCH_HARD_MAX, HALVE on an IQFeed
# symbol-limit signal (the rail-governor pattern), floored at WATCH_FLOOR — no need to know the plan's limit
# up front. The fresh-eligible set is the natural ceiling (usually a few hundred), so the cap rarely binds.
# One documented base (WATCH_FLOOR); the cap is adaptive. Retention raised 3d->30d so we can backtest N days.
RETENTION_DAYS = float(os.environ.get("IQFEED_TRADE_RETENTION_DAYS", "30") or 30)
WATCH_FLOOR = int(os.environ.get("IQFEED_WATCH_FLOOR", "64") or 64)          # the ONE documented base
WATCH_HARD_MAX = int(os.environ.get("IQFEED_WATCH_HARD_MAX", "1000") or 1000)  # backstop only
# STANDING-WATCH WIDENING (2026-07-17, PIT-measured): the 1800s freshness window let
# eligible movers flicker OUT of the roster between snapshot passes, so the ignition
# detector had no eyes on re-igniters (PLSM re-spiked +17% at 12:25 UTC). One documented
# base = one full standing day (86400s); the set stays bounded by the resolver capacity
# + viability-score ranking + the adaptive IQFeed-limit governor. Measured 2026-07-17:
# 24h-eligible = 363 distinct symbols; roster peak over the prior week = 259/hour;
# premarket usage 9-61/hour — comfortably inside the ~500 IQFeed L1 watch limit that
# the rail-governor self-discovers (WATCH_HARD_MAX start, halve-on-limit-signal).
ELIGIBLE_FRESH_S = float(os.environ.get("IQFEED_ELIGIBLE_FRESH_SECONDS", "86400") or 86400)  # standing-watch window (floor)
# These default-layout positions remain only for legacy diagnostic fixtures.
# Production ``reader`` requires an exact, generation-bound selected-field
# acknowledgement before any Q frame can enter an authority path.
L1_LAST, L1_SIZE, L1_TIME, L1_BID, L1_ASK = 2, 3, 4, 7, 9
SELECTED_UPDATE_FIELDS = (
    "Symbol",
    "Most Recent Trade",
    "Most Recent Trade Size",
    # Live IQConnect 6.2.3.5 rejects the "TimeMS" fieldset names outright
    # (E,... is not a valid fieldset field). The protocol-6.2 "...Time"
    # fields already carry microsecond precision (HH:MM:SS.ffffff, verified
    # live 2026-07-16: 15:56:43.267857), so the exact-print clock loses
    # nothing by using the names the feed actually acknowledges.
    "Most Recent Trade Time",
    "Most Recent Trade Date",
    "Most Recent Trade Market Center",
    "Most Recent Trade Conditions",
    "TickID",
    "Bid",
    "Bid Size",
    "Bid Time",
    "Ask",
    "Ask Size",
    "Ask Time",
    "Total Volume",
    "Delay",
    "Message Contents",
    "Decimal Precision",
)
SELECTED_UPDATE_FIELDS_SHA256 = hashlib.sha256(
    json.dumps(
        list(SELECTED_UPDATE_FIELDS),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
# IQFeed always prepends Symbol to Q rows and REJECTS a select that names it
# (the whole request is then silently ignored and the default layout stays) —
# request every field AFTER Symbol; the acknowledgement echoes Symbol first.
SELECT_UPDATE_FIELDS_COMMAND = "S,SELECT UPDATE FIELDS," + ",".join(
    SELECTED_UPDATE_FIELDS[1:]
)
_SELECTED_FIELD_INDEX = {
    name: index + 1 for index, name in enumerate(SELECTED_UPDATE_FIELDS)
}

engine = sa.create_engine(DB_URL, pool_pre_ping=True)

INS = sa.text(
    "INSERT INTO iqfeed_trade_ticks "
    "(symbol, observed_at, price, size, bid, ask, provider_event_at, received_at, "
    "timestamp_basis, bridge_version, provider_trade_reference_at, message_type, "
    "bridge_run_id, connection_generation, source_frame_sequence, "
    "source_frame_sha256) "
    "VALUES (:sym, :at, :px, :sz, :bid, :ask, :provider_at, :received_at, :basis, "
    ":bridge, :provider_trade_reference_at, :message_type, :bridge_run_id, "
    ":connection_generation, :source_frame_sequence, :source_frame_sha256)"
)

# Also feed the momentum ENTRY-GATE's NBBO freshness tape with this SAME tick-level IQFeed L1.
# The entry gate reads momentum_nbbo_spread_tape for its stale_bbo freshness check, but that
# table was fed ONLY by the slower/sparser Massive WS recorder — so the fresh tick-level IQFeed
# quotes (this bridge) NEVER reached the entry decision and wide-spread movers false-blocked on
# stale_bbo (VNTG had 1578 IQFeed ticks/5min @1s old, yet the gate saw a 10-270s WS quote).
# Mirror each valid-quote tick into the tape (source='iqfeed_l1') so the gate uses the freshest
# available quote. Default ON; IQFEED_WRITE_NBBO_TAPE=0 reverts.
WRITE_NBBO_TAPE = os.environ.get("IQFEED_WRITE_NBBO_TAPE", "1").strip().lower() not in ("0", "false", "no")
HOT_FULL_FIDELITY = os.environ.get(
    "IQFEED_HOT_FULL_FIDELITY_ENABLED", "1"
).strip().lower() not in ("0", "false", "no")
# Research trade rows retain the IQFeed Most-Recent-Trade-Time reference when it can be
# parsed. There is deliberately no receive-time fallback: an unparseable/replayed frame
# must never be made fresh merely because this process just received it.
OBSERVED_AT_TRADE_TIME = (
    os.environ.get("IQFEED_OBSERVED_AT_TRADE_TIME", "1").strip().lower() not in ("0", "false", "no")
)
BRIDGE_CAPTURE_CONFIGURATION = {
    "schema_version": "chili.iqfeed-l1-bridge-capture-config.v3",
    "protocol_version": "6.2",
    "host": HOST,
    "port": PORT,
    "flush_interval_seconds": FLUSH_INTERVAL_S,
    "authoritative_max_age_seconds": AUTHORITATIVE_MAX_AGE_S,
    "authoritative_future_tolerance_seconds": AUTHORITATIVE_FUTURE_TOLERANCE_S,
    "authoritative_timestamp_basis": AUTHORITATIVE_TIMESTAMP_BASIS,
    "observed_at_trade_time": OBSERVED_AT_TRADE_TIME,
    "write_nbbo_tape": WRITE_NBBO_TAPE,
    "hot_full_fidelity": HOT_FULL_FIDELITY,
    "selected_update_fields": list(SELECTED_UPDATE_FIELDS),
    "selected_update_fields_sha256": SELECTED_UPDATE_FIELDS_SHA256,
    "selected_fields_ack_timeout_seconds": SELECTED_FIELDS_ACK_TIMEOUT_S,
    "exact_print_timestamp_basis": EXACT_PRINT_TIMESTAMP_BASIS,
    "field_positions": {
        "last": L1_LAST,
        "size": L1_SIZE,
        "trade_time": L1_TIME,
        "bid": L1_BID,
        "ask": L1_ASK,
    },
}
BRIDGE_CAPTURE_CONFIGURATION_SHA256 = hashlib.sha256(
    json.dumps(
        BRIDGE_CAPTURE_CONFIGURATION,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
NBBO_INS = sa.text(
    "INSERT INTO momentum_nbbo_spread_tape "
    "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source, "
    "provider_event_at, received_at, timestamp_basis, bridge_version, "
    "provider_trade_reference_at, message_type, bridge_run_id, connection_generation, "
    "source_frame_sequence, source_frame_sha256) "
    "VALUES (:sym, :at, :bid, :ask, :mid, :spread_bps, NULL, 'iqfeed_l1', "
    ":provider_at, :received_at, :basis, :bridge, :provider_trade_reference_at, "
    ":message_type, :bridge_run_id, :connection_generation, "
    ":source_frame_sequence, :source_frame_sha256)"
)
NOTIFY_IQFEED_TICK = sa.text("SELECT pg_notify(:channel, :payload)")
MARK_TRADE_AVAILABLE = sa.text(
    "UPDATE iqfeed_trade_ticks SET available_at = :available_at "
    "WHERE bridge_run_id = :bridge_run_id "
    "AND connection_generation = :connection_generation "
    "AND source_frame_sequence = :source_frame_sequence "
    "AND source_frame_sha256 = :source_frame_sha256 "
    "AND symbol = :sym AND received_at = :received_at "
    "AND provider_trade_reference_at IS NOT DISTINCT FROM "
    ":provider_trade_reference_at AND message_type = :message_type "
    "AND available_at IS NULL"
)
MARK_NBBO_AVAILABLE = sa.text(
    "UPDATE momentum_nbbo_spread_tape SET available_at = :available_at "
    "WHERE bridge_run_id = :bridge_run_id "
    "AND connection_generation = :connection_generation "
    "AND source_frame_sequence = :source_frame_sequence "
    "AND source_frame_sha256 = :source_frame_sha256 "
    "AND symbol = :sym AND received_at = :received_at "
    "AND provider_trade_reference_at IS NOT DISTINCT FROM "
    ":provider_trade_reference_at AND message_type = :message_type "
    "AND available_at IS NULL"
)

IQFEED_SCHEMA_OWNER_MIGRATION_ID = "334_iqfeed_host_bridge_schema_ownership"
_TRADE_REQUIRED_COLUMNS = frozenset(
    {
        "symbol",
        "observed_at",
        "price",
        "size",
        "bid",
        "ask",
        "provider_event_at",
        "received_at",
        "timestamp_basis",
        "bridge_version",
        "provider_trade_reference_at",
        "message_type",
        "bridge_run_id",
        "connection_generation",
        "source_frame_sequence",
        "source_frame_sha256",
        "available_at",
    }
)
_NBBO_REQUIRED_COLUMNS = frozenset(
    {
        "symbol",
        "observed_at",
        "bid",
        "ask",
        "mid",
        "spread_bps",
        "source",
        "provider_event_at",
        "received_at",
        "timestamp_basis",
        "bridge_version",
        "provider_trade_reference_at",
        "message_type",
        "bridge_run_id",
        "connection_generation",
        "source_frame_sequence",
        "source_frame_sha256",
        "available_at",
    }
)
_SUBSCRIBE_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "symbol",
        "requested_at",
        "reason",
        "source_node_id",
        "correlation_id",
    }
)


def _verify_bridge_schema() -> None:
    """Read-only migration/schema gate which runs before any provider socket."""

    required = {
        "iqfeed_trade_ticks": _TRADE_REQUIRED_COLUMNS,
        "momentum_nbbo_spread_tape": _NBBO_REQUIRED_COLUMNS,
    }
    if SUBSCRIBE_ON_ALERT:
        required["momentum_bridge_subscribe_requests"] = (
            _SUBSCRIBE_REQUIRED_COLUMNS
        )
    try:
        with engine.connect() as connection:
            migrated = bool(connection.execute(
                sa.text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM schema_version WHERE version_id = :version_id"
                    ")"
                ),
                {"version_id": IQFEED_SCHEMA_OWNER_MIGRATION_ID},
            ).scalar_one())
            if not migrated:
                raise RuntimeError(
                    "required IQFeed schema-owner migration is not recorded"
                )
            for table_name, expected in required.items():
                observed = frozenset(connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = :table_name"
                    ),
                    {"table_name": table_name},
                ).scalars())
                missing = sorted(expected - observed)
                if missing:
                    raise RuntimeError(
                        f"IQFeed bridge schema {table_name} is missing: "
                        + ", ".join(missing)
                    )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "IQFeed bridge read-only schema verification failed"
        ) from exc


def _availability_params(row: dict, *, available_at: datetime) -> dict:
    """Exact batch-row identity for the post-insert release transaction.

    A failed release transaction intentionally leaves only that batch's rows
    NULL forever.  A later writer pass must not make an older, never-published
    frame appear observable merely because it shares the process generation.
    """

    source_frame_sequence = row.get("source_frame_sequence")
    if (
        isinstance(source_frame_sequence, bool)
        or not isinstance(source_frame_sequence, int)
        or source_frame_sequence <= 0
    ):
        raise ValueError("IQFeed release source-frame sequence is malformed")
    source_frame_sha256 = str(row.get("source_frame_sha256") or "").strip().lower()
    if len(source_frame_sha256) != 64 or any(
        ch not in "0123456789abcdef" for ch in source_frame_sha256
    ):
        raise ValueError("IQFeed release source-frame SHA-256 is malformed")

    return {
        "available_at": available_at,
        "bridge_run_id": str(row.get("bridge_run_id") or ""),
        "connection_generation": row.get("connection_generation"),
        "source_frame_sequence": source_frame_sequence,
        "source_frame_sha256": source_frame_sha256,
        "sym": str(row.get("sym") or "").upper(),
        "received_at": row.get("received_at"),
        "provider_trade_reference_at": row.get("provider_trade_reference_at"),
        "message_type": str(row.get("message_type") or ""),
    }


def _select_nbbo_rows_for_capture(
    rows: list[dict],
    *,
    hot_symbols: set[str],
) -> list[dict]:
    """Keep every hot-symbol frame; sample only the broad-universe ring.

    The input order is the socket receive order and is retained for hot names.
    Cold/broad names keep their newest frame in this writer flush, which bounds
    baseline storage while still preserving pre-trigger context.  Promotion to
    hot is explicit and never silently changes fidelity because the caller owns
    the current armed/alert set.
    """

    if not rows:
        return []
    hot = {str(symbol or "").strip().upper() for symbol in hot_symbols}
    if not HOT_FULL_FIDELITY:
        hot.clear()
    newest_cold_index: dict[str, int] = {}
    keep = [False] * len(rows)
    for index, row in enumerate(rows):
        symbol = str(row.get("sym") or "").strip().upper()
        if symbol in hot:
            keep[index] = True
            continue
        previous = newest_cold_index.get(symbol)
        if previous is not None:
            keep[previous] = False
        newest_cold_index[symbol] = index
        keep[index] = True
    return [row for index, row in enumerate(rows) if keep[index]]


def _publish_released_capture_rows(
    *,
    trade_rows: list[dict],
    quote_rows: list[dict],
    available_at: datetime,
) -> tuple[int, int]:
    """Offer the exact committed release batch without provider/DB fallback.

    The handoff uses ``put_nowait`` internally.  A full queue or malformed row
    becomes explicit promotion/run gap evidence and never delays this bridge's
    operational DB/NOTIFY path.
    """

    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is None:
        lost = len(trade_rows) + len(quote_rows)
        if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
            log.error(
                "IQFeed replay coverage unavailable: %s",
                json.dumps(
                    {
                        "code": "iqfeed_l1_capture_handoff_unbound_diagnostic",
                        "lost_rows": lost,
                        "available_at": available_at.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return 0, lost
        raise RuntimeError(
            "IQFeed L1 capture handoff is unbound; refusing silent released-row loss"
        )
    try:
        return handoff.offer_released_rows(
            trade_rows=trade_rows,
            quote_rows=quote_rows,
            available_at=available_at,
        )
    except Exception as exc:
        lost = handoff.record_release_failure(
            trade_rows=trade_rows,
            quote_rows=quote_rows,
            available_at=available_at,
        )
        log.exception(
            "IQFeed replay capture handoff failed after DB release; "
            "coverage gapped for %d rows: %s",
            lost,
            exc,
        )
        return 0, lost


def _record_unreleased_capture_gap(
    *,
    symbol: str | None,
    streams: tuple[Any, ...],
    available_at: datetime,
    reason: str,
) -> int:
    """Represent a rejected provider frame without querying DB/provider state."""

    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is None:
        lost = max(1, len(streams))
        if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
            log.error(
                "IQFeed replay coverage unavailable: %s",
                json.dumps(
                    {
                        "code": "iqfeed_l1_source_frame_unbound_diagnostic",
                        "symbol": symbol,
                        "streams": sorted(
                            str(getattr(stream, "value", stream)) for stream in streams
                        ),
                        "reason": reason,
                        "lost_count": lost,
                        "available_at": available_at.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return lost
        raise RuntimeError(
            "IQFeed L1 capture handoff is unbound; refusing silent source-frame loss"
        )
    recorder = getattr(handoff, "record_source_frame_failure", None)
    if callable(recorder):
        try:
            return int(
                recorder(
                    streams=streams,
                    symbol=symbol,
                    available_at=available_at,
                    reason=reason,
                )
            )
        except Exception:
            log.exception("IQFeed source-frame coverage gap handoff failed")
            return 0
    # Compatibility containment for an older structural test double.  The real
    # production handoff implements the typed source-frame method above.
    stream_values = {str(getattr(stream, "value", stream)) for stream in streams}
    trade_rows = ([{"sym": symbol}] if "iqfeed_print" in stream_values else [])
    quote_rows = ([{"sym": symbol}] if "nbbo_quote" in stream_values else [])
    try:
        return int(
            handoff.record_release_failure(
                trade_rows=trade_rows,
                quote_rows=quote_rows,
                available_at=available_at,
            )
        )
    except Exception:
        log.exception("IQFeed fallback source-frame coverage gap handoff failed")
        return 0


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


def _exact_trade_datetime_utc(date_text: str, time_text: str) -> datetime | None:
    """Parse only explicit IQFeed Date + TimeMS encodings; never infer a day.

    Installed IQFeed 6.2 artifacts expose both ISO and US slash date formats.
    Accepting those two provider encodings keeps the event clock exact while
    rejecting locale-dependent or timezone-bearing values.
    """

    if _ET is None:
        return None
    raw_date = str(date_text or "").strip()
    raw_time = str(time_text or "").strip()
    if not raw_date or not raw_time:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        date_format = "%Y-%m-%d"
    elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw_date):
        date_format = "%m/%d/%Y"
    else:
        return None
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", raw_time):
        return None
    time_format = "%H:%M:%S.%f" if "." in raw_time else "%H:%M:%S"
    try:
        provider_date = datetime.strptime(raw_date, date_format).date()
        provider_time = datetime.strptime(raw_time, time_format).time()
        local = datetime.combine(provider_date, provider_time)
        return local.replace(tzinfo=_ET).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _decoded_source_frame_line(source_frame_bytes: bytes) -> str | None:
    """Decode one newline-delimited wire frame without erasing payload bytes."""

    if not isinstance(source_frame_bytes, bytes):
        return None
    try:
        decoded = source_frame_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # ``reader`` splits only LF.  IQConnect normally leaves one CR terminator;
    # that single framing byte is not part of ``line``.  Never use ``rstrip``:
    # spaces and repeated controls are payload differences and must stay bound
    # to the exact source hash or fail closed.
    if decoded.endswith("\r"):
        decoded = decoded[:-1]
    if "\r" in decoded or "\n" in decoded:
        return None
    return decoded


def _observe_selected_update_fields_ack(
    line: str,
    *,
    connection_generation: int,
    source_frame_sha256: str,
    source_frame_bytes: bytes | None = None,
) -> bool:
    """Bind the exact provider-confirmed Q layout to this socket generation."""

    parts = line.split(",")
    if len(parts) < 3 or parts[0:2] != ["S", "CURRENT UPDATE FIELDNAMES"]:
        return False
    raw_fields = parts[2:]
    if raw_fields and raw_fields[-1] == "":
        raw_fields = raw_fields[:-1]
    fields = tuple(raw_fields)
    if fields != SELECTED_UPDATE_FIELDS:
        log.critical(
            "IQFeed selected-field acknowledgement mismatch generation=%d expected=%s observed=%s",
            connection_generation,
            SELECTED_UPDATE_FIELDS,
            fields,
        )
        return False
    if (
        len(source_frame_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in source_frame_sha256)
    ):
        return False
    source_bytes = (
        line.encode("utf-8") if source_frame_bytes is None else source_frame_bytes
    )
    decoded = _decoded_source_frame_line(source_bytes)
    if (
        decoded is None
        or decoded != line
        or hashlib.sha256(source_bytes).hexdigest() != source_frame_sha256
    ):
        return False
    with _connection_state_lock:
        if _active_connection_generation != int(connection_generation):
            return False
        _selected_fields_ack_sha256_by_generation[
            int(connection_generation)
        ] = source_frame_sha256
    return True


def _selected_fields_ack_sha256(connection_generation: int) -> str | None:
    with _connection_state_lock:
        return _selected_fields_ack_sha256_by_generation.get(
            int(connection_generation)
        )


def _wait_for_selected_fields_ack(
    connection_generation: int,
    stop_event: threading.Event,
    *,
    timeout_seconds: float = SELECTED_FIELDS_ACK_TIMEOUT_S,
) -> bool:
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _selected_fields_ack_sha256(connection_generation) is not None:
            return True
        if not _connection_generation_active(connection_generation, stop_event):
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    return _selected_fields_ack_sha256(connection_generation) is not None


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
            "source_frame_sequence": row.get("source_frame_sequence"),
            "source_frame_sha256": str(row.get("source_frame_sha256") or ""),
            "available_at": (
                row.get("available_at").isoformat()
                if isinstance(row.get("available_at"), datetime)
                else None
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


_pending: list[dict] = []
_pending_nbbo: list[dict] = []
_pending_lock = threading.Lock()
_ignition_detector = IgnitionDetector(IgnitionConfig())
_ignition_fires: list[str] = []          # serialized nomination payloads awaiting NOTIFY
_ignition_lock = threading.Lock()
NOTIFY_IGNITION = sa.text("SELECT pg_notify(:channel, :payload)")


def _ignition_payload(fire: IgnitionFire, *, connection_generation: int) -> str:
    """Minimal SEPARATE-channel nomination payload (NOT the v3 authority envelope)."""
    return json.dumps(
        {
            "schema": IGNITION_SCHEMA_VERSION,
            "symbol": fire.symbol,
            "source": "ignition_tick",
            "fired_at": fire.fired_at.isoformat(),
            "last_price": round(fire.last_price, 6),
            "pct_change_60s": round(fire.pct_change_60s, 6),
            "dollar_vol_60s": round(fire.dollar_vol_60s, 2),
            "prints_10s": int(fire.prints_10s),
            "bridge_run_id": BRIDGE_RUN_ID,
            "connection_generation": int(connection_generation),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _observe_ignition_print(
    symbol: str,
    at: datetime,
    price: float,
    size: float,
    *,
    connection_generation: int,
) -> None:
    """Feed one genuinely-new print to the detector; queue any nomination.

    Runs on the reader thread — must NEVER raise into the authority parse path
    and must NEVER touch the DB (the writer owns all DB work).
    """
    if not IGNITION_ENABLED:
        return
    try:
        fire = _ignition_detector.on_print(symbol, at, price, size)
        if fire is None:
            return
        payload = _ignition_payload(
            fire, connection_generation=connection_generation
        )
        with _ignition_lock:
            _ignition_fires.append(payload)
        log.info(
            "ignition nomination %s pct=%.1f%% $60s=%.0f prints10s=%d",
            fire.symbol,
            fire.pct_change_60s * 100.0,
            fire.dollar_vol_60s,
            fire.prints_10s,
        )
    except Exception:
        log.exception("ignition detector failed (nomination path only)")


def _drain_ignition_payloads() -> list[str]:
    with _ignition_lock:
        drained = _ignition_fires[:]
        _ignition_fires.clear()
    return drained


def _emit_ignition_notifications(connection) -> int:
    """NOTIFY every queued nomination on the ignition channel; returns the count.

    Failure is contained by the caller: nominations are advisory (the scheduler
    admission backstop still exists), so a failed emit is logged and dropped —
    it must never affect the tape/NBBO authority path.
    """
    payloads = _drain_ignition_payloads()
    for payload in payloads:
        connection.execute(
            NOTIFY_IGNITION,
            {"channel": IGNITION_CHANNEL, "payload": payload},
        )
    return len(payloads)
_last_trade: dict[str, str] = {}        # symbol -> last seen Most-Recent-Trade-Time (dedup key)
watched: set[str] = set()
_max_watch = WATCH_HARD_MAX             # adaptive watch cap; halved on an IQFeed limit signal, floored at WATCH_FLOOR
_limit_hit = False                      # set by the reader thread when IQFeed signals a symbol limit
sock_lock = threading.Lock()
_connection_state_lock = threading.Lock()
_active_connection_generation = 0
_last_nbbo_append_monotonic: float | None = None
_connection_generation = 0
_frame_sequence_by_generation: dict[int, int] = {}
_selected_fields_ack_sha256_by_generation: dict[int, str] = {}
_capture_handoff_lock = threading.Lock()
_capture_handoff: Any | None = None


class _ReaderQuiescenceError(RuntimeError):
    provider_reader_may_be_alive = True


def _begin_connection_generation() -> int:
    global _connection_generation, _active_connection_generation
    with _connection_state_lock:
        _connection_generation += 1
        _active_connection_generation = _connection_generation
        _frame_sequence_by_generation[_connection_generation] = 0
        _selected_fields_ack_sha256_by_generation.pop(_connection_generation, None)
        return _connection_generation


def _activate_connection_generation(connection_generation: int) -> None:
    global _active_connection_generation
    with _connection_state_lock:
        _active_connection_generation = int(connection_generation)
        _frame_sequence_by_generation.setdefault(int(connection_generation), 0)
        _selected_fields_ack_sha256_by_generation.pop(
            int(connection_generation), None
        )


def _next_source_frame_sequence(connection_generation: int) -> int:
    generation = int(connection_generation)
    if generation <= 0:
        raise ValueError("IQFeed source frame generation must be positive")
    with _connection_state_lock:
        next_sequence = _frame_sequence_by_generation.get(generation, 0) + 1
        _frame_sequence_by_generation[generation] = next_sequence
        return next_sequence


def bind_capture_handoff(handoff: Any) -> None:
    """Install one already-started, bounded no-fetch capture handoff.

    The normal bridge entry point does not manufacture account/run identity.
    Production composition must build the hash-bound capture process first and
    inject its handoff explicitly.  Rebinding in place is prohibited so a run
    cannot silently switch capture authority mid-connection.
    """

    if (
        not callable(getattr(handoff, "offer_released_rows", None))
        or not callable(getattr(handoff, "record_release_failure", None))
        or not callable(getattr(handoff, "record_connection_boundary", None))
        or not callable(getattr(handoff, "health", None))
    ):
        raise TypeError("IQFeed capture handoff is malformed")
    health = handoff.health()
    if not health.get("started") or not health.get("accepting"):
        raise RuntimeError("IQFeed capture handoff must be started before binding")
    global _capture_handoff
    with _connection_state_lock:
        if _active_connection_generation != 0:
            raise RuntimeError("IQFeed capture handoff cannot bind mid-connection")
        with _capture_handoff_lock:
            if _capture_handoff is not None:
                raise RuntimeError("IQFeed capture handoff is already bound")
            _capture_handoff = handoff


def unbind_capture_handoff(handoff: Any) -> None:
    """Remove the exact bound handoff after its producer has quiesced."""

    global _capture_handoff
    with _connection_state_lock:
        if _active_connection_generation != 0:
            raise RuntimeError("IQFeed capture handoff cannot unbind mid-connection")
        with _capture_handoff_lock:
            if _capture_handoff is not handoff:
                raise RuntimeError("IQFeed capture handoff ownership mismatch")
            _capture_handoff = None


def _require_standalone_capture_posture() -> None:
    """Refuse a provider socket unless capture is bound or explicitly diagnostic."""

    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is not None:
        return
    if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
        log.critical(
            "IQFeed L1 running in explicit uncaptured diagnostic mode; "
            "all ReplayV3 coverage from this process is unavailable"
        )
        return
    raise RuntimeError(
        "IQFeed L1 capture handoff must be bound before provider connection; "
        f"use {UNCAPTURED_DIAGNOSTIC_FLAG} only for non-certifying diagnostics"
    )


def _require_supervised_capture_posture() -> None:
    """Require the host-bound capture authority without consulting ``sys.argv``."""

    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is None:
        raise RuntimeError(
            "supervised IQFeed L1 requires a bound capture handoff"
        )


def _record_capture_connection_boundary(
    *,
    at: datetime,
    connection_generation: int,
    active: bool,
) -> None:
    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is None:
        return
    try:
        handoff.record_connection_boundary(
            at=at,
            bridge_run_id=BRIDGE_RUN_ID,
            connection_generation=connection_generation,
            active=active,
        )
    except Exception:
        log.exception("IQFeed L1 capture connection-boundary handoff failed")


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
        _frame_sequence_by_generation.pop(int(connection_generation), None)
        _selected_fields_ack_sha256_by_generation.pop(
            int(connection_generation), None
        )


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
    result = _live_symbols_read()
    return set(result.symbols)


def _live_symbols_read() -> SourceRead:
    try:
        with engine.connect() as c:
            rows = c.execute(sa.text(ACTIVE_EXECUTION_SESSION_SQL)).fetchall()
        return SourceRead.success(TargetCause.ACTIVE, active_capture_symbols(rows))
    except Exception as e:
        log.warning("symbol query failed: %s", e)
        return SourceRead.failure(
            TargetCause.ACTIVE,
            error_code="active_query_failed",
            error_detail=str(e),
        )


def _eligible_symbols(limit: int) -> list[str]:
    result = _eligible_symbols_read(limit)
    return list(result.symbols)


def _eligible_symbols_read(limit: int) -> SourceRead:
    """The fresh ELIGIBLE-MOVER universe ranked by explosiveness (viability_score) — the names ANY momentum
    version could pick, so a backtested new version has prints to fill against. Up to `limit`, most-explosive
    first. Empty outside market hours (no fresh viability) — fine, nothing to watch then."""
    if limit <= 0:
        return SourceRead.success(TargetCause.ELIGIBLE, ())
    try:
        with engine.connect() as c:
            rows = c.execute(sa.text(
                "SELECT symbol FROM ("
                "  SELECT DISTINCT ON (symbol) symbol, viability_score FROM momentum_symbol_viability "
                "  WHERE symbol NOT LIKE '%-%' AND (live_eligible OR paper_eligible) "
                "    AND freshness_ts > (now() at time zone 'utc') - make_interval(secs => :fresh) "
                "  ORDER BY symbol, freshness_ts DESC"
                ") q ORDER BY viability_score DESC NULLS LAST, symbol ASC LIMIT :lim"
            ), {"fresh": ELIGIBLE_FRESH_S, "lim": int(limit)}).fetchall()
        return SourceRead.success(TargetCause.ELIGIBLE, (str(r[0]) for r in rows))
    except Exception as e:
        log.warning("eligible query failed: %s", e)
        return SourceRead.failure(
            TargetCause.ELIGIBLE,
            error_code="eligible_query_failed",
            error_detail=str(e),
        )


def _alert_symbols(
    fresh_window_s: float,
    limit: int = WATCH_HARD_MAX,
) -> list[str]:
    result = _alert_symbols_read(fresh_window_s, limit=limit)
    return list(result.symbols)


def _alert_symbols_read(
    fresh_window_s: float,
    *,
    limit: int = WATCH_HARD_MAX,
) -> SourceRead:
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
                ") q ORDER BY freshest DESC, symbol ASC LIMIT :lim"
            ), {"w": float(fresh_window_s), "lim": max(0, int(limit))}).fetchall()
        return SourceRead.success(TargetCause.HINT, (str(r[0]) for r in rows))
    except Exception as e:
        log.debug("alert-subscribe query failed: %s", e)
        return SourceRead.failure(
            TargetCause.HINT,
            error_code="hint_query_failed",
            error_detail=str(e),
        )


def _ross_universe_symbols(limit: int) -> list[str]:
    result = _ross_universe_symbols_read(limit)
    return list(result.symbols)


def _ross_universe_symbols_read(limit: int) -> SourceRead:
    """Direct Ross-profile universe; independent of viability admission."""
    if limit <= 0:
        return SourceRead.success(TargetCause.ROSS, ())
    try:
        from app.services.trading.momentum_neural.universe import (
            EQUITY_ROSS_SMALLCAP,
            build_equity_universe,
        )

        symbols = build_equity_universe(EQUITY_ROSS_SMALLCAP) or []
        result = SourceRead.success(TargetCause.ROSS, symbols[: int(limit)])
        if not result.symbols:
            # The universe helper intentionally collapses provider/query errors
            # to [], which is indistinguishable from a legitimately empty scan.
            # Capture cannot interpret that ambiguity as permission to unwatch.
            return SourceRead.failure(
                TargetCause.ROSS,
                error_code="ross_universe_empty_or_unavailable",
            )
        return result
    except Exception as e:
        log.warning("ross universe query failed: %s", e)
        return SourceRead.failure(
            TargetCause.ROSS,
            error_code="ross_query_failed",
            error_detail=str(e),
        )


def _log_subscription_gaps(feed: str, gaps: tuple[CoverageGap, ...]) -> None:
    for gap in gaps:
        log.warning(
            "subscription coverage unavailable feed=%s code=%s source=%s symbol=%s causes=%s detail=%s",
            feed,
            gap.code,
            gap.source,
            gap.symbol or "-",
            ",".join(cause.value for cause in gap.causes) or "-",
            gap.detail or "-",
        )


def _resolve_target(
    *,
    reads: list[SourceRead],
    prior_causes: dict[str, frozenset[TargetCause]],
    capacity: int,
) -> TargetResolution:
    """Shared seam kept visible for focused L1/L2 parity tests."""
    require_complete_source_inventory(reads)
    return resolve_subscription_target(
        reads=reads,
        prior_causes=prior_causes,
        capacity=capacity,
    )


def _parse_selected_l1(
    line: str,
    *,
    connection_generation: int,
    selected_fields_ack_sha256: str,
    received_at: datetime | None = None,
    source_frame_sha256: str | None = None,
    source_frame_bytes: bytes | None = None,
) -> tuple[bool, bool]:
    """Parse one provider-confirmed selected-field Q frame into an exact print."""

    global _last_nbbo_append_monotonic
    p = line.split(",")
    if len(p) <= max(_SELECTED_FIELD_INDEX.values()):
        return False, False
    try:
        message_type = str(p[0] or "").strip().upper()
        if message_type != "Q":
            return False, False
        sym = str(p[_SELECTED_FIELD_INDEX["Symbol"]] or "").strip().upper()
        if (
            EQUITY_SYMBOL_RE.fullmatch(sym) is None
            or sym.endswith(".")
            or ".." in sym
        ):
            return False, False
        generation = int(connection_generation)
        if generation <= 0 or (
            _selected_fields_ack_sha256(generation)
            != selected_fields_ack_sha256
        ):
            return False, False
        if (
            len(selected_fields_ack_sha256) != 64
            or any(
                ch not in "0123456789abcdef"
                for ch in selected_fields_ack_sha256
            )
        ):
            return False, False
        frame_sequence = _next_source_frame_sequence(generation)
        frame_sha256 = (
            hashlib.sha256(line.encode("utf-8")).hexdigest()
            if source_frame_sha256 is None
            else str(source_frame_sha256).strip().lower()
        )
        if len(frame_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in frame_sha256
        ):
            return False, False
        frame_bytes = (
            line.encode("utf-8") if source_frame_bytes is None else source_frame_bytes
        )
        decoded_frame = _decoded_source_frame_line(frame_bytes)
        if (
            decoded_frame is None
            or decoded_frame != line
            or hashlib.sha256(frame_bytes).hexdigest() != frame_sha256
        ):
            return False, False
        received = received_at or datetime.now(timezone.utc)
        if not isinstance(received, datetime) or received.tzinfo is None:
            return False, False
        received = received.astimezone(timezone.utc)
        raw_trade_date = str(
            p[_SELECTED_FIELD_INDEX["Most Recent Trade Date"]] or ""
        ).strip()
        raw_trade_time = str(
            p[_SELECTED_FIELD_INDEX["Most Recent Trade Time"]] or ""
        ).strip()
        provider_event_at = _exact_trade_datetime_utc(
            raw_trade_date, raw_trade_time
        )
        if provider_event_at is None:
            return False, False
        receive_event_delta = (received - provider_event_at).total_seconds()
        if receive_event_delta < -AUTHORITATIVE_FUTURE_TOLERANCE_S:
            return False, False
        bid_raw = str(p[_SELECTED_FIELD_INDEX["Bid"]] or "").strip()
        ask_raw = str(p[_SELECTED_FIELD_INDEX["Ask"]] or "").strip()
        bid = float(bid_raw) if bid_raw else None
        ask = float(ask_raw) if ask_raw else None
        quote_valid = bool(
            bid is not None
            and ask is not None
            and math.isfinite(bid)
            and math.isfinite(ask)
            and bid > 0
            and ask >= bid
        )
        quote_captured = bool(
            quote_valid and receive_event_delta <= AUTHORITATIVE_MAX_AGE_S
        )
        if quote_captured:
            assert bid is not None and ask is not None
            mid = (bid + ask) / 2.0
            with _pending_lock:
                _pending_nbbo.append(
                    {
                        "sym": sym,
                        "at": provider_event_at.replace(tzinfo=None),
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "spread_bps": (ask - bid) / mid * 10_000.0,
                        "provider_at": None,
                        "provider_trade_reference_at": provider_event_at,
                        "received_at": received,
                        "basis": AUTHORITATIVE_TIMESTAMP_BASIS,
                        "bridge": BRIDGE_BUILD,
                        "message_type": message_type,
                        "bridge_run_id": BRIDGE_RUN_ID,
                        "connection_generation": generation,
                        "source_frame_sequence": frame_sequence,
                        "source_frame_sha256": frame_sha256,
                    }
                )
            _last_nbbo_append_monotonic = time.monotonic()

        raw_tick_id = str(p[_SELECTED_FIELD_INDEX["TickID"]] or "").strip()
        raw_market_center = str(
            p[_SELECTED_FIELD_INDEX["Most Recent Trade Market Center"]] or ""
        ).strip()
        raw_conditions = str(
            p[_SELECTED_FIELD_INDEX["Most Recent Trade Conditions"]] or ""
        ).strip()
        message_contents = str(
            p[_SELECTED_FIELD_INDEX["Message Contents"]] or ""
        )
        if not raw_tick_id.isdigit() or not raw_market_center:
            return False, quote_captured
        trade_key = "|".join(
            (raw_trade_date, raw_trade_time, raw_tick_id, raw_market_center)
        )
        if _last_trade.get(sym) == trade_key:
            return True, quote_captured
        price = float(p[_SELECTED_FIELD_INDEX["Most Recent Trade"]] or 0)
        size = float(p[_SELECTED_FIELD_INDEX["Most Recent Trade Size"]] or 0)
        if (
            not math.isfinite(price)
            or not math.isfinite(size)
            or price <= 0
            or size <= 0
        ):
            return False, quote_captured
        trade_conditions = [raw_conditions] if raw_conditions else []
        _last_trade[sym] = trade_key
        with _pending_lock:
            _pending.append(
                {
                    "sym": sym,
                    "at": provider_event_at.replace(tzinfo=None),
                    "px": price,
                    "sz": size,
                    "bid": bid if quote_valid else None,
                    "ask": ask if quote_valid else None,
                    "provider_at": provider_event_at,
                    "provider_trade_reference_at": provider_event_at,
                    "received_at": received,
                    "basis": EXACT_PRINT_TIMESTAMP_BASIS,
                    "bridge": BRIDGE_BUILD,
                    "message_type": message_type,
                    "bridge_run_id": BRIDGE_RUN_ID,
                    "connection_generation": generation,
                    "source_frame_sequence": frame_sequence,
                    "source_frame_sha256": frame_sha256,
                    "provider_trade_date": raw_trade_date,
                    "provider_trade_time": raw_trade_time,
                    "provider_tick_id": raw_tick_id,
                    "trade_market_center": raw_market_center,
                    "trade_conditions": trade_conditions,
                    "message_contents": message_contents,
                    "selected_update_fields": list(SELECTED_UPDATE_FIELDS),
                    "selected_update_fields_sha256": (
                        SELECTED_UPDATE_FIELDS_SHA256
                    ),
                    "selected_update_fields_ack_sha256": (
                        selected_fields_ack_sha256
                    ),
                }
            )
        # Ignition nomination rides the SAME exact-print stream (genuinely-new
        # trades only — the dedup key above already dropped repeats/replays).
        _observe_ignition_print(
            sym,
            provider_event_at,
            price,
            size,
            connection_generation=generation,
        )
        return True, quote_captured
    except (TypeError, ValueError, IndexError):
        return False, False


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
        source_frame_sequence = _next_source_frame_sequence(generation)
        source_frame_sha256 = hashlib.sha256(line.encode("utf-8")).hexdigest()
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
                    "source_frame_sequence": source_frame_sequence,
                    "source_frame_sha256": source_frame_sha256,
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
            "source_frame_sequence": source_frame_sequence,
            "source_frame_sha256": source_frame_sha256,
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
            decoded_wire = raw.decode(errors="replace")
            line = (
                decoded_wire[:-1]
                if decoded_wire.endswith("\r")
                else decoded_wire
            )
            if not line:
                continue
            c0 = line[0]
            received_at = datetime.now(timezone.utc)
            source_frame_sha256 = hashlib.sha256(raw).hexdigest()
            if c0 == "Q":
                ack_sha256 = _selected_fields_ack_sha256(connection_generation)
                symbol = str(
                    line.split(",", 2)[1] if "," in line else ""
                ).strip().upper() or None
                if ack_sha256 is None:
                    _record_unreleased_capture_gap(
                        symbol=symbol,
                        streams=("iqfeed_print", "nbbo_quote"),
                        available_at=received_at,
                        reason="iqfeed_selected_fields_unconfirmed",
                    )
                    continue
                print_valid, quote_captured = _parse_selected_l1(
                    line,
                    connection_generation=connection_generation,
                    selected_fields_ack_sha256=ack_sha256,
                    received_at=received_at,
                    source_frame_sha256=source_frame_sha256,
                    source_frame_bytes=raw,
                )
                missing_streams = (
                    *(("iqfeed_print",) if not print_valid else ()),
                    *(("nbbo_quote",) if not quote_captured else ()),
                )
                if missing_streams:
                    _record_unreleased_capture_gap(
                        symbol=symbol,
                        streams=missing_streams,
                        available_at=received_at,
                        reason="iqfeed_selected_q_frame_unavailable",
                    )
                seen += 1
            elif c0 == "P":
                # Provider summary is not a new execution and never enters the
                # exact-print or NBBO authority paths.
                continue
            elif c0 == "T":                     # timestamp heartbeat (NOT a trade)
                continue
            elif line.startswith("S,") or c0 in ("n", "E"):
                if line.startswith("S,CURRENT UPDATE FIELDNAMES,"):
                    if not _observe_selected_update_fields_ack(
                        line,
                        connection_generation=connection_generation,
                        source_frame_sha256=source_frame_sha256,
                        source_frame_bytes=raw,
                    ):
                        log.warning(
                            "IQFeed ignored non-authoritative update-field roster generation=%d",
                            connection_generation,
                        )
                elif any(k in line.upper() for k in ("SYMBOL LIMIT", "MAX SYMBOL", "LIMIT REACHED", "TOO MANY SYMBOL")):
                    if _connection_generation_active(connection_generation, stop_event):
                        _limit_hit = True            # current writer halves its watch-set on this signal
                    log.warning("IQFeed symbol-limit signal: %s", line[:160])
                else:
                    log.info("feed: %s", line[:160])
    _request_connection_stop(connection_generation, stop_event)


def _try_watch_symbol(
    connection_socket: socket.socket,
    symbol: str,
    *,
    causes: tuple[TargetCause, ...] = (),
    watched_set: set[str] | None = None,
) -> None:
    """Watch one symbol or invalidate this connection's entire local state.

    ``sendall`` raising does not prove that IQConnect received zero bytes.  The
    provider has no durable per-symbol ACK, so retaining any local watch claim
    after a failed send would be optimistic.  The typed exception deliberately
    escapes the writer; ``_run_connection`` closes this socket generation and
    ``main`` reconnects from an empty watch set.
    """
    state = watched if watched_set is None else watched_set
    try:
        _send(connection_socket, f"w{symbol}")
    except Exception as exc:
        state.clear()
        raise SubscriptionConnectionIndeterminate(
            CoverageGap(
                code="watch_send_indeterminate",
                source="iqfeed_l1",
                symbol=symbol,
                causes=causes,
                detail=(
                    "command_index=1/1; connection invalidation required; "
                    + str(exc)[:384]
                ),
            )
        ) from exc
    state.add(symbol)


def _try_sticky_resubscribe_symbol(
    connection_socket: socket.socket,
    symbol: str,
    *,
    causes: tuple[TargetCause, ...] = (),
    watched_set: set[str] | None = None,
) -> None:
    """Re-send an L1 watch without treating a failed send as harmless."""
    state = watched if watched_set is None else watched_set
    try:
        _send(connection_socket, f"w{symbol}")
    except Exception as exc:
        state.clear()
        raise SubscriptionConnectionIndeterminate(
            CoverageGap(
                code="sticky_resubscribe_send_indeterminate",
                source="iqfeed_l1",
                symbol=symbol,
                causes=causes,
                detail=(
                    "command_index=1/1; connection invalidation required; "
                    + str(exc)[:384]
                ),
            )
        ) from exc


def _try_unwatch_symbol(
    connection_socket: socket.socket,
    symbol: str,
    *,
    causes: tuple[TargetCause, ...] = (),
    watched_set: set[str] | None = None,
) -> None:
    """Unwatch one symbol or invalidate this connection's local watch claim."""
    state = watched if watched_set is None else watched_set
    try:
        _send(connection_socket, f"r{symbol}")
    except Exception as exc:
        state.clear()
        raise SubscriptionConnectionIndeterminate(
            CoverageGap(
                code="unwatch_send_indeterminate",
                source="iqfeed_l1",
                symbol=symbol,
                causes=causes,
                detail=(
                    "command_index=1/1; connection invalidation required; "
                    + str(exc)[:384]
                ),
            )
        ) from exc
    state.discard(symbol)


def writer(
    forced_syms: set[str],
    deadline: float | None,
    connection_socket: socket.socket,
    stop_event: threading.Event,
    connection_generation: int,
) -> None:
    global _max_watch, _limit_hit
    last_refresh = 0.0
    # Defer the first hourly retention sweep to one hour after connect: with
    # last_prune=0.0 it ran on the FIRST loop iteration, and its observed_at-only
    # DELETE full-scans iqfeed_trade_ticks (~128M rows, 139s live-measured
    # 2026-07-16) BEFORE the first reconcile() can send any watch -- every
    # watch (and the 30s capture-certification smoke window) starved behind it.
    last_prune = time.monotonic()
    last_fast_sub = 0.0
    # Explicit CLI symbols are capture-hot for the lifetime of this writer.
    # Dynamic hot membership is refreshed from live/paper sessions + fresh alerts.
    hot_symbols: set[str] = {str(symbol).upper() for symbol in forced_syms}
    prior_causes: dict[str, frozenset[TargetCause]] = {
        symbol: frozenset({TargetCause.RETAINED}) for symbol in watched
    }
    source_reads: dict[TargetCause, SourceRead] = {
        cause: SourceRead.success(cause, ())
        for cause in (
            TargetCause.ACTIVE,
            TargetCause.HINT,
            TargetCause.ELIGIBLE,
            TargetCause.ROSS,
        )
    }

    def reconcile(*, allow_unwatch: bool, sticky: bool = False) -> None:
        nonlocal prior_causes, hot_symbols
        reads = (
            [SourceRead.success(TargetCause.FORCED, forced_syms)]
            if forced_syms
            else list(source_reads.values())
        )
        resolution = _resolve_target(
            reads=reads,
            prior_causes=prior_causes,
            capacity=max(_max_watch, len(forced_syms)),
        )
        _log_subscription_gaps("iqfeed_l1", resolution.gaps)
        target_causes = resolution.causes_by_symbol
        hot_symbols = {
            symbol
            for symbol, causes in target_causes.items()
            if TargetCause.ACTIVE in causes
            or TargetCause.HINT in causes
            or TargetCause.FORCED in causes
        }

        if allow_unwatch:
            for symbol in sorted(watched - resolution.symbols):
                _try_unwatch_symbol(
                    connection_socket,
                    symbol,
                    causes=tuple(
                        cause
                        for cause in TargetCause
                        if cause in prior_causes.get(symbol, ())
                    ),
                )
                prior_causes.pop(symbol, None)
                _last_trade.pop(symbol, None)

        for target in resolution.targets:
            symbol = target.symbol
            if symbol in watched:
                prior_causes[symbol] = frozenset(target.causes)
                if sticky:
                    _try_sticky_resubscribe_symbol(
                        connection_socket,
                        symbol,
                        causes=target.causes,
                    )
                continue
            protected_target = bool(
                TargetCause.ACTIVE in target.causes
                or TargetCause.FORCED in target.causes
            )
            if len(watched) >= resolution.capacity and not protected_target:
                _log_subscription_gaps(
                    "iqfeed_l1",
                    (
                        CoverageGap(
                            code="watch_capacity_not_freed",
                            source="iqfeed_l1",
                            symbol=symbol,
                            causes=target.causes,
                            detail=(
                                f"watched={len(watched)} "
                                f"capacity={resolution.capacity}"
                            ),
                        ),
                    ),
                )
                continue
            _try_watch_symbol(
                connection_socket,
                symbol,
                causes=target.causes,
            )
            prior_causes[symbol] = frozenset(target.causes)
            log.info(
                "watching trades: %s causes=%s",
                symbol,
                ",".join(cause.value for cause in target.causes),
            )

    while (
        _connection_generation_active(connection_generation, stop_event)
        and (deadline is None or time.monotonic() < deadline)
    ):
        if stop_event.wait(FLUSH_INTERVAL_S):
            break
        # CAPTURE-G3 FAST PATH: subscribe first-alert names IMMEDIATELY (short poll), additive to
        # the slow REFRESH_S set below — closes the ~2.7-min Gate-0 blind window. Runs BEFORE the
        # slow refresh so a fresh mover is watched within ~SUBSCRIBE_FAST_POLL_S of its first alert.
        # The shared resolver protects active/held names, then admits fresh
        # newest-first hints ahead of colder broad-universe fallbacks. Any
        # capacity displacement is deterministic and emitted as explicit
        # coverage-unavailable evidence before socket reconciliation.
        if (
            SUBSCRIBE_ON_ALERT
            and not forced_syms                     # explicit CLI symbols -> no dynamic subscribe
            and time.monotonic() - last_fast_sub >= SUBSCRIBE_FAST_POLL_S
        ):
            last_fast_sub = time.monotonic()
            source_reads[TargetCause.HINT] = _alert_symbols_read(
                SUBSCRIBE_FRESH_WINDOW_S,
                limit=_max_watch,
            )
            reconcile(allow_unwatch=True)
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
            if not forced_syms:
                source_reads[TargetCause.ACTIVE] = _live_symbols_read()
                source_reads[TargetCause.ELIGIBLE] = _eligible_symbols_read(
                    _max_watch
                )
                source_reads[TargetCause.ROSS] = _ross_universe_symbols_read(
                    _max_watch
                )
                source_reads[TargetCause.HINT] = (
                    _alert_symbols_read(
                        SUBSCRIBE_FRESH_WINDOW_S,
                        limit=_max_watch,
                    )
                    if SUBSCRIBE_ON_ALERT
                    else SourceRead.success(TargetCause.HINT, ())
                )
            reconcile(allow_unwatch=True, sticky=STICKY_RESUBSCRIBE)
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
        # Full-fidelity for current hot symbols; bounded newest-per-flush context
        # for the broad universe.  Missing exact IQFeed quote-event clocks still
        # make these rows noncertifying regardless of storage fidelity.
        nbbo_rows = _select_nbbo_rows_for_capture(
            nbbo_rows,
            hot_symbols=hot_symbols,
        )
        if rows or nbbo_rows:
            try:
                # Phase 1: persist the raw rows.  ``received_at`` may precede
                # this commit by up to the flush interval, so it is not a
                # strategy-availability clock.
                with engine.begin() as c:
                    if rows:
                        c.execute(INS, rows)
                    if WRITE_NBBO_TAPE and nbbo_rows:
                        c.execute(NBBO_INS, nbbo_rows)
                # Phase 2: stamp the replay release clock and enqueue the
                # event-driven notifications in ONE transaction.  PostgreSQL
                # delivers NOTIFY messages only when this transaction commits,
                # so a dispatcher can never observe an authority envelope whose
                # matching DB row still has ``available_at IS NULL``.  Strict
                # replay treats this commit boundary as the release boundary.
                # Exact print rows retain the provider Date+TimeMS event clock;
                # quote rows keep provider_event_at NULL because the selected
                # L1 layout still exposes no exact quote-event timestamp.
                available_at = datetime.now(timezone.utc)
                with engine.begin() as c:
                    if rows:
                        result = c.execute(
                            MARK_TRADE_AVAILABLE,
                            [
                                _availability_params(row, available_at=available_at)
                                for row in rows
                            ],
                        )
                        if result.rowcount not in (-1, len(rows)):
                            raise RuntimeError(
                                "IQFeed trade release identity mismatch: "
                                f"expected={len(rows)} updated={result.rowcount}"
                            )
                    if WRITE_NBBO_TAPE and nbbo_rows:
                        result = c.execute(
                            MARK_NBBO_AVAILABLE,
                            [
                                _availability_params(row, available_at=available_at)
                                for row in nbbo_rows
                            ],
                        )
                        if result.rowcount not in (-1, len(nbbo_rows)):
                            raise RuntimeError(
                                "IQFeed NBBO release identity mismatch: "
                                f"expected={len(nbbo_rows)} updated={result.rowcount}"
                            )
                        if IQFEED_NOTIFY_ENABLED:
                            for row in nbbo_rows:
                                row["available_at"] = available_at
                                c.execute(
                                    NOTIFY_IQFEED_TICK,
                                    {
                                        "channel": IQFEED_NOTIFY_CHANNEL,
                                        "payload": _notify_payload(row),
                                    },
                                )
                capture_accepted, capture_rejected = _publish_released_capture_rows(
                    trade_rows=rows,
                    quote_rows=(nbbo_rows if WRITE_NBBO_TAPE else []),
                    available_at=available_at,
                )
                if capture_rejected:
                    log.warning(
                        "IQFeed replay capture rejected/gapped %d of %d released rows",
                        capture_rejected,
                        capture_accepted + capture_rejected,
                    )
            except Exception as e:
                failure_at = datetime.now(timezone.utc)
                with _capture_handoff_lock:
                    handoff = _capture_handoff
                if handoff is not None:
                    try:
                        handoff.record_release_failure(
                            trade_rows=rows,
                            quote_rows=(nbbo_rows if WRITE_NBBO_TAPE else []),
                            available_at=failure_at,
                        )
                    except Exception:
                        log.exception(
                            "IQFeed DB release failure and capture loss accounting both failed"
                        )
                log.warning(
                    "trade/BBO insert failed (%d trade, %d BBO rows): %s",
                    len(rows),
                    len(nbbo_rows),
                    e,
                )
        # IGNITION: emit queued nominations on their own channel in their own
        # transaction — contained so a failure can never affect the tape path.
        with _ignition_lock:
            has_ignition_fires = bool(_ignition_fires)
        if IGNITION_ENABLED and has_ignition_fires:
            try:
                with engine.begin() as c:
                    emitted = _emit_ignition_notifications(c)
                if emitted:
                    log.info("ignition notify emitted=%d channel=%s", emitted, IGNITION_CHANNEL)
            except Exception as e:
                log.warning("ignition notify emit failed (nominations dropped): %s", e)
    _request_connection_stop(connection_generation, stop_event)


def _selftest() -> int:
    """Verify the DB path WITHOUT IQFeed: write a synthetic row, read it back."""
    _verify_bridge_schema()
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
            "source_frame_sequence": 1,
            "source_frame_sha256": hashlib.sha256(
                b"iqfeed-bridge-selftest-source-frame"
            ).hexdigest(),
        }])
    with engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM iqfeed_trade_ticks WHERE symbol='_SELFTEST'")).scalar()
        c2 = c.execute(sa.text("DELETE FROM iqfeed_trade_ticks WHERE symbol='_SELFTEST'"))  # noqa: F841
    log.info("selftest: wrote+read %s synthetic row(s), table OK", n)
    return 0 if n and n >= 1 else 1


def _run_connection(
    forced: set[str],
    deadline: float | None,
    *,
    supervisor_stop_event: threading.Event | None = None,
    connected_event: threading.Event | None = None,
    ready_event: threading.Event | None = None,
) -> None:
    """Own one socket generation through close and reader quiescence.

    A subsequent connection may not be created until this function has closed the
    concrete socket and joined its reader. If close cannot retire the reader within
    the bounded interval, raise a terminal error so ``main`` refuses to rebind.
    """
    global _last_nbbo_append_monotonic
    if connected_event is not None:
        connected_event.clear()
    if ready_event is not None:
        ready_event.clear()
    if supervisor_stop_event is not None and supervisor_stop_event.is_set():
        return
    connection_socket = socket.create_connection((HOST, PORT), timeout=10)
    stop_event = threading.Event()
    reader_thread: threading.Thread | None = None
    stop_relay_thread: threading.Thread | None = None
    stop_relay_done = threading.Event()
    connection_generation = _begin_connection_generation()
    try:
        if connected_event is not None:
            connected_event.set()

        if supervisor_stop_event is not None:
            def relay_supervisor_stop() -> None:
                while not stop_relay_done.wait(0.05):
                    if supervisor_stop_event.is_set():
                        _request_connection_stop(
                            connection_generation,
                            stop_event,
                        )
                        _close_connection_socket(connection_socket)
                        return

            stop_relay_thread = threading.Thread(
                target=relay_supervisor_stop,
                daemon=False,
                name=f"iqfeed-trade-stop-relay-g{connection_generation}",
            )
            stop_relay_thread.start()
        _record_capture_connection_boundary(
            at=datetime.now(timezone.utc),
            connection_generation=connection_generation,
            active=True,
        )
        _capture_bc("socket connected; boundary recorded (real handoff)")
        connection_socket.settimeout(2.0)
        watched.clear()
        _last_trade.clear()
        _last_nbbo_append_monotonic = time.monotonic()
        _send(
            connection_socket,
            "S,SET PROTOCOL,6.2",
        )
        _send(connection_socket, SELECT_UPDATE_FIELDS_COMMAND)
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
        _capture_bc("reader started; BEGIN field-ack wait")
        if not _wait_for_selected_fields_ack(
            connection_generation,
            stop_event,
        ):
            _capture_bc("field-ack FAILED (roster not acknowledged)")
            raise RuntimeError(
                "IQFeed exact selected-field roster was not acknowledged"
            )
        if supervisor_stop_event is not None and supervisor_stop_event.is_set():
            return
        _capture_bc("field ack ok; READY")
        if ready_event is not None:
            ready_event.set()
        writer(
            forced,
            deadline,
            connection_socket,
            stop_event,
            connection_generation,
        )
    finally:
        if ready_event is not None:
            ready_event.clear()
        _request_connection_stop(connection_generation, stop_event)
        _close_connection_socket(connection_socket)
        if reader_thread is not None:
            reader_thread.join(timeout=READER_JOIN_TIMEOUT_S)
        reader_quiesced = bool(
            reader_thread is None or not reader_thread.is_alive()
        )
        _record_capture_connection_boundary(
            at=datetime.now(timezone.utc),
            connection_generation=connection_generation,
            active=False,
        )
        _retire_connection_generation(connection_generation)
        if connected_event is not None:
            connected_event.clear()
        stop_relay_done.set()
        if stop_relay_thread is not None:
            stop_relay_thread.join(timeout=1.0)
        stop_relay_quiesced = bool(
            stop_relay_thread is None or not stop_relay_thread.is_alive()
        )
        if not reader_quiesced or not stop_relay_quiesced:
            raise _ReaderQuiescenceError(
                "IQFeed reader/stop relay did not quiesce after socket close; "
                "refusing reconnect"
            )


def run_supervised(
    *,
    stop_event: threading.Event,
    schema_ready_event: threading.Event | None = None,
    connected_event: threading.Event | None = None,
    ready_event: threading.Event | None = None,
    forced_symbols: Iterable[str] = (),
    reconnect_wait_seconds: float = 10.0,
) -> None:
    """Run the L1 provider loop under an external, fail-closed supervisor.

    Unlike ``main``, this entry point never reads ``sys.argv`` and never allows
    uncaptured diagnostic posture.  It reports the concrete socket and
    protocol/selected-field readiness separately, clears both during every
    reconnect, and makes the reconnect delay interruptible by ``stop_event``.
    Terminal reader-quiescence failures escape to the owning supervisor.
    """

    if not callable(getattr(stop_event, "is_set", None)) or not callable(
        getattr(stop_event, "wait", None)
    ):
        raise TypeError("supervised IQFeed L1 stop event is malformed")
    for event, label in (
        (schema_ready_event, "schema-ready"),
        (connected_event, "connected"),
        (ready_event, "ready"),
    ):
        if event is not None and (
            not callable(getattr(event, "set", None))
            or not callable(getattr(event, "clear", None))
        ):
            raise TypeError(f"supervised IQFeed L1 {label} event is malformed")
    reconnect_wait = float(reconnect_wait_seconds)
    if not math.isfinite(reconnect_wait) or reconnect_wait <= 0:
        raise ValueError("supervised IQFeed L1 reconnect wait must be positive")
    forced = {
        str(symbol or "").strip().upper()
        for symbol in forced_symbols
        if str(symbol or "").strip()
    }
    for event in (schema_ready_event, connected_event, ready_event):
        if event is not None:
            event.clear()
    if stop_event.is_set():
        return
    _require_supervised_capture_posture()
    _capture_bc("posture ok; BEGIN schema gate")
    _verify_bridge_schema()
    _capture_bc("schema gate ok")
    if schema_ready_event is not None:
        schema_ready_event.set()
    try:
        while not stop_event.is_set():
            try:
                _run_connection(
                    forced,
                    None,
                    supervisor_stop_event=stop_event,
                    connected_event=connected_event,
                    ready_event=ready_event,
                )
            except _ReaderQuiescenceError:
                raise
            except SubscriptionConnectionIndeterminate as exc:
                _capture_bc(
                    "exc SubscriptionConnectionIndeterminate: "
                    + str(exc)[:140]
                )
                _log_subscription_gaps("iqfeed_l1", (exc.gap,))
                log.warning(
                    "supervised IQFeed L1 subscription state indeterminate; "
                    "reconnecting"
                )
            except Exception as exc:
                _capture_bc(f"exc {type(exc).__name__}: {str(exc)[:140]}")
                log.warning("supervised IQFeed L1 bridge error: %s", exc)
            if stop_event.is_set():
                break
            _capture_bc(f"reconnecting in {reconnect_wait:.1f}s")
            log.info(
                "supervised IQFeed L1 reconnecting in %.3fs",
                reconnect_wait,
            )
            if stop_event.wait(reconnect_wait):
                break
    finally:
        if ready_event is not None:
            ready_event.clear()
        if connected_event is not None:
            connected_event.clear()


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _verify_bridge_schema()
    _require_standalone_capture_posture()
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
    while deadline is None or time.monotonic() < deadline:
        try:
            _run_connection(forced, deadline)
        except KeyboardInterrupt:
            break
        except _ReaderQuiescenceError as e:
            log.critical("bridge terminal reconnect refusal: %s", e)
            break
        except SubscriptionConnectionIndeterminate as e:
            _log_subscription_gaps("iqfeed_l1", (e.gap,))
            log.warning("IQFeed L1 subscription state indeterminate; reconnecting")
        except Exception as e:
            log.warning("bridge error: %s", e)
        if deadline is not None and time.monotonic() >= deadline:
            break
        log.info("reconnecting in 10s…")
        time.sleep(10)
    log.info("trade bridge stopped")


if __name__ == "__main__":
    main()
