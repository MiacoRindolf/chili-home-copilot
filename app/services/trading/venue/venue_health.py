"""P1.2 — venue health + circuit breaker.

Rolling per-venue latency (submit-to-ack P50/P95, ack-to-fill P50/P95),
error rate, and rate-limit-hit rate computed from the
``TradingExecutionEvent`` stream. A venue flips to ``degraded`` when P95
latency or error rate crosses the configured threshold; the degraded
signal feeds the AutoTrader v1 and momentum_neural live entry gates so
new entries pause while open positions continue to manage themselves.

Why a separate module (not just a query in ops_health_service)
--------------------------------------------------------------
* **Per-call freshness.** The breaker is consulted on every entry
  decision. A stale cached ops-health snapshot would let a venue that's
  just started misbehaving continue to receive entries.
* **Tight settings integration.** Thresholds are read live so tests
  monkeypatch cleanly (mirrors :mod:`rate_limiter`).
* **Isolation from ops-health aggregation.** ``ops_health_service`` is
  for the UI; the circuit breaker is an operational gate. Both read the
  same underlying event stream but have different freshness/failure
  requirements.

Contract
--------
    health = summarize_venue(db, venue="coinbase")
    # {
    #   "venue": "coinbase", "window_sec": 300, "samples": 42,
    #   "submit_to_ack_p50_ms": 180.0, "submit_to_ack_p95_ms": 320.0,
    #   "ack_to_fill_p50_ms": 220.0, "ack_to_fill_p95_ms": 480.0,
    #   "error_rate": 0.023, "rate_limit_rate": 0.0,
    #   "n_errors": 1, "n_rate_limits": 0, "n_acks": 20, "n_fills": 19,
    #   "status": "healthy" | "degraded" | "insufficient_data" | "disabled",
    #   "reason": None | "ack_to_fill_p95_ms_exceeded:480.0>..." | ...,
    #   "thresholds": {...},
    # }

    if is_venue_degraded(db, venue="coinbase"):
        return {"ok": False, "blocked_by": "venue_degraded", "venue": "coinbase"}

What counts as an "error"
-------------------------
Events whose ``event_type`` is ``"reject"`` or whose ``status`` resolves
to the canonical ``REJECTED`` state. Rate-limit events
(``event_type="rate_limit"``) are counted separately — they're the
limiter's own exhaustion signal, not a venue fault. Both contribute to
``is_venue_degraded`` via the aggregate ``error_rate`` since either is a
"stop sending new orders here" signal operationally.

What this is NOT
----------------
* Not a distributed / cross-process health monitor. Per-process read of
  the shared DB event stream is sufficient: the events ARE the signal.
* Not automatic — the breaker only pauses new entries; exits / stop
  updates / reconciliation continue normally so open positions stay
  protected.
* Not a replacement for broker-level health endpoints — those are
  advisory; THIS breaker trips on what we actually observed in the last
  5 minutes of traffic, which is the stronger signal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Canonical status spellings that map to REJECTED via the P1.1 state
# machine. Kept local so we don't force-import the state-machine module
# (which would add a cycle on callers that themselves feed the stream).
_REJECT_STATUSES = frozenset({
    "rejected", "failed", "denied",
})

# Broker-source → canonical venue name. We key health on the normalized
# venue so "coinbase_spot" and "coinbase" share one breaker.
_BROKER_TO_VENUE = {
    "coinbase": "coinbase",
    "coinbase_spot": "coinbase",
    "crypto": "coinbase",
    "robinhood": "robinhood",
    "robinhood_spot": "robinhood",
    "equities": "robinhood",
    "manual": "manual",
}


def canonicalize_venue(raw: str | None) -> str:
    """Normalize a venue / broker_source string to the canonical venue key."""
    if not raw:
        return "unknown"
    s = str(raw).strip().lower()
    return _BROKER_TO_VENUE.get(s, s)


# ── Feature flag + threshold resolution ───────────────────────────────


def _is_enabled() -> bool:
    """Feature flag read live so tests' monkeypatch takes effect immediately."""
    try:
        from ....config import settings
        return bool(getattr(settings, "chili_venue_health_enabled", False))
    except Exception:
        return False


def _resolve_thresholds() -> dict[str, Any]:
    """Return the active threshold config — read live per call."""
    try:
        from ....config import settings
        return {
            "window_sec": int(getattr(settings, "chili_venue_health_window_sec", 300)),
            "min_samples": int(getattr(settings, "chili_venue_health_min_samples", 5)),
            "ack_to_fill_p95_ms": int(getattr(settings, "chili_venue_health_ack_to_fill_p95_ms", 5000)),
            "submit_to_ack_p95_ms": int(getattr(settings, "chili_venue_health_submit_to_ack_p95_ms", 3000)),
            "error_rate": float(getattr(settings, "chili_venue_health_error_rate_pct", 0.10)),
            "auto_switch_to_paper": bool(getattr(settings, "chili_venue_health_auto_switch_to_paper", False)),
        }
    except Exception:
        return {
            "window_sec": 300,
            "min_samples": 5,
            "ack_to_fill_p95_ms": 5000,
            "submit_to_ack_p95_ms": 3000,
            "error_rate": 0.10,
            "auto_switch_to_paper": False,
        }


# ── Percentile helper (module-local so we don't cross-import exec_audit) ─


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    rows = sorted(float(v) for v in values)
    idx = max(0, min(len(rows) - 1, int(round((len(rows) - 1) * q))))
    return rows[idx]


# ── Core reader ───────────────────────────────────────────────────────


def summarize_venue(
    db: Session,
    *,
    venue: str,
    window_sec: int | None = None,
) -> dict[str, Any]:
    """Return a summary of venue health for the given rolling window.

    When the feature flag is off returns a frozen-shape dict with
    ``status="disabled"`` so callers can treat it uniformly (no
    ``is_venue_degraded`` short-circuit needed upstream).
    """
    cfg = _resolve_thresholds()
    win = int(window_sec) if window_sec is not None else int(cfg["window_sec"])
    venue_key = canonicalize_venue(venue)

    base = {
        "venue": venue_key,
        "window_sec": win,
        "samples": 0,
        "lifecycle_samples": 0,
        "latency_samples": 0,
        "submit_to_ack_p50_ms": None,
        "submit_to_ack_p95_ms": None,
        "ack_to_fill_p50_ms": None,
        "ack_to_fill_p95_ms": None,
        "error_rate": 0.0,
        "rate_limit_rate": 0.0,
        "n_events": 0,
        "n_errors": 0,
        "n_rate_limits": 0,
        "n_acks": 0,
        "n_fills": 0,
        "status": "disabled" if not _is_enabled() else "insufficient_data",
        "reason": None,
        "thresholds": {
            "ack_to_fill_p95_ms": cfg["ack_to_fill_p95_ms"],
            "submit_to_ack_p95_ms": cfg["submit_to_ack_p95_ms"],
            "error_rate": cfg["error_rate"],
            "min_samples": cfg["min_samples"],
        },
    }
    if not _is_enabled():
        return base

    since = datetime.now(timezone.utc) - timedelta(seconds=max(1, win))
    # DB recorded_at is naive UTC by convention (DateTime without tz).
    since_naive = since.replace(tzinfo=None)

    # Pull the event stream for this venue in the window. We filter on
    # ``venue`` when populated and fall back to ``broker_source`` when
    # venue is NULL — older rows may have only broker_source.
    rows = db.execute(text("""
        SELECT event_type, status, submit_to_ack_ms, ack_to_first_fill_ms,
               venue, broker_source
        FROM trading_execution_events
        WHERE recorded_at >= :since
          AND (LOWER(COALESCE(venue, '')) = :v
               OR LOWER(COALESCE(broker_source, '')) = :v
               OR LOWER(COALESCE(broker_source, '')) = :v_raw)
    """), {"since": since_naive, "v": venue_key, "v_raw": (venue or "").strip().lower()}).fetchall()

    if not rows:
        return base

    submit_acks: list[float] = []
    ack_fills: list[float] = []
    n_events = 0
    n_errors = 0
    n_rate_limits = 0
    n_acks = 0
    n_fills = 0

    for r in rows:
        event_type = (r[0] or "").strip().lower()
        status = (r[1] or "").strip().lower()
        s2a = r[2]
        a2f = r[3]
        n_events += 1

        if event_type == "rate_limit":
            n_rate_limits += 1
            continue

        if event_type == "reject" or status in _REJECT_STATUSES:
            n_errors += 1
            continue

        if event_type == "ack" or status in ("ack", "acknowledged", "open", "confirmed", "pending"):
            n_acks += 1
        if event_type in ("fill", "partial_fill"):
            n_fills += 1

        if s2a is not None:
            try:
                submit_acks.append(float(s2a))
            except (TypeError, ValueError):
                pass
        if a2f is not None:
            try:
                ack_fills.append(float(a2f))
            except (TypeError, ValueError):
                pass

    # ``samples`` legacy meaning was "ack-to-fill observations", but that
    # scoped the breaker away from exactly the case we want to catch: a
    # venue rejecting 100% of orders has zero ack-to-fill samples. Split
    # the two notions:
    #
    #   * ``lifecycle_samples`` — count of all events (fills + rejects +
    #     rate-limits + acks). Used as the floor for the ERROR-RATE
    #     evaluation. If a venue produces 3 rejects and nothing else, we
    #     have a clear "stop" signal and MUST trip the breaker.
    #   * ``latency_samples``  — count of ack-to-fill observations. Used
    #     as the floor for the LATENCY thresholds. Without this we can't
    #     compute a meaningful P95.
    #
    # ``samples`` in the output keeps its historical meaning (ack-to-fill
    # count) so dashboards don't silently change; the new counters are
    # exposed alongside.
    lifecycle_samples = n_events
    latency_samples = len(ack_fills)
    submit_ack_samples = len(submit_acks)

    # Error rate divides over all lifecycle events (not just acks) so a
    # venue that only rejects — zero acks — still shows 100% error rate.
    denom = max(1, n_events)
    err_rate = round((n_errors + n_rate_limits) / denom, 4)
    rl_rate = round(n_rate_limits / denom, 4)

    s2a_p50 = _percentile(submit_acks, 0.50)
    s2a_p95 = _percentile(submit_acks, 0.95)
    a2f_p50 = _percentile(ack_fills, 0.50)
    a2f_p95 = _percentile(ack_fills, 0.95)

    out = dict(base)
    out.update({
        "samples": int(latency_samples),
        "lifecycle_samples": int(lifecycle_samples),
        "latency_samples": int(latency_samples),
        "n_events": int(n_events),
        "n_errors": int(n_errors),
        "n_rate_limits": int(n_rate_limits),
        "n_acks": int(n_acks),
        "n_fills": int(n_fills),
        "submit_to_ack_p50_ms": round(s2a_p50, 2) if s2a_p50 is not None else None,
        "submit_to_ack_p95_ms": round(s2a_p95, 2) if s2a_p95 is not None else None,
        "ack_to_fill_p50_ms": round(a2f_p50, 2) if a2f_p50 is not None else None,
        "ack_to_fill_p95_ms": round(a2f_p95, 2) if a2f_p95 is not None else None,
        "error_rate": err_rate,
        "rate_limit_rate": rl_rate,
    })

    min_samples = int(cfg["min_samples"])

    # Evaluate thresholds in priority order — error rate first because a
    # venue rejecting every order is the clearest "stop" signal. Error
    # rate needs ``lifecycle_samples`` (any event), NOT latency samples,
    # so a 100%-reject venue with no fills still trips.
    if lifecycle_samples >= min_samples and err_rate >= float(cfg["error_rate"]):
        out["status"] = "degraded"
        out["reason"] = (
            f"error_rate={err_rate:.4f}>={float(cfg['error_rate']):.4f} "
            f"(errors={n_errors}, rate_limits={n_rate_limits}, events={n_events})"
        )
        return out

    # Latency thresholds require ack-to-fill / submit-to-ack samples.
    if (
        latency_samples >= min_samples
        and a2f_p95 is not None
        and a2f_p95 >= int(cfg["ack_to_fill_p95_ms"])
    ):
        out["status"] = "degraded"
        out["reason"] = (
            f"ack_to_fill_p95_ms={a2f_p95:.1f}>={int(cfg['ack_to_fill_p95_ms'])}"
        )
        return out

    if (
        submit_ack_samples >= min_samples
        and s2a_p95 is not None
        and s2a_p95 >= int(cfg["submit_to_ack_p95_ms"])
    ):
        out["status"] = "degraded"
        out["reason"] = (
            f"submit_to_ack_p95_ms={s2a_p95:.1f}>={int(cfg['submit_to_ack_p95_ms'])}"
        )
        return out

    # Neither breach AND we have enough lifecycle signal to say so → healthy.
    if lifecycle_samples >= min_samples:
        out["status"] = "healthy"
        out["reason"] = None
        return out

    # Below the lifecycle floor we can't make a statement. ``insufficient_data``
    # communicates "no signal yet" to operators without tripping the breaker.
    out["status"] = "insufficient_data"
    out["reason"] = (
        f"lifecycle_samples={lifecycle_samples}<{min_samples}"
    )
    return out


# ── Public gate helpers ───────────────────────────────────────────────


def is_venue_degraded(db: Session, *, venue: str) -> bool:
    """True iff the venue is currently past a degraded threshold.

    ``insufficient_data`` is NOT degraded — too few samples means we
    can't make a statement. ``disabled`` is NOT degraded either — the
    feature flag off means operators haven't turned the gate on yet.
    Both paths fall through to ``False`` so unwired environments behave
    exactly as before.
    """
    if not _is_enabled():
        return False
    try:
        summary = summarize_venue(db, venue=venue)
    except Exception:
        # Defensive: a DB hiccup shouldn't cause a spurious degraded
        # flip. Log and pretend healthy — the rate limiter + idempotency
        # store already handle the worst-case (runaway-retry) scenarios.
        logger.exception("venue_health summarize failed for %s", venue)
        return False
    return summary.get("status") == "degraded"


def venue_degraded_reason(db: Session, *, venue: str) -> Optional[str]:
    """Return the ``reason`` string when degraded, else ``None``.

    Used by audit writers so the block decision is self-explanatory in
    ``AutoTraderRun.reason`` / session events.
    """
    if not _is_enabled():
        return None
    try:
        summary = summarize_venue(db, venue=venue)
    except Exception:
        return None
    if summary.get("status") != "degraded":
        return None
    return summary.get("reason")


def should_auto_switch_to_paper(db: Session, *, venue: str) -> bool:
    """Whether the ``auto_switch_to_paper`` config is on AND venue is degraded.

    Kept separate from ``is_venue_degraded`` so the gate caller can
    decide the behavior: block-and-retry (default) vs flip-mode-to-paper.
    """
    cfg = _resolve_thresholds()
    if not cfg.get("auto_switch_to_paper"):
        return False
    return is_venue_degraded(db, venue=venue)


# ── Error-event recorder (for rate-limit hits) ────────────────────────


def record_rate_limit_event(
    *,
    venue: str,
    ticker: str | None = None,
    source: str = "unspecified",
) -> None:
    """Persist a ``rate_limit`` row to ``trading_execution_events``.

    Called from rate-limiter exhaustion sites in venue adapters. Opens
    its own SessionLocal (adapters don't carry sessions) and commits.
    Never raises — a recording failure must not bubble up into the
    caller's order-placement path.

    We write a minimal row: venue + event_type='rate_limit' + status
    carrying the source tag ('cb_place_market' etc). The health summary
    counts these via ``event_type = 'rate_limit'`` independent of
    status, so status serves purely as a debugging breadcrumb.
    """
    try:
        from ....db import SessionLocal
        from ....models.trading import TradingExecutionEvent
    except Exception:
        return
    try:
        db = SessionLocal()
    except Exception:
        return
    try:
        venue_key = canonicalize_venue(venue)
        row = TradingExecutionEvent(
            ticker=ticker,
            venue=venue_key,
            broker_source=venue_key,
            event_type="rate_limit",
            status=(source or "unspecified")[:32],
            recorded_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


__all__ = [
    "canonicalize_venue",
    "is_venue_degraded",
    "record_rate_limit_event",
    "should_auto_switch_to_paper",
    "summarize_venue",
    "venue_degraded_reason",
]
