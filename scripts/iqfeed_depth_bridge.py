"""IQFeed Level 2 depth bridge — host-side daemon feeding CHILI real order-book depth.

IQConnect binds 127.0.0.1 only, so this runs ON THE WINDOWS HOST (chili-env
python) and bridges into CHILI through Postgres (the boundary containers
already share). Flow:

  IQConnect :9200 --(type-6 per-venue price-level frames)--> in-memory books
      --> top-of-book + 5-level aggregates + signed imbalance
      --> iqfeed_depth_snapshots rows every SNAP_INTERVAL_S per symbol

Symbols tracked include execution-relevant LIVE and PAPER equity sessions plus
the shared broad/hot sources, polled every REFRESH_S. The app-side consumer
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

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import socket
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - defensive for a broken host tz database
    _ET = None

import sqlalchemy as sa

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iqfeed_bridge")

HOST, PORT = "127.0.0.1", 9200
DB_URL = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
SNAP_INTERVAL_S = 2.0     # per-symbol snapshot write cadence
REFRESH_S = 20.0          # execution-session symbol refresh cadence
# STICKY RE-SUBSCRIBE (2026-06-16, the LION dead-L2 bug): IQFeed silently drops a
# per-symbol depth subscription. The loop only sent WOR/WPL on FIRST-SEEN, so once a
# held name was in ``watched`` it was never re-sent — LION's book went dark at 18:17
# while the position was held to 20:17 (33 other names kept streaming), blinding the
# exit (n_snaps=0 -> sell_into_strength gated 'stale_or_thin' 1111x). Re-send the depth
# subscription for every execution-active name each refresh so a silent drop self-heals.
# Idempotent (deduped by the venue book). =0 reverts to first-seen-only.
STICKY_RESUBSCRIBE = os.environ.get("CHILI_IQFEED_STICKY_RESUBSCRIBE", "1") != "0"
DEPTH_LEVELS = 5
STALE_VENUE_ROW_S = 900.0  # drop venue levels not refreshed in 15min (overnight ghosts)

# Keep the running bridge's broad pre-trigger coverage and add the alert path.
SUBSCRIBE_ON_ALERT = os.environ.get(
    "CHILI_MOMENTUM_BRIDGE_SUBSCRIBE_ON_ALERT_ENABLED", "1"
).strip().lower() not in ("0", "false", "no")
SUBSCRIBE_FAST_POLL_S = float(
    os.environ.get("IQFEED_DEPTH_SUBSCRIBE_FAST_POLL_S", "3") or 3
)
SUBSCRIBE_FRESH_WINDOW_S = float(
    os.environ.get("IQFEED_SUBSCRIBE_FRESH_WINDOW_S", "180") or 180
)
ELIGIBLE_FRESH_S = float(
    os.environ.get("CHILI_IQFEED_DEPTH_ELIGIBLE_FRESH_SECONDS", "1800") or 1800
)
DEPTH_WATCH_FLOOR = int(
    os.environ.get("CHILI_IQFEED_DEPTH_WATCH_FLOOR", "48") or 48
)
DEPTH_WATCH_HARD_MAX = int(
    os.environ.get("CHILI_IQFEED_DEPTH_WATCH_MAX", "128") or 128
)
READER_JOIN_TIMEOUT_S = float(
    os.environ.get("CHILI_IQFEED_DEPTH_READER_JOIN_TIMEOUT_SECONDS", "5") or 5
)
BRIDGE_VERSION = "iqfeed-l2-exact-frame-capture-v1"
BRIDGE_RUN_ID = str(uuid.uuid4())
UNCAPTURED_DIAGNOSTIC_FLAG = "--allow-uncaptured-diagnostic"
_CHECKPOINT_COMPLETION_BASIS = (
    "provider_snapshot_completion_boundary_unavailable"
)


def _bridge_source_sha256(path: str | Path = __file__) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        # The typed handoff rejects this value and records coverage loss.  Never
        # replace an unreadable source with a plausible arbitrary digest.
        return "source-unreadable"


BRIDGE_SOURCE_SHA256 = _bridge_source_sha256()
BRIDGE_BUILD = (
    f"{BRIDGE_VERSION}+sha256:{BRIDGE_SOURCE_SHA256[:16]}"
    if len(BRIDGE_SOURCE_SHA256) == 64
    else f"{BRIDGE_VERSION}+source-unreadable"
)
BRIDGE_CAPTURE_CONFIGURATION = {
    "schema_version": "chili.iqfeed-depth-bridge.capture-config.v1",
    "protocol": "6.2",
    "provider_timezone": "America/New_York",
    "message_type": "6",
    "message_fields": {
        "symbol": 1,
        "venue": 3,
        "side": 4,
        "price": 5,
        "size": 6,
        "condition_code": 8,
        "provider_time": 9,
        "provider_date": 10,
    },
    "initial_snapshot_completion_basis": _CHECKPOINT_COMPLETION_BASIS,
    "stale_venue_row_seconds": STALE_VENUE_ROW_S,
}
BRIDGE_CAPTURE_CONFIGURATION_SHA256 = hashlib.sha256(
    json.dumps(
        BRIDGE_CAPTURE_CONFIGURATION,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()

engine = sa.create_engine(DB_URL, pool_pre_ping=True)

IQFEED_SCHEMA_OWNER_MIGRATION_ID = "334_iqfeed_host_bridge_schema_ownership"
_DEPTH_REQUIRED_COLUMNS = frozenset(
    {
        "symbol",
        "observed_at",
        "bid_top",
        "ask_top",
        "bid_top_size",
        "ask_top_size",
        "bid5_size",
        "ask5_size",
        "imbalance5",
        "venues",
        "bids_json",
        "asks_json",
        "source",
    }
)


def _verify_depth_schema() -> None:
    """Read-only migration/schema gate which runs before any provider socket."""

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
            observed = frozenset(connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'iqfeed_depth_snapshots'"
                )
            ).scalars())
            missing = sorted(_DEPTH_REQUIRED_COLUMNS - observed)
            if missing:
                raise RuntimeError(
                    "IQFeed depth bridge schema is missing: "
                    + ", ".join(missing)
                )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "IQFeed depth bridge read-only schema verification failed"
        ) from exc


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float
    updated_monotonic: float
    provider_at: datetime | None
    received_at: datetime
    connection_generation: int
    source_frame_sequence: int
    source_frame_sha256: str
    condition_code: str


class Book:
    """Per-symbol venue book retaining each level's exact source provenance."""

    def __init__(self) -> None:
        self.levels: dict[tuple[str, str], BookLevel] = {}
        self.last_connection_generation: int | None = None
        self.last_source_frame_sequence: int | None = None
        self.last_source_frame_sha256: str | None = None

    def update(
        self,
        venue: str,
        side: str,
        price: float,
        size: float,
        *,
        provider_at: datetime | None = None,
        received_at: datetime | None = None,
        connection_generation: int | None = None,
        source_frame_sequence: int | None = None,
        source_frame_sha256: str | None = None,
        condition_code: str | None = None,
    ) -> bool:
        venue = str(venue or "").strip().upper()
        side = str(side or "").strip().upper()
        condition = str(condition_code or "").strip()
        legacy_unprovenanced = bool(
            received_at is None
            and connection_generation is None
            and source_frame_sequence is None
            and source_frame_sha256 is None
            and condition_code is None
            and provider_at is None
        )
        if legacy_unprovenanced:
            if (
                not venue
                or len(venue) > 16
                or side not in {"A", "B"}
                or isinstance(price, bool)
                or not isinstance(price, (int, float))
                or isinstance(size, bool)
                or not isinstance(size, (int, float))
                or not math.isfinite(price)
                or not math.isfinite(size)
                or price <= 0
                or size < 0
            ):
                return False
            identity = (venue, side)
            if size == 0:
                self.levels.pop(identity, None)
                return True
            self.levels[identity] = BookLevel(
                price=float(price),
                size=float(size),
                updated_monotonic=time.monotonic(),
                provider_at=None,
                received_at=datetime.now(timezone.utc),
                connection_generation=0,
                source_frame_sequence=0,
                source_frame_sha256="0" * 64,
                condition_code="legacy_unprovenanced",
            )
            return True
        if (
            not isinstance(received_at, datetime)
            or received_at.tzinfo is None
            or (
                provider_at is not None
                and (
                    not isinstance(provider_at, datetime)
                    or provider_at.tzinfo is None
                )
            )
        ):
            return False
        received = received_at.astimezone(timezone.utc)
        provider = (
            None
            if provider_at is None
            else provider_at.astimezone(timezone.utc)
        )
        if (
            not venue
            or len(venue) > 16
            or side not in {"A", "B"}
            or not condition
            or isinstance(price, bool)
            or not isinstance(price, (int, float))
            or isinstance(size, bool)
            or not isinstance(size, (int, float))
            or not math.isfinite(price)
            or not math.isfinite(size)
            or price <= 0
            or size < 0
            or isinstance(connection_generation, bool)
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
            or isinstance(source_frame_sequence, bool)
            or not isinstance(source_frame_sequence, int)
            or source_frame_sequence <= 0
            or not isinstance(source_frame_sha256, str)
            or len(source_frame_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in source_frame_sha256)
        ):
            return False
        generation = int(connection_generation)
        sequence = int(source_frame_sequence)
        if (
            self.last_connection_generation is not None
            and generation != self.last_connection_generation
        ):
            # A Book instance is connection-local; cross-generation reuse would
            # contaminate a checkpoint even if the numeric frame fields look valid.
            return False
        if (
            self.last_source_frame_sequence is not None
            and sequence <= self.last_source_frame_sequence
        ):
            return False
        self.last_connection_generation = generation
        self.last_source_frame_sequence = sequence
        self.last_source_frame_sha256 = source_frame_sha256
        identity = (venue, side)
        if size == 0:
            self.levels.pop(identity, None)
            return True
        self.levels[identity] = BookLevel(
            price=float(price),
            size=float(size),
            updated_monotonic=time.monotonic(),
            provider_at=provider,
            received_at=received,
            connection_generation=generation,
            source_frame_sequence=sequence,
            source_frame_sha256=source_frame_sha256,
            condition_code=condition,
        )
        return True

    def snapshot(self) -> dict | None:
        now = time.monotonic()
        bids, asks = [], []
        active_venues: set[str] = set()
        for (venue, side), level in self.levels.items():
            if now - level.updated_monotonic > STALE_VENUE_ROW_S or level.size <= 0:
                continue
            active_venues.add(venue)
            (bids if side == "B" else asks).append((level.price, level.size))
        if not bids or not asks:
            return None
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        if bids[0][0] >= asks[0][0] * 1.05:  # crossed >5% = ghost venue rows
            return None
        b5 = sum(sz for _, sz in bids[:DEPTH_LEVELS])
        a5 = sum(sz for _, sz in asks[:DEPTH_LEVELS])
        tot = b5 + a5
        bids_json = [[round(px, 6), round(sz, 4)] for px, sz in bids[:DEPTH_LEVELS]]
        asks_json = [[round(px, 6), round(sz, 4)] for px, sz in asks[:DEPTH_LEVELS]]
        return {
            "bid_top": bids[0][0], "ask_top": asks[0][0],
            "bid_top_size": bids[0][1], "ask_top_size": asks[0][1],
            "bid5_size": b5, "ask5_size": a5,
            "imbalance5": round((b5 - a5) / tot, 4) if tot > 0 else None,
            "venues": len(active_venues),
            "bids_json": bids_json,
            "asks_json": asks_json,
        }

    def capture_checkpoint(
        self,
        *,
        symbol: str,
        received_at: datetime,
    ) -> dict[str, Any] | None:
        """Assemble a local book checkpoint without claiming provider completion."""

        generation = self.last_connection_generation
        covered = self.last_source_frame_sequence
        covered_hash = self.last_source_frame_sha256
        if generation is None or covered is None or covered_hash is None:
            return None
        now = time.monotonic()
        levels = []
        for (venue, side), level in sorted(
            self.levels.items(), key=lambda item: (item[0][1], item[0][0])
        ):
            if (
                level.connection_generation != generation
                or level.size <= 0
                or now - level.updated_monotonic > STALE_VENUE_ROW_S
            ):
                continue
            levels.append(
                {
                    "venue": venue,
                    "side": side,
                    "px": level.price,
                    "sz": level.size,
                    "provider_at": level.provider_at,
                    "connection_generation": level.connection_generation,
                    "source_frame_sequence": level.source_frame_sequence,
                    "source_frame_sha256": level.source_frame_sha256,
                    "condition_code": level.condition_code,
                }
            )
        if not levels:
            return None
        return {
            "sym": str(symbol or "").strip().upper(),
            "received_at": received_at.astimezone(timezone.utc),
            "bridge": BRIDGE_BUILD,
            "bridge_run_id": BRIDGE_RUN_ID,
            "connection_generation": generation,
            "covered_through_source_frame_sequence": covered,
            "covered_through_source_frame_sha256": covered_hash,
            "initial_snapshot_complete": False,
            "completion_basis": _CHECKPOINT_COMPLETION_BASIS,
            "levels": levels,
        }


books: dict[str, Book] = defaultdict(Book)
books_lock = threading.RLock()
watched: set[str] = set()
sock_lock = threading.Lock()
sock: socket.socket | None = None
running = True
_max_watch = DEPTH_WATCH_HARD_MAX
_limit_hit = False
_connection_state_lock = threading.Lock()
_connection_generation = 0
_active_connection_generation = 0
_frame_sequence_by_generation: dict[int, int] = {}
_capture_handoff_lock = threading.Lock()
_capture_handoff: Any | None = None
_capture_hot_symbols: set[str] = set()
_capture_checkpointed_generation: dict[str, int] = {}


class _DepthReaderQuiescenceError(RuntimeError):
    """The prior socket reader survived close, so reconnect must be refused."""

    provider_reader_may_be_alive = True


def _begin_connection_generation() -> int:
    global _connection_generation, _active_connection_generation
    with _connection_state_lock:
        _connection_generation += 1
        _active_connection_generation = _connection_generation
        _frame_sequence_by_generation[_connection_generation] = 0
        return _connection_generation


def _connection_generation_active(
    connection_generation: int,
    stop_event: threading.Event,
) -> bool:
    with _connection_state_lock:
        return bool(
            _active_connection_generation == int(connection_generation)
            and not stop_event.is_set()
        )


def _next_source_frame_sequence(connection_generation: int) -> int:
    generation = int(connection_generation)
    if generation <= 0:
        raise ValueError("IQFeed L2 source frame generation must be positive")
    with _connection_state_lock:
        if _active_connection_generation != generation:
            raise ValueError("IQFeed L2 source frame generation is not active")
        sequence = _frame_sequence_by_generation.get(generation, 0) + 1
        _frame_sequence_by_generation[generation] = sequence
        return sequence


def _request_connection_stop(
    connection_generation: int,
    stop_event: threading.Event,
) -> bool:
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


def bind_capture_handoff(handoff: Any) -> None:
    """Bind one already-started, bounded no-fetch capture handoff.

    The bridge never creates account/run authority itself.  Binding is only
    permitted while disconnected so a producer cannot silently join halfway
    through a provider generation.
    """

    required = (
        "activate_hot_symbol",
        "offer_delta_rows",
        "deactivate_hot_symbol",
        "record_connection_boundary",
        "record_release_failure",
        "health",
    )
    if any(not callable(getattr(handoff, name, None)) for name in required):
        raise TypeError("IQFeed L2 capture handoff is malformed")
    health = handoff.health()
    if not health.get("started") or not health.get("accepting"):
        raise RuntimeError("IQFeed L2 capture handoff must be started before binding")
    global _capture_handoff
    with _connection_state_lock:
        if _active_connection_generation != 0:
            raise RuntimeError("IQFeed L2 capture handoff cannot bind mid-connection")
        with _capture_handoff_lock:
            if _capture_handoff is not None:
                raise RuntimeError("IQFeed L2 capture handoff is already bound")
            _capture_handoff = handoff


def unbind_capture_handoff(handoff: Any) -> None:
    """Unbind the exact handoff only after the provider reader has quiesced."""

    global _capture_handoff
    with _connection_state_lock:
        if _active_connection_generation != 0:
            raise RuntimeError("IQFeed L2 capture handoff cannot unbind mid-connection")
        with _capture_handoff_lock:
            if _capture_handoff is not handoff:
                raise RuntimeError("IQFeed L2 capture handoff ownership mismatch")
            _capture_handoff = None


def _require_standalone_capture_posture() -> None:
    """Refuse a provider socket unless capture is bound or explicitly diagnostic."""

    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is not None:
        return
    if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
        log.critical(
            "IQFeed L2 running in explicit uncaptured diagnostic mode; "
            "all ReplayV3 depth coverage from this process is unavailable"
        )
        return
    raise RuntimeError(
        "IQFeed L2 capture handoff must be bound before provider connection; "
        f"use {UNCAPTURED_DIAGNOSTIC_FLAG} only for non-certifying diagnostics"
    )


def _require_supervised_capture_posture() -> None:
    """Require the host-bound L2 authority without consulting ``sys.argv``."""

    with _capture_handoff_lock:
        handoff = _capture_handoff
    if handoff is None:
        raise RuntimeError(
            "supervised IQFeed L2 requires a bound capture handoff"
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


def _send(cmd: str) -> None:
    with sock_lock:
        if sock is None:
            raise ConnectionError("IQFeed L2 socket is not connected")
        sock.sendall((cmd + "\r\n").encode())


def _live_symbols() -> set[str]:
    return set(_live_symbols_read().symbols)


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
    return list(_eligible_symbols_read(limit).symbols)


def _eligible_symbols_read(limit: int) -> SourceRead:
    if limit <= 0:
        return SourceRead.success(TargetCause.ELIGIBLE, ())
    try:
        with engine.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT symbol FROM ("
                    "  SELECT DISTINCT ON (symbol) symbol, viability_score FROM momentum_symbol_viability "
                    "  WHERE symbol NOT LIKE '%-USD' AND (live_eligible OR paper_eligible) "
                    "    AND freshness_ts > (now() at time zone 'utc') - make_interval(secs => :fresh) "
                    "  ORDER BY symbol, freshness_ts DESC"
                    ") q ORDER BY viability_score DESC NULLS LAST, symbol ASC LIMIT :lim"
                ),
                {"fresh": ELIGIBLE_FRESH_S, "lim": int(limit)},
            ).fetchall()
        return SourceRead.success(TargetCause.ELIGIBLE, (str(row[0]) for row in rows))
    except Exception as exc:
        log.warning("eligible query failed: %s", exc)
        return SourceRead.failure(
            TargetCause.ELIGIBLE,
            error_code="eligible_query_failed",
            error_detail=str(exc),
        )


def _alert_symbols(
    fresh_window_s: float,
    limit: int = DEPTH_WATCH_HARD_MAX,
) -> list[str]:
    return list(_alert_symbols_read(fresh_window_s, limit=limit).symbols)


def _alert_symbols_read(
    fresh_window_s: float,
    *,
    limit: int = DEPTH_WATCH_HARD_MAX,
) -> SourceRead:
    try:
        with engine.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT symbol FROM ("
                    "  SELECT symbol, max(requested_at) AS freshest "
                    "  FROM momentum_bridge_subscribe_requests "
                    "  WHERE requested_at > (now() at time zone 'utc') - make_interval(secs => :w) "
                    "    AND symbol NOT LIKE '%-%' "
                    "  GROUP BY symbol"
                    ") q ORDER BY freshest DESC, symbol ASC LIMIT :lim"
                ),
                {"w": float(fresh_window_s), "lim": max(0, int(limit))},
            ).fetchall()
        return SourceRead.success(TargetCause.HINT, (str(row[0]) for row in rows))
    except Exception as exc:
        log.warning("hint query failed: %s", exc)
        return SourceRead.failure(
            TargetCause.HINT,
            error_code="hint_query_failed",
            error_detail=str(exc),
        )


def _ross_universe_symbols(limit: int) -> list[str]:
    return list(_ross_universe_symbols_read(limit).symbols)


def _ross_universe_symbols_read(limit: int) -> SourceRead:
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
            # build_equity_universe collapses fetch errors to []; preserve the
            # prior target because empty-vs-unavailable cannot be proven here.
            return SourceRead.failure(
                TargetCause.ROSS,
                error_code="ross_universe_empty_or_unavailable",
            )
        return result
    except Exception as exc:
        log.warning("ross universe query failed: %s", exc)
        return SourceRead.failure(
            TargetCause.ROSS,
            error_code="ross_query_failed",
            error_detail=str(exc),
        )


def _resolve_target(
    *,
    reads: list[SourceRead],
    prior_causes: dict[str, frozenset[TargetCause]],
    capacity: int,
) -> TargetResolution:
    require_complete_source_inventory(reads)
    return resolve_subscription_target(
        reads=reads,
        prior_causes=prior_causes,
        capacity=capacity,
    )


def _log_subscription_gaps(gaps: tuple[CoverageGap, ...]) -> None:
    for gap in gaps:
        log.warning(
            "subscription coverage unavailable feed=iqfeed_l2 code=%s source=%s symbol=%s causes=%s detail=%s",
            gap.code,
            gap.source,
            gap.symbol or "-",
            ",".join(cause.value for cause in gap.causes) or "-",
            gap.detail or "-",
        )


def _try_watch_symbol(
    symbol: str,
    *,
    causes: tuple[TargetCause, ...] = (),
    watched_set: set[str] | None = None,
) -> None:
    state = watched if watched_set is None else watched_set
    _send_depth_subscription_sequence(
        commands=(f"WOR,{symbol}", f"WPL,{symbol}", f"w{symbol}"),
        action="watch",
        symbol=symbol,
        causes=causes,
        watched_set=state,
    )
    state.add(symbol)


def _try_sticky_resubscribe_symbol(
    symbol: str,
    *,
    causes: tuple[TargetCause, ...] = (),
    watched_set: set[str] | None = None,
) -> None:
    state = watched if watched_set is None else watched_set
    _send_depth_subscription_sequence(
        commands=(f"WOR,{symbol}", f"WPL,{symbol}", f"w{symbol}"),
        action="sticky_resubscribe",
        symbol=symbol,
        causes=causes,
        watched_set=state,
    )


def _try_unwatch_symbol(
    symbol: str,
    *,
    causes: tuple[TargetCause, ...] = (),
    watched_set: set[str] | None = None,
) -> None:
    state = watched if watched_set is None else watched_set
    _send_depth_subscription_sequence(
        commands=(f"ROR,{symbol}", f"RPL,{symbol}", f"r{symbol}"),
        action="unwatch",
        symbol=symbol,
        causes=causes,
        watched_set=state,
    )
    state.discard(symbol)


def _send_depth_subscription_sequence(
    *,
    commands: tuple[str, ...],
    action: str,
    symbol: str,
    watched_set: set[str],
    causes: tuple[TargetCause, ...] = (),
) -> None:
    """Send an all-or-reconnect L2 subscription command sequence.

    IQFeed exposes no transaction or durable per-symbol ACK across WOR/WPL/w.
    If command N raises, commands before N may already be active and command N
    itself may be partially transmitted.  There is no truthful local rollback;
    clear the entire connection-local watch claim and force the writer to escape
    into the reconnect loop.
    """
    total = len(commands)
    for index, command in enumerate(commands, start=1):
        try:
            _send(command)
        except Exception as exc:
            watched_set.clear()
            raise SubscriptionConnectionIndeterminate(
                CoverageGap(
                    code=f"{action}_send_indeterminate",
                    source="iqfeed_l2",
                    symbol=symbol,
                    causes=causes,
                    detail=(
                        f"command_index={index}/{total}; "
                        "connection invalidation required; "
                        + str(exc)[:384]
                    ),
                )
            ) from exc


def _parse_l2_provider_at(date_text: str, time_text: str) -> datetime | None:
    """Parse the exact ET date/time carried by one IQFeed type-6 frame."""

    if _ET is None:
        return None
    raw_date = str(date_text or "").strip()
    raw_time = str(time_text or "").strip()
    if not raw_date or not raw_time:
        return None
    try:
        local = datetime.fromisoformat(f"{raw_date}T{raw_time}")
    except ValueError:
        return None
    if local.tzinfo is not None:
        return None
    try:
        return local.replace(tzinfo=_ET).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _bound_capture_handoff() -> Any | None:
    with _capture_handoff_lock:
        return _capture_handoff


def _activate_capture_symbol_locked(
    symbol: str,
    *,
    available_at: datetime,
) -> bool:
    """Queue the current checkpoint while the book/order lock is held."""

    handoff = _bound_capture_handoff()
    if handoff is None:
        if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
            log.error(
                "IQFeed L2 checkpoint coverage unavailable: %s",
                json.dumps(
                    {
                        "code": "iqfeed_l2_capture_handoff_unbound_diagnostic",
                        "symbol": symbol,
                        "available_at": available_at.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return False
        raise RuntimeError(
            "IQFeed L2 capture handoff is unbound; refusing silent checkpoint loss"
        )
    book = books.get(symbol)
    checkpoint = (
        None
        if book is None
        else book.capture_checkpoint(symbol=symbol, received_at=available_at)
    )
    if checkpoint is None:
        # This deliberately malformed checkpoint registers the hot request and
        # an explicit coverage-unavailable fact in the bounded handoff.  It can
        # never be mistaken for a provider-complete snapshot.
        try:
            handoff.activate_hot_symbol(
                {"sym": symbol},
                available_at=available_at,
            )
        except Exception:
            log.exception("IQFeed L2 empty checkpoint loss accounting failed")
        return False
    try:
        accepted = bool(
            handoff.activate_hot_symbol(
                checkpoint,
                available_at=available_at,
            )
        )
    except Exception as exc:
        try:
            handoff.record_release_failure(
                rows=[checkpoint],
                available_at=available_at,
            )
        except Exception:
            log.exception(
                "IQFeed L2 checkpoint handoff and loss accounting both failed"
            )
        else:
            log.exception("IQFeed L2 checkpoint handoff failed: %s", exc)
        return False
    if accepted:
        _capture_checkpointed_generation[symbol] = int(
            checkpoint["connection_generation"]
        )
    return accepted


def activate_capture_symbol(
    symbol: str,
    *,
    available_at: datetime | None = None,
) -> bool:
    """Request full-fidelity L2 capture for one runtime-admitted hot symbol."""

    normalized = str(symbol or "").strip().upper()
    if (
        not normalized
        or len(normalized) > 16
        or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789." for ch in normalized)
    ):
        raise ValueError("IQFeed L2 capture symbol is malformed")
    at = available_at or datetime.now(timezone.utc)
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("IQFeed L2 capture activation time must be timezone-aware")
    at = at.astimezone(timezone.utc)
    with books_lock:
        _capture_hot_symbols.add(normalized)
        return _activate_capture_symbol_locked(normalized, available_at=at)


def deactivate_capture_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("IQFeed L2 capture symbol is required")
    with books_lock:
        existed = normalized in _capture_hot_symbols
        _capture_hot_symbols.discard(normalized)
        _capture_checkpointed_generation.pop(normalized, None)
        handoff = _bound_capture_handoff()
        if handoff is not None:
            try:
                handoff.deactivate_hot_symbol(normalized)
            except Exception:
                log.exception("IQFeed L2 capture deactivation handoff failed")
        return existed


def _record_capture_connection_boundary(
    *,
    at: datetime,
    connection_generation: int,
    active: bool,
) -> None:
    with books_lock:
        _capture_checkpointed_generation.clear()
    handoff = _bound_capture_handoff()
    if handoff is None:
        if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
            log.error(
                "IQFeed L2 connection-boundary coverage unavailable: %s",
                json.dumps(
                    {
                        "code": "iqfeed_l2_connection_boundary_unbound_diagnostic",
                        "bridge_run_id": BRIDGE_RUN_ID,
                        "connection_generation": connection_generation,
                        "active": active,
                        "available_at": at.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return
        raise RuntimeError(
            "IQFeed L2 capture handoff is unbound; refusing silent connection boundary"
        )
    try:
        handoff.record_connection_boundary(
            at=at,
            bridge_run_id=BRIDGE_RUN_ID,
            connection_generation=connection_generation,
            active=active,
        )
    except Exception:
        # The bridge must not crash the provider reader, but a capture failure is
        # never hidden: the bound handoff health/close remains fail-closed.
        log.exception("IQFeed L2 capture connection-boundary handoff failed")


def _publish_capture_delta_locked(
    row: dict[str, Any],
    *,
    available_at: datetime,
    allow_recheckpoint: bool,
) -> tuple[int, int, int]:
    """Publish one already-observed row without blocking or fetching anything."""

    symbol = str(row.get("sym") or "").strip().upper()
    handoff = _bound_capture_handoff()
    if not symbol:
        if handoff is None or not _capture_hot_symbols:
            return (0, 0, 1)
        try:
            return tuple(
                int(value)
                for value in handoff.offer_delta_rows(
                    [row], available_at=available_at
                )
            )
        except Exception:
            try:
                lost = handoff.record_release_failure(
                    rows=[row], available_at=available_at
                )
            except Exception:
                log.exception(
                    "IQFeed L2 unattributed frame and loss accounting both failed"
                )
                lost = max(1, len(_capture_hot_symbols))
            else:
                log.exception("IQFeed L2 unattributed frame handoff failed")
            return (0, max(1, int(lost)), 0)
    if symbol not in _capture_hot_symbols:
        return (0, 0, 1)
    if handoff is None:
        if UNCAPTURED_DIAGNOSTIC_FLAG in sys.argv:
            log.error(
                "IQFeed L2 delta coverage unavailable: %s",
                json.dumps(
                    {
                        "code": "iqfeed_l2_delta_unbound_diagnostic",
                        "symbol": symbol,
                        "available_at": available_at.astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return (0, 1, 0)
        raise RuntimeError(
            "IQFeed L2 capture handoff is unbound; refusing silent delta loss"
        )
    generation = row.get("connection_generation")
    if (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and _capture_checkpointed_generation.get(symbol) != generation
    ):
        checkpointed = _activate_capture_symbol_locked(
            symbol, available_at=available_at
        )
        if checkpointed and allow_recheckpoint:
            return (1, 0, 0)
    try:
        result = handoff.offer_delta_rows([row], available_at=available_at)
    except Exception as exc:
        try:
            lost = handoff.record_release_failure(
                rows=[row],
                available_at=available_at,
            )
        except Exception:
            log.exception("IQFeed L2 delta handoff and loss accounting both failed")
            lost = 1
        log.exception(
            "IQFeed L2 replay capture handoff failed; coverage gapped for %d row: %s",
            lost,
            exc,
        )
        _capture_checkpointed_generation.pop(symbol, None)
        return (0, max(1, int(lost)), 0)
    accepted, rejected, ignored = result
    if rejected:
        _capture_checkpointed_generation.pop(symbol, None)
        if allow_recheckpoint:
            _activate_capture_symbol_locked(symbol, available_at=available_at)
    return int(accepted), int(rejected), int(ignored)


def reader(
    connection_socket: socket.socket,
    stop_event: threading.Event,
    connection_generation: int,
) -> None:
    global running, _limit_hit
    buf = b""
    frames = 0
    while running and _connection_generation_active(
        connection_generation, stop_event
    ):
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
            if frames < 12 and line and line[0] not in ("T",):
                log.info("raw[%d]: %s", frames, line[:140])
            if not line or line[0] in ("T",):
                continue
            if line[0] == "6":
                received_at = datetime.now(timezone.utc)
                try:
                    source_frame_sequence = _next_source_frame_sequence(
                        connection_generation
                    )
                except ValueError:
                    stop_event.set()
                    break
                source_frame_sha256 = hashlib.sha256(raw).hexdigest()
                p = line.split(",")
                symbol = str(p[1] if len(p) > 1 else "").strip().upper()
                if (
                    not symbol
                    or len(symbol) > 16
                    or any(
                        ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789."
                        for ch in symbol
                    )
                ):
                    symbol = ""
                base_row: dict[str, Any] = {
                    "sym": symbol,
                    "received_at": received_at,
                    "bridge": BRIDGE_BUILD,
                    "bridge_run_id": BRIDGE_RUN_ID,
                    "connection_generation": connection_generation,
                    "source_frame_sequence": source_frame_sequence,
                    "source_frame_sha256": source_frame_sha256,
                }
                parsed_row = dict(base_row)
                provider_at: datetime | None = None
                parsed_values: tuple[str, str, float, float, str] | None = None
                if len(p) >= 11:
                    provider_at = _parse_l2_provider_at(p[10], p[9])
                    try:
                        venue = str(p[3] or "").strip().upper()
                        side = str(p[4] or "").strip().upper()
                        price = float(p[5])
                        size = float(p[6])
                        condition_code = str(p[8] or "").strip()
                    except (TypeError, ValueError, IndexError):
                        pass
                    else:
                        parsed_row.update(
                            {
                                "venue": venue,
                                "side": side,
                                "px": price,
                                "sz": size,
                                "condition_code": condition_code,
                                "provider_at": provider_at,
                            }
                        )
                        parsed_values = (
                            venue,
                            side,
                            price,
                            size,
                            condition_code,
                        )
                available_at = datetime.now(timezone.utc)
                with books_lock:
                    updated = False
                    if symbol and parsed_values is not None:
                        venue, side, price, size, condition_code = parsed_values
                        updated = books[symbol].update(
                            venue,
                            side,
                            price,
                            size,
                            provider_at=provider_at,
                            received_at=received_at,
                            connection_generation=connection_generation,
                            source_frame_sequence=source_frame_sequence,
                            source_frame_sha256=source_frame_sha256,
                            condition_code=condition_code,
                        )
                    _publish_capture_delta_locked(
                        parsed_row,
                        available_at=available_at,
                        allow_recheckpoint=bool(updated and provider_at is not None),
                    )
                frames += 1
            elif line.startswith("S,") or line[0] in ("n", "E"):
                if any(
                    marker in line.upper()
                    for marker in (
                        "SYMBOL LIMIT",
                        "MAX SYMBOL",
                        "LIMIT REACHED",
                        "TOO MANY SYMBOL",
                    )
                ):
                    _limit_hit = True
                    log.warning("IQFeed depth symbol-limit signal: %s", line[:160])
                else:
                    log.info("feed: %s", line[:160])
    stop_event.set()
    running = False


def writer(forced_syms: set[str], deadline: float | None) -> None:
    global running, _max_watch, _limit_hit
    last_refresh = 0.0
    last_fast_sub = 0.0
    # Defer the first retention sweep (same starvation as the L1 bridge): a
    # first-iteration DELETE scan on iqfeed_depth_snapshots blocked the first
    # depth watch ~68s past connect, far beyond the capture-smoke window.
    last_prune = time.monotonic()
    ins = sa.text(
        "INSERT INTO iqfeed_depth_snapshots (symbol, observed_at, bid_top, ask_top, "
        "bid_top_size, ask_top_size, bid5_size, ask5_size, imbalance5, venues, "
        "bids_json, asks_json) "
        "VALUES (:sym, :at, :bt, :at2, :bts, :ats, :b5, :a5, :imb, :v, "
        "CAST(:bids AS JSONB), CAST(:asks AS JSONB))"
    )
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

    def reconcile(*, allow_unwatch: bool, sticky_active: bool = False) -> None:
        nonlocal prior_causes
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
        _log_subscription_gaps(resolution.gaps)
        if sticky_active:
            active_symbols = {
                target.symbol
                for target in resolution.targets
                if TargetCause.ACTIVE in target.causes
            }
            with books_lock:
                dark_active = [
                    symbol
                    for symbol in sorted(active_symbols)
                    if symbol not in books or books[symbol].snapshot() is None
                ]
            if dark_active:
                log.warning(
                    "depth re-subscribe (active book empty): %s",
                    ",".join(dark_active),
                )
        if allow_unwatch:
            for symbol in sorted(watched - resolution.symbols):
                _try_unwatch_symbol(
                    symbol,
                    causes=tuple(
                        cause
                        for cause in TargetCause
                        if cause in prior_causes.get(symbol, ())
                    ),
                )
                prior_causes.pop(symbol, None)
                with books_lock:
                    books.pop(symbol, None)
        for target in resolution.targets:
            symbol = target.symbol
            if symbol in watched:
                prior_causes[symbol] = frozenset(target.causes)
                if sticky_active and TargetCause.ACTIVE in target.causes:
                    _try_sticky_resubscribe_symbol(
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
                    (
                        CoverageGap(
                            code="watch_capacity_not_freed",
                            source="iqfeed_l2",
                            symbol=symbol,
                            causes=target.causes,
                            detail=(
                                f"watched={len(watched)} "
                                f"capacity={resolution.capacity}"
                            ),
                        ),
                    )
                )
                continue
            _try_watch_symbol(symbol, causes=target.causes)
            prior_causes[symbol] = frozenset(target.causes)
            log.info(
                "watching depth: %s causes=%s",
                symbol,
                ",".join(cause.value for cause in target.causes),
            )

    while running and (deadline is None or time.monotonic() < deadline):
        time.sleep(SNAP_INTERVAL_S)
        if (
            SUBSCRIBE_ON_ALERT
            and not forced_syms
            and time.monotonic() - last_fast_sub >= SUBSCRIBE_FAST_POLL_S
        ):
            last_fast_sub = time.monotonic()
            source_reads[TargetCause.HINT] = _alert_symbols_read(
                SUBSCRIBE_FRESH_WINDOW_S,
                limit=_max_watch,
            )
            reconcile(allow_unwatch=True)
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
            if _limit_hit:
                _max_watch = max(DEPTH_WATCH_FLOOR, _max_watch // 2)
                _limit_hit = False
                log.warning(
                    "depth watch cap halved -> %d (IQFeed L2 symbol-limit)",
                    _max_watch,
                )
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
            reconcile(allow_unwatch=True, sticky_active=STICKY_RESUBSCRIBE)
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
                         "imb": snap["imbalance5"], "v": snap["venues"],
                         "bids": json.dumps(snap["bids_json"]),
                         "asks": json.dumps(snap["asks_json"])})
        if rows:
            try:
                with engine.begin() as c:
                    c.execute(ins, rows)
            except Exception as e:
                log.warning("snapshot insert failed (%d rows): %s", len(rows), e)
    running = False


def _run_connection(
    forced: set[str],
    deadline: float | None,
    *,
    supervisor_stop_event: threading.Event | None = None,
    connected_event: threading.Event | None = None,
    ready_event: threading.Event | None = None,
) -> None:
    """Own and fully retire one L2 socket before reconnect is permitted."""
    global sock, running
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
                name=f"iqfeed-depth-stop-relay-g{connection_generation}",
            )
            stop_relay_thread.start()
        connection_socket.settimeout(2.0)
        with sock_lock:
            sock = connection_socket
        running = True
        watched.clear()
        with books_lock:
            books.clear()
            _capture_checkpointed_generation.clear()
        _record_capture_connection_boundary(
            at=datetime.now(timezone.utc),
            connection_generation=connection_generation,
            active=True,
        )
        _send("S,SET PROTOCOL,6.2")
        log.info(
            "connected to IQConnect %s:%s (protocol 6.2) run=%s generation=%d",
            HOST,
            PORT,
            BRIDGE_RUN_ID,
            connection_generation,
        )
        reader_thread = threading.Thread(
            target=reader,
            args=(connection_socket, stop_event, connection_generation),
            daemon=True,
            name=f"iqfeed-depth-reader-g{connection_generation}",
        )
        reader_thread.start()
        if supervisor_stop_event is not None and supervisor_stop_event.is_set():
            return
        if not reader_thread.is_alive() or stop_event.is_set():
            raise RuntimeError("IQFeed L2 reader stopped before readiness")
        if ready_event is not None:
            ready_event.set()
        writer(forced, deadline)
    finally:
        if ready_event is not None:
            ready_event.clear()
        running = False
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
        with sock_lock:
            if sock is connection_socket:
                sock = None
        if connected_event is not None:
            connected_event.clear()
        stop_relay_done.set()
        if stop_relay_thread is not None:
            stop_relay_thread.join(timeout=1.0)
        stop_relay_quiesced = bool(
            stop_relay_thread is None or not stop_relay_thread.is_alive()
        )
        if not reader_quiesced or not stop_relay_quiesced:
            raise _DepthReaderQuiescenceError(
                "IQFeed L2 reader/stop relay did not quiesce after socket close; "
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
    """Run the L2 provider loop under an external, fail-closed supervisor.

    The supervised path requires an already-bound capture handoff, reports
    current socket/readiness state, and does not inspect command-line flags.
    Provider reconnect waits are interruptible; a reader that cannot be joined
    is terminal and escapes to the owning host supervisor.
    """

    if not callable(getattr(stop_event, "is_set", None)) or not callable(
        getattr(stop_event, "wait", None)
    ):
        raise TypeError("supervised IQFeed L2 stop event is malformed")
    for event, label in (
        (schema_ready_event, "schema-ready"),
        (connected_event, "connected"),
        (ready_event, "ready"),
    ):
        if event is not None and (
            not callable(getattr(event, "set", None))
            or not callable(getattr(event, "clear", None))
        ):
            raise TypeError(f"supervised IQFeed L2 {label} event is malformed")
    reconnect_wait = float(reconnect_wait_seconds)
    if not math.isfinite(reconnect_wait) or reconnect_wait <= 0:
        raise ValueError("supervised IQFeed L2 reconnect wait must be positive")
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
    _verify_depth_schema()
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
            except _DepthReaderQuiescenceError:
                raise
            except SubscriptionConnectionIndeterminate as exc:
                _log_subscription_gaps((exc.gap,))
                log.warning(
                    "supervised IQFeed L2 subscription state indeterminate; "
                    "reconnecting"
                )
            except Exception as exc:
                log.warning("supervised IQFeed L2 bridge error: %s", exc)
            if stop_event.is_set():
                break
            log.info(
                "supervised IQFeed L2 reconnecting in %.3fs",
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
    _verify_depth_schema()
    _require_standalone_capture_posture()
    forced = {a.upper() for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()}
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.monotonic() + float(sys.argv[sys.argv.index("--seconds") + 1])
    # Reconnect loop: IQConnect restarts (relogin, updates) must not kill the
    # bridge — drop state, reconnect, re-watch (watched set resets so the
    # refresh pass re-subscribes everything).
    while deadline is None or time.monotonic() < deadline:
        try:
            _run_connection(forced, deadline)
        except KeyboardInterrupt:
            break
        except _DepthReaderQuiescenceError as e:
            log.critical("bridge terminal reconnect refusal: %s", e)
            break
        except SubscriptionConnectionIndeterminate as e:
            _log_subscription_gaps((e.gap,))
            log.warning("IQFeed L2 subscription state indeterminate; reconnecting")
        except Exception as e:
            log.warning("bridge error: %s", e)
        if deadline is not None and time.monotonic() >= deadline:
            break
        log.info("reconnecting in 10s…")
        time.sleep(10)
    log.info("bridge stopped")


if __name__ == "__main__":
    main()
