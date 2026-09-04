"""Derive the two ignition admission governors from measured nomination traffic.

READ-ONLY. This script never writes to the database, never mutates settings, and
is deliberately NOT wired into startup or any scheduler — an operator runs it,
reads the manifest, and decides.

WHAT IT DERIVES
---------------
``chili_momentum_ignition_admits_per_minute``
    p99 of DISTINCT symbols nominated per minute over the window — the real
    arrival rate. Bounded above by
    ``chili_momentum_risk_max_concurrent_live_sessions``: admitting faster than
    the lane can hold sessions only spends admission latency on names that will
    be refused downstream anyway.

``chili_momentum_ignition_dedup_ttl_seconds``
    p90 fire -> admission-decision latency (``recorded_at - fired_at`` on rows
    the admission path wrote). A repeat attempt before the first one has even
    resolved is pure duplicate work. CEILINGED at
    ``chili_momentum_auto_arm_max_watch_seconds``: past the watch deadline the
    earlier attempt's session is already reaped, so the same symbol firing again
    is a genuinely new idea rather than a repeat.

WHY THE TABLE EXISTS AT ALL
---------------------------
``ross_event_admission.admit_ross_event`` is the only code path that can create a
viability row for a symbol the universe poll has never seen, and it produced
**0 ross_event_admitted events across 3,379 live sessions in 14 days** with no
durable trace of the refusals. ``momentum_ignition_nominations`` (migration 376)
is that trace, and it records governor-SUPPRESSED nominations too — otherwise the
governors would censor exactly the distribution needed to derive them.

TWO CENSORING EFFECTS ARE REPORTED, NEVER SILENTLY ABSORBED
-----------------------------------------------------------
1. The producer (``scripts/iqfeed_ignition_detector.py``) applies its OWN
   ``max_fires_per_minute`` and ``dedup_ttl_s``. An observed p99 that sits at the
   producer cap is a CEILING, not a measurement.
2. Latency is only observable for nominations that reached the admission
   transaction; a window with too few of those yields no TTL recommendation.

USAGE
-----
    python scripts/derive_ignition_governors.py --days 14 \
        --out ignition_governor_manifest.json

Exit code 0 on success (including "insufficient data" — that is a legitimate
finding, not a failure); 2 on a usage/connection error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "chili.ignition-governor-manifest.v1"

# The producer's own hard caps (scripts/iqfeed_ignition_detector.py
# IgnitionConfig). Read here only to LABEL an observation as censored — never to
# choose a value.
PRODUCER_MAX_FIRES_PER_MINUTE = 6
PRODUCER_DEDUP_TTL_S = 300.0

# Day-1 values of the settings being derived, used when app.config cannot be
# imported (this script must run from a bare checkout too). Each MUST equal the
# corresponding app/config.py default — test_derive_ignition_governors.py pins
# every one of these against Settings.model_fields[...].default, because a stale
# literal here silently produces a wrong recommendation rather than an error.
FALLBACK_ADMITS_PER_MINUTE = 6  # chili_momentum_ignition_admits_per_minute
FALLBACK_DEDUP_TTL_S = 300.0  # chili_momentum_ignition_dedup_ttl_seconds
FALLBACK_MAX_CONCURRENT_LIVE = 5  # chili_momentum_risk_max_concurrent_live_sessions
FALLBACK_MAX_WATCH_SECONDS = 300  # chili_momentum_auto_arm_max_watch_seconds

# The setting bounds declared in app/config.py. The derivation must land inside
# them or the manifest is not actionable.
ADMITS_PER_MINUTE_BOUNDS = (1, 120)
DEDUP_TTL_BOUNDS = (1.0, 3600.0)

# Below these sample sizes the percentile is noise, so the script recommends
# nothing and says so. One documented floor per statistic.
MIN_NOMINATIONS = 30
MIN_ADMISSION_LATENCY_SAMPLES = 20

NOMINATION_MINUTES_SQL = """
SELECT date_trunc('minute', fired_at) AS minute,
       count(DISTINCT symbol) AS distinct_symbols,
       count(*) AS nominations
FROM momentum_ignition_nominations
WHERE fired_at >= %(start)s AND fired_at < %(end)s
GROUP BY 1
ORDER BY 1
"""

ADMISSION_LATENCY_SQL = """
SELECT extract(epoch FROM (recorded_at - fired_at)) AS latency_s
FROM momentum_ignition_nominations
WHERE fired_at >= %(start)s AND fired_at < %(end)s
  AND recorded_at IS NOT NULL
  AND outcome NOT LIKE 'governor_%%'
  AND outcome <> 'already_tracked'
ORDER BY 1
"""

OUTCOME_CENSUS_SQL = """
SELECT outcome, count(*) AS n
FROM momentum_ignition_nominations
WHERE fired_at >= %(start)s AND fired_at < %(end)s
GROUP BY 1
ORDER BY 2 DESC
"""

UNIVERSE_REASON_CENSUS_SQL = """
SELECT coalesce(ross_universe_reason, '(none)') AS reason, count(*) AS n
FROM momentum_ignition_nominations
WHERE fired_at >= %(start)s AND fired_at < %(end)s
GROUP BY 1
ORDER BY 2 DESC
"""


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolated q-percentile (0..1) of an ASCENDING list."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def _current_settings() -> dict:
    """Current values of the settings involved. Falls back to day-1 defaults."""
    values = {
        "chili_momentum_ignition_admits_per_minute": FALLBACK_ADMITS_PER_MINUTE,
        "chili_momentum_ignition_dedup_ttl_seconds": FALLBACK_DEDUP_TTL_S,
        "chili_momentum_risk_max_concurrent_live_sessions": FALLBACK_MAX_CONCURRENT_LIVE,
        "chili_momentum_auto_arm_max_watch_seconds": FALLBACK_MAX_WATCH_SECONDS,
        "source": "fallback_day1_defaults",
    }
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from app.config import settings  # noqa: PLC0415

        for key in list(values):
            if key == "source":
                continue
            current = getattr(settings, key, None)
            if current is not None:
                values[key] = current
        values["source"] = "app.config.settings"
    except Exception:
        pass
    return values


def _fetch(database_url: str, statement_timeout_ms: int, start, end) -> dict:
    """Run every read in ONE read-only transaction with a bounded timeout."""
    import psycopg2  # noqa: PLC0415

    params = {"start": start, "end": end}
    conn = psycopg2.connect(database_url)
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s", (statement_timeout_ms,))
            cur.execute(NOMINATION_MINUTES_SQL, params)
            minutes = [(row[0], int(row[1]), int(row[2])) for row in cur.fetchall()]
            cur.execute(ADMISSION_LATENCY_SQL, params)
            latencies = [float(row[0]) for row in cur.fetchall() if row[0] is not None]
            cur.execute(OUTCOME_CENSUS_SQL, params)
            outcomes = {str(row[0]): int(row[1]) for row in cur.fetchall()}
            cur.execute(UNIVERSE_REASON_CENSUS_SQL, params)
            reasons = {str(row[0]): int(row[1]) for row in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()
    return {
        "minutes": minutes,
        "latencies": latencies,
        "outcomes": outcomes,
        "universe_reasons": reasons,
    }


def derive_admits_per_minute(
    distinct_per_minute: list[int],
    *,
    max_concurrent_live_sessions: int,
    current_value: int,
) -> dict:
    """PURE: the admits/minute recommendation and its full derivation."""
    total_nominations = len(distinct_per_minute)
    out: dict = {
        "setting": "chili_momentum_ignition_admits_per_minute",
        "current_value": int(current_value),
        "sample_minutes": total_nominations,
        "bound_max_concurrent_live_sessions": int(max_concurrent_live_sessions),
        "producer_cap": PRODUCER_MAX_FIRES_PER_MINUTE,
    }
    if total_nominations < MIN_NOMINATIONS:
        out.update(
            recommended_value=None,
            reason="insufficient_data",
            derivation=(
                f"only {total_nominations} nomination-minutes in the window; "
                f"{MIN_NOMINATIONS} required before a p99 is anything but noise. "
                "Keep the current value."
            ),
        )
        return out
    ordered = sorted(float(v) for v in distinct_per_minute)
    p99 = percentile(ordered, 0.99)
    p50 = percentile(ordered, 0.50)
    observed = int(-(-float(p99 or 0.0) // 1))  # ceil
    bounded = max(
        ADMITS_PER_MINUTE_BOUNDS[0],
        min(observed, int(max_concurrent_live_sessions), ADMITS_PER_MINUTE_BOUNDS[1]),
    )
    censored = observed >= PRODUCER_MAX_FIRES_PER_MINUTE
    out.update(
        p50_distinct_symbols_per_minute=p50,
        p99_distinct_symbols_per_minute=p99,
        observed_ceil=observed,
        recommended_value=bounded,
        censored_by_producer_cap=censored,
        reason="derived",
        derivation=(
            f"p99 of distinct symbols nominated per minute = {p99:.2f} "
            f"(median {p50:.2f}) over {total_nominations} minutes; ceil = "
            f"{observed}; bounded by max_concurrent_live_sessions="
            f"{max_concurrent_live_sessions} and the setting's own "
            f"{ADMITS_PER_MINUTE_BOUNDS} range -> {bounded}."
            + (
                " WARNING: the observation sits at the producer's own "
                f"max_fires_per_minute={PRODUCER_MAX_FIRES_PER_MINUTE}, so it is "
                "a CEILING, not a measurement — raise the producer cap first if "
                "you want the true arrival rate."
                if censored
                else ""
            )
        ),
    )
    return out


def derive_dedup_ttl_seconds(
    admission_latencies_s: list[float],
    *,
    max_watch_seconds: float,
    current_value: float,
) -> dict:
    """PURE: the dedup-TTL recommendation and its full derivation."""
    samples = len(admission_latencies_s)
    out: dict = {
        "setting": "chili_momentum_ignition_dedup_ttl_seconds",
        "current_value": float(current_value),
        "sample_size": samples,
        "ceiling_auto_arm_max_watch_seconds": float(max_watch_seconds),
        "producer_dedup_ttl_s": PRODUCER_DEDUP_TTL_S,
    }
    if samples < MIN_ADMISSION_LATENCY_SAMPLES:
        out.update(
            recommended_value=None,
            reason="insufficient_data",
            derivation=(
                f"only {samples} nominations reached the admission transaction; "
                f"{MIN_ADMISSION_LATENCY_SAMPLES} required before a p90 latency "
                "means anything. Keep the current value."
            ),
        )
        return out
    ordered = sorted(float(v) for v in admission_latencies_s)
    p90 = percentile(ordered, 0.90) or 0.0
    p50 = percentile(ordered, 0.50) or 0.0
    bounded = max(
        DEDUP_TTL_BOUNDS[0],
        min(round(p90, 1), float(max_watch_seconds), DEDUP_TTL_BOUNDS[1]),
    )
    out.update(
        p50_admission_latency_s=p50,
        p90_admission_latency_s=p90,
        recommended_value=bounded,
        reason="derived",
        derivation=(
            f"p90 fire->decision latency = {p90:.2f}s (median {p50:.2f}s) over "
            f"{samples} admission attempts; ceilinged at "
            f"auto_arm_max_watch_seconds={max_watch_seconds:.0f}s and the "
            f"setting's own {DEDUP_TTL_BOUNDS} range -> {bounded}."
        ),
    )
    return out


def build_manifest(
    fetched: dict,
    *,
    window_start: datetime,
    window_end: datetime,
    current: dict,
) -> dict:
    """PURE: assemble the manifest from fetched rows + current settings."""
    minutes = fetched.get("minutes") or []
    distinct_per_minute = [int(row[1]) for row in minutes]
    nominations = sum(int(row[2]) for row in minutes)
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "applied": False,
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "days": round(
                (window_end - window_start).total_seconds() / 86400.0, 4
            ),
        },
        "sample": {
            "nominations": nominations,
            "nomination_minutes": len(minutes),
            "admission_attempts": len(fetched.get("latencies") or []),
            "outcomes": fetched.get("outcomes") or {},
            "ross_universe_reasons": fetched.get("universe_reasons") or {},
        },
        "current_settings": current,
        "governors": [
            derive_admits_per_minute(
                distinct_per_minute,
                max_concurrent_live_sessions=int(
                    current["chili_momentum_risk_max_concurrent_live_sessions"]
                ),
                current_value=int(
                    current["chili_momentum_ignition_admits_per_minute"]
                ),
            ),
            derive_dedup_ttl_seconds(
                list(fetched.get("latencies") or []),
                max_watch_seconds=float(
                    current["chili_momentum_auto_arm_max_watch_seconds"]
                ),
                current_value=float(
                    current["chili_momentum_ignition_dedup_ttl_seconds"]
                ),
            ),
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=14.0, help="trailing window (days)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="read-only connection string (defaults to $DATABASE_URL)",
    )
    parser.add_argument(
        "--out",
        default="ignition_governor_manifest.json",
        help="manifest path to write",
    )
    parser.add_argument("--statement-timeout-ms", type=int, default=20_000)
    args = parser.parse_args(argv)

    if not str(args.database_url or "").strip():
        print(
            "ERROR: no --database-url and no DATABASE_URL in the environment.",
            file=sys.stderr,
        )
        return 2
    if args.days <= 0:
        print("ERROR: --days must be positive.", file=sys.stderr)
        return 2

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=float(args.days))
    try:
        fetched = _fetch(
            str(args.database_url),
            int(args.statement_timeout_ms),
            window_start,
            window_end,
        )
    except Exception as exc:  # connection / missing table / timeout
        print(f"ERROR: read failed: {exc}", file=sys.stderr)
        return 2

    manifest = build_manifest(
        fetched,
        window_start=window_start,
        window_end=window_end,
        current=_current_settings(),
    )
    # newline="" — Windows text mode would otherwise turn every \n into \r\n.
    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"window: {window_start.isoformat()} -> {window_end.isoformat()}")
    print(
        "sample: {nominations} nominations / {minutes} minutes / "
        "{attempts} admission attempts".format(
            nominations=manifest["sample"]["nominations"],
            minutes=manifest["sample"]["nomination_minutes"],
            attempts=manifest["sample"]["admission_attempts"],
        )
    )
    for governor in manifest["governors"]:
        print(
            f"  {governor['setting']}: current={governor['current_value']} "
            f"recommended={governor['recommended_value']} "
            f"({governor['reason']})"
        )
        print(f"    {governor['derivation']}")
    print(f"manifest written: {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover — operator entry point
    raise SystemExit(main())
