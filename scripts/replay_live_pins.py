"""REPLAY LIVE PINS -- the two numbers a replay cannot get from its tape and must take from
LIVE, derived once per bench, pinned, and shared by every arm.

WHY THIS FILE EXISTS ([E], 2026-09-11). Two harness defects kept the canon A/B from
measuring the exit doctrine that is actually on main:

1. PUBLICATION CLOCKS. Since #1392 (987e2b2ad) and #1385 (140fd0f08) every print read the
   lane makes carries ``received_at <= :available_by AND available_at <= :available_by``
   (``tape_selection.signed_tape_query``, ``entry_gates._VERDICT_AVAILABLE_BOUND`` ->
   ``leg_prints_between`` / ``leg_prints_since_high``). The replay mirror inserted
   ``(symbol, observed_at, price, size, bid, ask, source)`` only, so in the sink both clocks
   were NULL and EVERY one of those reads returned zero rows: the whole-sale G/D verdict and
   the deadman walk could never decide, and the tape-gated entry triggers failed closed.
   The hydrated source cannot supply the clocks either: ``iqfeed_lookup_hist`` rows carry
   ``received_at`` = the HYDRATION wall time (VEEE 2026-07-13 -> 2026-09-03 06:07:18Z) and
   ``available_at`` NULL. So the clocks are DERIVED from the live tape's own publication-lag
   distribution and stamped ``observed_at + p50``.

2. BROKER MULTIPLIER. The replay equity seam served ``multiplier = 1.0``
   (``risk_policy._notional_ceiling_basis``); live serves the Alpaca account's own
   ``multiplier`` field (4.0). The newest live admission receipt that names
   ``source = broker_multiplier`` is the pin.

This module is deliberately ``app``-free (psycopg2 + stdlib), because
``scripts/ross_replay_bench.py`` must import without a DATABASE_URL and the driver
(``replay_v3_fsm_window.py``) cannot be imported at all outside a ``_test`` env. Both import
THIS file, so the derivation lives in one place.

Every SELECT here is bounded: the publication read is a backward PK index scan capped by
``PUBLICATION_LAG_SAMPLE_ROWS``; the multiplier read is ``ORDER BY id DESC LIMIT 1`` on the
session PK (measured 0.66 ms on live ``chili``, 2026-09-11). Both run read-only under a
statement timeout. Nothing here writes to the live database.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

LIVE_PINS_SCHEMA = "chili.replay_live_pins.v1"
#: The driver env key that carries the pinned JSON (a ross_replay_bench contract key).
LIVE_PINS_ENV = "REPLAY_LIVE_PINS"
#: ``timestamp_basis`` written on a mirrored row whose clocks were DERIVED here.
PUBLICATION_TIMESTAMP_BASIS = "replay_v3_derived_publication"
#: The live source whose clocks ARE publication clocks (the bridge stamps both).
LIVE_PUBLICATION_SOURCE = "iqfeed_l1"

#: A READ bound, not a decision: the largest backward-PK tail the repo's own validated
#: frontier-probe setting admits (``chili_momentum_halt_frontier_tail_rows``, ``le=20000``).
#: At the RTH turnover measured there (334 rows/s) it is ~60 s of tape across every
#: subscribed name; the fail-closed rule below, not this bound, decides adequacy.
PUBLICATION_LAG_SAMPLE_ROWS = 20000

#: A row is REAL-TIME iff ``available_at - observed_at`` is below this. Spelled here because
#: this module is app-free; it MUST equal the default of
#: ``app.config.Settings.chili_momentum_halt_frontier_max_arrival_delay_s`` (the geometric
#: middle of the empty band between the worst real-time bridge stall, 109.9 s, and the
#: 15-min delayed-entitlement floor, 899.9 s -- sqrt(109.9 x 899.9) = 314 -> 300).
#: tests/test_replay_v3_publication_clock_mirror.py pins the equality. Delayed-entitlement
#: rows ([38]) are a different product, not a slow real-time row, and never enter the p50.
REALTIME_ARRIVAL_FENCE_S_DEFAULT = 300.0
REALTIME_ARRIVAL_FENCE_SOURCE = "settings.chili_momentum_halt_frontier_max_arrival_delay_s"

#: The percentiles every publication receipt reports.
REPORTED_PERCENTILES: tuple[float, ...] = (0.10, 0.50, 0.90, 0.99)


def n_min_for_percentile(p: float) -> int:
    """ceil(1 / (1 - p)): below this a percentile is not a measurement.

    Same rule as ``held_bbo.n_min_for_percentile`` (re-spelled: app-free module; a test
    pins the two equal)."""
    return int(math.ceil(1.0 / (1.0 - float(p))))


#: FAIL-CLOSED floor: the smallest sample in which EVERY percentile this receipt reports
#: (up to p99) is a measurement -> 100 rows.
PUBLICATION_LAG_N_MIN = n_min_for_percentile(max(REPORTED_PERCENTILES))

#: A live admission receipt whose multiplier came from the BROKER (not a fallback).
BROKER_TRUTH_MULTIPLIER_SOURCES: tuple[str, ...] = (
    "broker_multiplier",
    "buying_power_over_equity",
    "broker_reported_buying_power",
)

ALPACA_FAMILIES: tuple[str, ...] = ("alpaca_spot", "alpaca_short")


class LivePinUnavailable(RuntimeError):
    """A pin the replay needs could not be derived. ``code`` names which."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ─── SQL (psycopg2 pyformat) ─────────────────────────────────────────────────────────────

# The eligibility predicate is the READERS' OWN (tape_selection.signed_tape_query:47-49):
# both clocks known and finite, available_at >= received_at. ``observed_at`` is naive UTC,
# the clocks are timestamptz, hence ``AT TIME ZONE 'UTC'`` before subtracting (correct under
# any session TimeZone).
PUBLICATION_LAG_SQL = (
    "SELECT symbol, observed_at, "
    "EXTRACT(EPOCH FROM (received_at - (observed_at AT TIME ZONE 'UTC'))) AS recv_lag_s, "
    "EXTRACT(EPOCH FROM (available_at - (observed_at AT TIME ZONE 'UTC'))) AS avail_lag_s "
    "FROM (SELECT symbol, source, observed_at, received_at, available_at "
    "      FROM iqfeed_trade_ticks ORDER BY id DESC LIMIT %(sample_rows)s) t "
    "WHERE source = %(source)s "
    "AND received_at IS NOT NULL AND available_at IS NOT NULL "
    "AND available_at >= received_at "
    "AND isfinite(observed_at) AND isfinite(received_at) AND isfinite(available_at)"
)

# The newest LIVE admission receipt for the family whose multiplier is broker truth.
BROKER_MULTIPLIER_SQL = (
    "SELECT id, symbol, updated_at, "
    "risk_snapshot_json -> 'momentum_policy_caps_derivation' -> 'notional_ceiling' "
    "FROM trading_automation_sessions "
    "WHERE mode = 'live' AND execution_family = %(ef)s "
    "AND risk_snapshot_json -> 'momentum_policy_caps_derivation' -> 'notional_ceiling' "
    "    ->> 'source' = ANY(%(sources)s) "
    "ORDER BY id DESC LIMIT 1"
)


# ─── PURE HELPERS ────────────────────────────────────────────────────────────────────────

def percentile_cont(sorted_values: Sequence[float], p: float) -> float:
    """PostgreSQL ``percentile_cont`` semantics (linear between the bracketing ranks)."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile of an empty sample")
    if n == 1:
        return float(sorted_values[0])
    pos = float(p) * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo]) + frac * (float(sorted_values[hi]) - float(sorted_values[lo]))


def median_ci95(sorted_values: Sequence[float]) -> tuple[float, float]:
    """Distribution-free 95% CI of the median from order statistics (binomial ranks
    n/2 -+ z*sqrt(n)/2, z = the 0.975 normal quantile). Reported, never a gate."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("CI of an empty sample")
    z = statistics.NormalDist().inv_cdf(0.975)
    # 1-based ranks r = floor((n - z*sqrt(n))/2), s = ceil(1 + (n + z*sqrt(n))/2); 0-based below
    lo = max(0, int(math.floor((n - z * math.sqrt(n)) / 2.0)) - 1)
    hi = min(n - 1, int(math.ceil((n + z * math.sqrt(n)) / 2.0)))
    return float(sorted_values[lo]), float(sorted_values[hi])


def _pct_block(values: Sequence[float]) -> dict[str, float]:
    s = sorted(float(v) for v in values)
    out = {f"p{int(round(p * 100)):02d}": round(percentile_cont(s, p), 6) for p in REPORTED_PERCENTILES}
    lo, hi = median_ci95(s)
    out["p50_ci95"] = [round(lo, 6), round(hi, 6)]
    return out


def summarize_publication_lags(
    rows: Iterable[Sequence[Any]],
    *,
    fence_s: float,
    n_min: int = PUBLICATION_LAG_N_MIN,
) -> dict[str, Any]:
    """rows = (symbol, observed_at, recv_lag_s, avail_lag_s) -> the publication receipt.

    Delayed-entitlement rows (``avail_lag >= fence_s``) are EXCLUDED and counted. Raises
    ``LivePinUnavailable('publication_clock_unavailable', ...)`` below ``n_min``."""
    kept: list[tuple[str, Any, float, float]] = []
    delayed = 0
    read = 0
    for r in rows:
        read += 1
        sym, obs, rl, al = r[0], r[1], r[2], r[3]
        try:
            rl_f, al_f = float(rl), float(al)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(rl_f) and math.isfinite(al_f)):
            continue
        if al_f >= float(fence_s):
            delayed += 1
            continue
        kept.append((str(sym), obs, rl_f, al_f))
    n = len(kept)
    if n < int(n_min):
        raise LivePinUnavailable(
            "publication_clock_unavailable",
            f"{n} real-time rows (< n_min {int(n_min)}) among {read} read "
            f"({delayed} delayed >= {float(fence_s)} s excluded) -- no live publication-lag "
            "distribution to stamp the mirror with; refusing to replay blind",
        )
    recv = [k[2] for k in kept]
    avail = [k[3] for k in kept]
    recv_p = _pct_block(recv)
    avail_p = _pct_block(avail)
    by_sym: dict[str, list[float]] = {}
    for sym, _obs, _rl, al in kept:
        by_sym.setdefault(sym, []).append(al)
    per_symbol = {
        s: {"n": len(v), "available_lag_p50_s": round(percentile_cont(sorted(v), 0.5), 6)}
        for s, v in sorted(by_sym.items())
    }
    obs_times = [k[1] for k in kept if k[1] is not None]
    recv_lag = float(recv_p["p50"])
    avail_lag = float(avail_p["p50"])
    if avail_lag < recv_lag:  # impossible when every row has available >= received; assert it
        raise LivePinUnavailable(
            "publication_clock_unordered",
            f"available p50 {avail_lag} < received p50 {recv_lag}",
        )
    return {
        "rows_read": read,
        "n": n,
        "n_symbols": len(by_sym),
        "n_min": int(n_min),
        "n_min_rule": "ceil(1/(1-p)) at the highest reported percentile p=0.99 "
                      "(held_bbo.n_min_for_percentile)",
        "excluded_delayed": delayed,
        "fence_s": float(fence_s),
        "observed_span_utc": (
            [min(obs_times).isoformat(), max(obs_times).isoformat()] if obs_times else None
        ),
        "received_lag_s": recv_p,
        "available_lag_s": avail_p,
        "per_symbol": per_symbol,
        # THE BINDING VALUES
        "recv_lag_s": recv_lag,
        "avail_lag_s": avail_lag,
        "binding": ("received_at = observed_at + received_lag_s.p50; "
                    "available_at = observed_at + available_lag_s.p50; "
                    "provider_event_at = observed_at"),
    }


def _aware_utc(t: Any) -> Optional[datetime]:
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if not isinstance(t, datetime):
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def publication_clocks(
    observed_at: Any,
    publication: Mapping[str, Any],
    *,
    source_received_at: Any = None,
    source_available_at: Any = None,
    source_provider_event_at: Any = None,
    source_timestamp_basis: Any = None,
) -> tuple[datetime, datetime, datetime, str, str]:
    """(provider_event_at, received_at, available_at, timestamp_basis, kind) for ONE print.

    A source row that ALREADY carries real publication clocks (a live ``iqfeed_l1`` tape:
    both known and ``available >= received``, the readers' own predicate) keeps them --
    including a 15-min delayed-entitlement row, which live also saw 900 s late.
    Everything else (every hydrated row: ``available_at`` NULL, ``received_at`` = the
    hydration wall time) is stamped ``observed_at + the pinned p50 lags``. Aware UTC."""
    obs = _aware_utc(observed_at)
    if obs is None:
        raise ValueError(f"observed_at is not a datetime: {observed_at!r}")
    s_recv = _aware_utc(source_received_at)
    s_avail = _aware_utc(source_available_at)
    if s_recv is not None and s_avail is not None and s_avail >= s_recv:
        return (
            _aware_utc(source_provider_event_at) or obs,
            s_recv,
            s_avail,
            str(source_timestamp_basis or "source_recorded"),
            "source",
        )
    recv = obs + timedelta(seconds=float(publication["recv_lag_s"]))
    avail = obs + timedelta(seconds=float(publication["avail_lag_s"]))
    return obs, recv, avail, PUBLICATION_TIMESTAMP_BASIS, "derived"


def trade_row_with_clocks(
    symbol: str,
    source_row: Sequence[Any],
    publication: Mapping[str, Any],
    *,
    mirror_source: str = "replay_v3",
    clock_counts: Optional[dict[str, int]] = None,
) -> tuple:
    """One mirrored ``iqfeed_trade_ticks`` row in ``TRADE_MIRROR_INSERT_COLUMNS`` order.

    ``source_row`` = (observed_at, price, size, bid, ask, id[, received_at, available_at,
    provider_event_at, timestamp_basis]) -- the driver's ``_TRADE_MIRROR_SQL`` shape; the
    in-memory fallback mirror passes only the first six."""
    obs = source_row[0]
    extra = list(source_row[6:10]) + [None] * (4 - len(source_row[6:10]))
    pev, recv, avail, basis, kind = publication_clocks(
        obs, publication,
        source_received_at=extra[0], source_available_at=extra[1],
        source_provider_event_at=extra[2], source_timestamp_basis=extra[3],
    )
    if clock_counts is not None:
        clock_counts[kind] = int(clock_counts.get(kind, 0)) + 1
    naive_obs = obs.replace(tzinfo=None) if getattr(obs, "tzinfo", None) is not None else obs
    return (
        symbol, naive_obs, float(source_row[1]), float(source_row[2] or 0.0),
        source_row[3], source_row[4], mirror_source, pev, recv, avail, basis,
    )


#: The INSERT column list the tick mirror writes -- the readers' two publication clocks
#: and their provenance travel WITH the print, never as a later UPDATE.
TRADE_MIRROR_INSERT_COLUMNS: tuple[str, ...] = (
    "symbol", "observed_at", "price", "size", "bid", "ask", "source",
    "provider_event_at", "received_at", "available_at", "timestamp_basis",
)


# ─── BROKER MULTIPLIER ───────────────────────────────────────────────────────────────────

def summarize_broker_multiplier(row: Optional[Sequence[Any]], *, execution_family: str) -> dict[str, Any]:
    """row = (session_id, symbol, updated_at, notional_ceiling_derivation) or None."""
    if row is None:
        return {
            "multiplier": None,
            "source": "unavailable",
            "execution_family": execution_family,
            "reason": "no live admission receipt names a broker-truth multiplier for this family",
        }
    sid, sym, upd, d = row[0], row[1], row[2], row[3]
    if isinstance(d, str):
        d = json.loads(d)
    d = dict(d or {})
    try:
        m = float(d.get("multiplier"))
    except (TypeError, ValueError):
        m = float("nan")
    if not (math.isfinite(m) and m >= 1.0):
        return {
            "multiplier": None,
            "source": "unavailable",
            "execution_family": execution_family,
            "reason": f"receipt session {sid} carries an unusable multiplier {d.get('multiplier')!r}",
        }
    return {
        "multiplier": m,
        "source": str(d.get("source")),
        "execution_family": execution_family,
        "session_id": int(sid),
        "symbol": str(sym),
        "session_updated_at_utc": (upd.isoformat() if hasattr(upd, "isoformat") else str(upd)),
        "receipt_equity_usd": d.get("equity_usd"),
        "receipt_ceiling_usd": d.get("ceiling_usd"),
        "receipt_binding": d.get("binding"),
        "receipt_crossover_stop_pct": d.get("crossover_stop_pct"),
        "binding": "replay equity seam multiplier = the newest live admission receipt's "
                   "momentum_policy_caps_derivation.notional_ceiling.multiplier",
    }


# ─── DERIVE / LOAD ───────────────────────────────────────────────────────────────────────

def _db_name(dsn: str) -> str:
    try:
        from urllib.parse import urlsplit

        return (urlsplit(dsn).path or "").rsplit("/", 1)[-1] or "(unnamed)"
    except Exception:
        return "(unparsed)"


def derive_live_pins(
    dsn: str,
    *,
    execution_family: str,
    fence_s: float = REALTIME_ARRIVAL_FENCE_S_DEFAULT,
    fence_source: str = REALTIME_ARRIVAL_FENCE_SOURCE,
    sample_rows: int = PUBLICATION_LAG_SAMPLE_ROWS,
    n_min: int = PUBLICATION_LAG_N_MIN,
    statement_timeout_ms: int = 20000,
    connect: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Derive BOTH pins from the LIVE database, read-only and bounded.

    Fails closed (``LivePinUnavailable``) when the publication clock cannot be measured.
    The multiplier is reported ``unavailable`` rather than raised; callers that measure
    the canon (the bench, an Alpaca family) refuse on that themselves."""
    if connect is None:
        import psycopg2

        connect = psycopg2.connect
    conn = connect(dsn)
    try:
        try:
            conn.set_session(readonly=True)
        except Exception:
            pass
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        cur.execute(PUBLICATION_LAG_SQL, {"sample_rows": int(sample_rows),
                                          "source": LIVE_PUBLICATION_SOURCE})
        lag_rows = cur.fetchall()
        pub = summarize_publication_lags(lag_rows, fence_s=fence_s, n_min=n_min)
        pub.update({
            "source": LIVE_PUBLICATION_SOURCE,
            "sample_rows": int(sample_rows),
            "fence_source": fence_source,
            "query": PUBLICATION_LAG_SQL,
        })
        cur.execute(BROKER_MULTIPLIER_SQL, {"ef": str(execution_family),
                                            "sources": list(BROKER_TRUTH_MULTIPLIER_SOURCES)})
        mult = summarize_broker_multiplier(cur.fetchone(), execution_family=str(execution_family))
        mult["query"] = BROKER_MULTIPLIER_SQL
        cur.close()
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return {
        "schema": LIVE_PINS_SCHEMA,
        "derived_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_db": _db_name(dsn),
        "publication_clock": pub,
        "broker_multiplier": mult,
    }


def pins_sha256(pins: Mapping[str, Any]) -> str:
    blob = json.dumps(pins, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def validate_live_pins(pins: Any) -> dict[str, Any]:
    """Refuse a pin document that does not carry both binding lags (and a schema we know)."""
    if not isinstance(pins, dict):
        raise LivePinUnavailable("live_pins_invalid", f"expected an object, got {type(pins).__name__}")
    if pins.get("schema") != LIVE_PINS_SCHEMA:
        raise LivePinUnavailable("live_pins_invalid",
                                 f"schema {pins.get('schema')!r} != {LIVE_PINS_SCHEMA!r}")
    pub = pins.get("publication_clock")
    if not isinstance(pub, dict):
        raise LivePinUnavailable("publication_clock_unavailable", "pins carry no publication_clock")
    for key in ("recv_lag_s", "avail_lag_s"):
        try:
            v = float(pub.get(key))
        except (TypeError, ValueError):
            raise LivePinUnavailable("publication_clock_unavailable", f"{key} missing") from None
        if not math.isfinite(v):
            raise LivePinUnavailable("publication_clock_unavailable", f"{key} not finite")
    if float(pub["avail_lag_s"]) < float(pub["recv_lag_s"]):
        raise LivePinUnavailable("publication_clock_unordered", "avail_lag_s < recv_lag_s")
    if not isinstance(pins.get("broker_multiplier"), dict):
        raise LivePinUnavailable("live_pins_invalid", "pins carry no broker_multiplier block")
    return pins


def load_live_pins(text_or_path: str) -> dict[str, Any]:
    """A pin document from a JSON string or a path to one."""
    raw = str(text_or_path or "").strip()
    if not raw:
        raise LivePinUnavailable("live_pins_invalid", "empty")
    if not raw.startswith("{"):
        with open(raw, encoding="utf-8") as fh:
            raw = fh.read()
    return validate_live_pins(json.loads(raw))


def dumps_live_pins(pins: Mapping[str, Any]) -> str:
    """The compact, key-sorted serialisation that travels in ``REPLAY_LIVE_PINS``."""
    return json.dumps(pins, sort_keys=True, separators=(",", ":"), default=str)


# ─── THE EQUITY SEAM OBJECT ──────────────────────────────────────────────────────────────

@dataclass
class ReplayEquityProvider:
    """The replay equity seam's provider (``risk_policy.replay_account_equity``) PLUS the
    pinned broker multiplier. ``risk_policy._notional_ceiling_basis`` reads
    ``replay_multiplier`` / ``replay_multiplier_source`` off the installed provider; a bare
    callable (every other replay) has neither and keeps the seam's named 1.0."""

    equity_usd: float
    replay_multiplier: Optional[float] = None
    replay_multiplier_source: Optional[str] = None

    def __call__(self, *args: Any, **kwargs: Any) -> float:
        return self.equity_usd


def equity_provider_from_pins(equity_usd: float, pins: Mapping[str, Any]) -> ReplayEquityProvider:
    mult = (pins or {}).get("broker_multiplier") or {}
    m = mult.get("multiplier")
    if m is None:
        return ReplayEquityProvider(float(equity_usd))
    return ReplayEquityProvider(float(equity_usd), float(m), str(mult.get("source") or "broker_multiplier"))


# ─── POST-MIRROR INVARIANT ───────────────────────────────────────────────────────────────

# Symbol-scoped, run once per arm on the SINK. Kept here (not in the driver) because the
# driver's tie-stability fence (replay_harness_invariants.assert_tie_stable_sql) requires
# every tape SELECT in the driver to be a replay read ending ``ORDER BY observed_at, id``;
# these are audits of the mirror, not replay reads.
NULL_CLOCK_COUNT_SQL = (
    "SELECT count(*) FROM iqfeed_trade_ticks "
    "WHERE source = :src AND symbol = :s AND (received_at IS NULL OR available_at IS NULL)"
)
FIRST_PUBLICATION_SQL = (
    "SELECT observed_at, available_at FROM iqfeed_trade_ticks "
    "WHERE source = :src AND symbol = :s AND available_at IS NOT NULL "
    "ORDER BY available_at ASC, id ASC LIMIT 1"
)


def assert_publication_clock_visible(
    db: Any,
    symbol: str,
    *,
    readers: Mapping[str, Callable[..., Any]],
    mirror_source: str = "replay_v3",
    lookback_s: float = 1.0,
) -> dict[str, Any]:
    """FAIL CLOSED after the mirror: prove the lane's OWN print readers SEE the mirrored tape.

    ``readers`` = {name: reader(symbol, *, db, after, as_of) -> rows}: the entry window read
    (``tape_selection.signed_tape_query``) and, where the tree has it, the verdict / deadman
    batch read (``entry_gates.leg_prints_between``). For EACH:
      1. zero mirrored rows carry a NULL publication clock;
      2. at the first publication instant the reader returns >= 1 row;
      3. strictly before it the same reader returns NOTHING (the stamp binds -- no look-ahead).
    Raises ``LivePinUnavailable('publication_clock_blind', ...)`` on 1 or 2 and
    ``('publication_clock_lookahead', ...)`` on 3."""
    from sqlalchemy import text as _t

    if not readers:
        raise LivePinUnavailable("publication_clock_blind", "no reader to probe the mirror with")
    n_null = int(db.execute(_t(NULL_CLOCK_COUNT_SQL), {"src": mirror_source, "s": symbol}).scalar() or 0)
    if n_null:
        raise LivePinUnavailable("publication_clock_blind",
                                 f"{n_null} mirrored {symbol} rows carry a NULL publication clock")
    first = db.execute(_t(FIRST_PUBLICATION_SQL), {"src": mirror_source, "s": symbol}).fetchone()
    if first is None:
        return {"status": "empty_mirror", "null_clock_rows": 0}
    obs0 = _aware_utc(first[0])
    avail0 = _aware_utc(first[1])
    as_of = max(obs0, avail0)
    after = obs0 - timedelta(seconds=float(lookback_s))
    naive = lambda t: t.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    probes: dict[str, dict[str, Any]] = {}
    for name, reader in readers.items():
        rows = reader(symbol, db=db, after=naive(after), as_of=naive(as_of))
        if not rows:
            raise LivePinUnavailable(
                "publication_clock_blind",
                f"{name}({symbol}, as_of={as_of.isoformat()}) returned "
                f"{'None' if rows is None else 0} rows at the first publication instant",
            )
        before_n = None
        if avail0 > obs0:
            before = avail0 - timedelta(microseconds=1)
            pre = reader(symbol, db=db, after=naive(after), as_of=naive(before))
            before_n = 0 if pre is None else len(pre)
            if before_n:
                raise LivePinUnavailable(
                    "publication_clock_lookahead",
                    f"{name}: {before_n} {symbol} rows visible before the first publication "
                    f"instant {avail0.isoformat()}",
                )
        probes[name] = {"probe_rows": len(rows), "pre_publication_rows": before_n}
    return {
        "status": "visible",
        "null_clock_rows": 0,
        "first_observed_at_utc": obs0.isoformat(),
        "first_available_at_utc": avail0.isoformat(),
        "probe_as_of_utc": as_of.isoformat(),
        "probe_rows": min(p["probe_rows"] for p in probes.values()),
        "readers": probes,
    }


# ─── CLI: derive once, pin to a file every bench of one A/B reuses ───────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="replay_live_pins",
        description="Derive the replay's live pins (publication clock + broker multiplier) "
                    "ONCE, read-only and bounded, and write them for --live-pins.",
    )
    ap.add_argument("--dsn", required=True, help="the LIVE database (read-only)")
    ap.add_argument("--execution-family", required=True)
    ap.add_argument("--out", required=True, help="where to write the pin JSON")
    ap.add_argument("--fence-s", type=float, default=None,
                    help=f"real-time arrival fence; default {REALTIME_ARRIVAL_FENCE_S_DEFAULT} "
                         f"({REALTIME_ARRIVAL_FENCE_SOURCE}) or the lane env value")
    args = ap.parse_args(argv)
    fence = REALTIME_ARRIVAL_FENCE_S_DEFAULT if args.fence_s is None else float(args.fence_s)
    try:
        pins = derive_live_pins(args.dsn, execution_family=args.execution_family, fence_s=fence)
    except LivePinUnavailable as exc:
        print(f"[replay_live_pins] ABORT {exc.code}: {exc.detail}")
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", newline="\n", encoding="utf-8") as fh:
        fh.write(json.dumps(pins, indent=2, default=str) + "\n")
    pub, mult = pins["publication_clock"], pins["broker_multiplier"]
    print(f"[replay_live_pins] {args.out} sha256={pins_sha256(pins)}")
    print(f"  publication: recv_lag p50 {pub['recv_lag_s']} s, avail_lag p50 {pub['avail_lag_s']} s "
          f"(n={pub['n']}, {pub['n_symbols']} symbols, {pub['excluded_delayed']} delayed excluded, "
          f"span {pub['observed_span_utc']})")
    print(f"  broker multiplier: {mult.get('multiplier')} ({mult.get('source')}; session "
          f"{mult.get('session_id')} {mult.get('symbol')} {mult.get('session_updated_at_utc')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
