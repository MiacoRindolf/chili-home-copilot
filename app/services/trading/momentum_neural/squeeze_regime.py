"""S6 (Ross "Forcing a Crash" 08-21) — SUPPRESSION/SQUEEZE market-regime index.

Ang H1 2026 ay suppression regime (ang MEMX short algo ay lumalabas "as soon as
the stock was up more than 50%"; lahat ng mover ay nagra-round-trip, walang
nakaka-500%). Pagkatapos ng May 11, 2026 LNAI lawsuit ay nawala ang algo at
naging squeeze regime (ASTC $4.5→$26, ZJYL $1→$18 sa 10s; "I can't even count
the number of stocks that have gone up over 1,000%"). Ang axis na ito ang
nagpapangalan sa binary na iyon mula sa SARILING datos ng scanner:

  * DAILY AGGREGATES (``momentum_squeeze_regime_daily``, mig369): kada UTC na
    araw, ilan ang +50%/+100% movers sa ``momentum_viability_history`` at ilan
    doon ang NAG-HOLD (ang huling obserbasyon ng araw ay nagpapanatili ng >=
    kalahati ng max gain) vs NAG-FADE (round trip). Ang mover na itinigil ng
    scanner bago ang huling bahagi ng araw ay UNRESOLVED (hindi kasama sa
    ratio — katapatan sa sukat, hindi hula).
  * FTD AGGREGATE (opsyonal, ``sec_fails_to_deliver`` #1102): market-total at
    mover-subset na fail quantity ng pinakabagong settlement date — regulatory
    phantom-supply evidence. OBSERVABILITY LANG sa phase na ito; HINDI bahagi
    ng label (may ~30-araw na lag ang SEC files).
  * LABEL (suppression | neutral | squeeze): ang trailing ``_WINDOW_DAYS`` na
    window vs ang ``_TRAILING_DAYS`` na baseline, gamit ang PAREHONG p20/p80
    percentile symmetry ng breadth regime (isang documented base).

OBSERVABILITY-FIRST (utos ng operator): ang label ay nilo-log, pini-persist
(daily table + regime snapshots), at inilalakip sa ``BreadthRegime`` — pero
WALANG sizing/selection na gumagamit nito sa phase na ito. Ang tilt wiring ay
hiwalay na phase pagkatapos mabasa ang live na datos.

PERF (bridge-starvation lesson 08-20): ang mabigat na per-day scan ay sa BATCH
job lang (scheduler, 3:10 LA) at index-only sa pamamagitan ng partial covering
index (mig369, mig363 pattern). Ang hot-path reader ay LIMIT-25 na PK read ng
1-row-per-day na table, may sariling memo — ligtas sa ilalim ng breadth lock.
FAIL-OPEN kahit saan: walang table / walang rows / DB error => neutral.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ....config import settings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Documented bases ────────────────────────────────────────────────────────────
# Ang suppression-algo trigger sa video: "as soon as the stock was up more than
# 50%" [00:32:54]. KEEP IN SYNC sa mig369 partial-index predicate (change_pct >=
# 50.0) — ang literal sa SQL ay galing dito para manatiling index-only ang scan.
_MOVER_PCT = 50.0
# S6: "bilang ng >= +100% movers per araw" — ang malalaking mover na sinusukat
# ni Ross para sa regime shift.
_BIG_MOVER_PCT = 100.0
# HELD = ang huling obserbasyon ng araw ay nagpapanatili ng >= kalahati ng max
# intraday gain; mas mababa = FADED (ang "round trip" ni Ross). Kalahati ang
# ONE documented split ng hold-vs-give-back.
_HOLD_RETAIN_FRAC = 0.5
# Ang hold/fade verdict ay bilang lang kapag ang HULING obserbasyon ng mover ay
# nasa huling (1 - 0.75) = 1/4 ng day span — kapag mas maaga itong ibinagsak ng
# scanner sa board, UNRESOLVED (hindi natin nakita ang katapusan).
_EOD_RESOLVE_FRAC = 0.75
# Rolling regime window (S6: "Rolling 5-10 araw" — ang maikling dulo para sa
# responsiveness) vs trailing baseline (kapareho ng breadth _TRAILING_SESSIONS).
_WINDOW_DAYS = 5
_TRAILING_DAYS = 20
# Parehong percentile floor ng breadth regime (p20/p80 symmetry) — ang isang
# documented base ng regime family.
_PCTL_FLOOR = 0.20
# Backfill horizon: ang history retention ay 30d; 2-araw na margin para hindi
# kailanman ma-persist ang bahagyang-na-prune na araw bilang kumpletong bilang.
_BACKFILL_DAYS = 28
# Kailangan ng ganito karaming baseline day (at ratio day) bago tumawag ng
# hindi-neutral na label — kapareho ng breadth "thin_baseline" floor.
_MIN_BASELINE_DAYS = 5
# Reader memo TTL: ang daily table ay minsan-kada-araw lang nagbabago; 300s ay
# sagana na para sa hot-path dedupe nang hindi tumatagal ang label flip.
_READ_MEMO_TTL_S = 300.0


@dataclass(frozen=True)
class SqueezeRegime:
    """Ang market-level suppression/squeeze axis. Neutral = fail-open default."""
    label: str                       # "suppression" | "neutral" | "squeeze"
    reason: str
    window_hold_ratio: float | None  # held/(held+faded) sa window; None kapag walang resolved mover
    window_movers_50: int
    window_movers_100: int
    as_of_day: str | None            # ISO date ng pinakabagong daily row sa window


_NEUTRAL_SQUEEZE = SqueezeRegime(
    label="neutral", reason="no_daily_rows", window_hold_ratio=None,
    window_movers_50=0, window_movers_100=0, as_of_day=None,
)

_READ_MEMO: dict[str, Any] = {"value": None, "computed_at_monotonic": None}
_LAST_LOGGED_LABEL: dict[str, str | None] = {"label": None}


def _now_utc(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_squeeze_regime_enabled", True))


def label_from_daily_rows(rows: list[dict[str, Any]]) -> SqueezeRegime:
    """PURE label computation mula sa daily rows (BAGONG-UNA ang pagkakasunod).

    Window (huling ``_WINDOW_DAYS``) vs baseline (susunod na ``_TRAILING_DAYS``):
      * squeeze_hold      — may mover at ang window hold-ratio ay >= baseline p80
                            (at ang mover rate ay hindi bumagsak sa ilalim ng median)
      * squeeze_emergent  — halos WALANG mover ang baseline (walang mabuong ratio
                            distribution) pero may mover na ngayon at karamihan ay
                            nagho-hold — ang post-May-11 na hugis mula sa patay na H1
      * suppression_fade  — ang window hold-ratio ay <= baseline p20 (round trips)
      * suppression_vanish— dating may mover ang baseline pero nawala sila ngayon
      * mixed / thin_baseline / no_daily_rows — neutral
    Ang strict-separation guards (``> p20`` / ``< p80``) ang pumipigil sa
    degenerate na baseline (lahat magkapareho) na tumawag ng regime."""
    from .breadth_regime import _percentile

    window = rows[:_WINDOW_DAYS]
    baseline = rows[_WINDOW_DAYS:_WINDOW_DAYS + _TRAILING_DAYS]
    if not window:
        return _NEUTRAL_SQUEEZE

    w_m50 = sum(int(r.get("movers_50") or 0) for r in window)
    w_m100 = sum(int(r.get("movers_100") or 0) for r in window)
    w_held = sum(int(r.get("held_50") or 0) for r in window)
    w_faded = sum(int(r.get("faded_50") or 0) for r in window)
    w_resolved = w_held + w_faded
    hold_ratio = (w_held / w_resolved) if w_resolved > 0 else None
    w_rate = w_m50 / float(len(window))
    as_of = window[0].get("day")
    as_of_iso = as_of.isoformat() if isinstance(as_of, date) else (str(as_of) if as_of else None)

    def _mk(label: str, reason: str) -> SqueezeRegime:
        return SqueezeRegime(
            label=label, reason=reason, window_hold_ratio=hold_ratio,
            window_movers_50=w_m50, window_movers_100=w_m100, as_of_day=as_of_iso,
        )

    if len(baseline) < _MIN_BASELINE_DAYS:
        return _mk("neutral", "thin_baseline")

    b_rates = sorted(float(int(r.get("movers_50") or 0)) for r in baseline)
    b_ratios = sorted(
        int(r.get("held_50") or 0) / float(int(r.get("held_50") or 0) + int(r.get("faded_50") or 0))
        for r in baseline
        if (int(r.get("held_50") or 0) + int(r.get("faded_50") or 0)) > 0
    )
    p20_rate = _percentile(b_rates, _PCTL_FLOOR) or 0.0
    p50_rate = _percentile(b_rates, 0.5) or 0.0
    p80_rate = _percentile(b_rates, 1.0 - _PCTL_FLOOR) or 0.0
    ratio_ok = len(b_ratios) >= _MIN_BASELINE_DAYS
    p20_ratio = _percentile(b_ratios, _PCTL_FLOOR) if ratio_ok else None
    p80_ratio = _percentile(b_ratios, 1.0 - _PCTL_FLOOR) if ratio_ok else None

    # SQUEEZE — may mover at NAGHO-HOLD sila.
    if hold_ratio is not None and w_rate > 0:
        if (
            ratio_ok
            and p80_ratio is not None and p20_ratio is not None
            and hold_ratio >= p80_ratio and hold_ratio > p20_ratio
            and w_rate >= p50_rate
        ):
            return _mk("squeeze", "squeeze_hold")
        if not ratio_ok and w_rate > p80_rate and hold_ratio >= _HOLD_RETAIN_FRAC:
            return _mk("squeeze", "squeeze_emergent")

    # SUPPRESSION — nagra-round-trip ang mga mover, o tuluyang nawala.
    if (
        hold_ratio is not None and ratio_ok
        and p20_ratio is not None and p80_ratio is not None
        and hold_ratio <= p20_ratio and hold_ratio < p80_ratio
    ):
        return _mk("suppression", "suppression_fade")
    if p50_rate > 0 and w_rate <= p20_rate and w_rate < p50_rate:
        return _mk("suppression", "suppression_vanish")

    return _mk("neutral", "mixed")


def read_squeeze_regime(db: "Session | None") -> SqueezeRegime:
    """Hot-path reader: LIMIT-25 PK read ng daily table -> label. Memoized
    (``_READ_MEMO_TTL_S``); bypass ang memo sa GOLDEN/CHILI_PYTEST (kapareho ng
    breadth memo semantics). FAIL-OPEN: anumang error => neutral."""
    if db is None or not _enabled():
        return _NEUTRAL_SQUEEZE
    import os as _os
    import time as _time

    _bypass = _os.environ.get("GOLDEN") == "1" or bool(_os.environ.get("CHILI_PYTEST"))
    if not _bypass:
        _at = _READ_MEMO.get("computed_at_monotonic")
        _val = _READ_MEMO.get("value")
        if _val is not None and _at is not None and (_time.monotonic() - float(_at)) < _READ_MEMO_TTL_S:
            return _val
    try:
        from sqlalchemy import text

        # SAVEPOINT — a "fail-open" read is only fail-open if a failure cannot
        # poison the CALLER'S transaction (2026-08-23). This runs on the
        # caller's session; when the SELECT fails, PostgreSQL aborts the whole
        # transaction, and while the ``except`` below happily returns neutral,
        # every later statement in that transaction dies with
        # InFailedSqlTransaction. That is not fail-open — it is fail-closed
        # with collateral damage, and it silently killed entire golden replay
        # runs whose sink simply had no ``momentum_squeeze_regime_daily`` table
        # (migrations do not run there). The nested transaction rolls back only
        # this probe.
        with db.begin_nested():
            rows = db.execute(
                text(
                    "SELECT day, movers_50, movers_100, held_50, faded_50 "
                    "FROM momentum_squeeze_regime_daily ORDER BY day DESC LIMIT :lim"
                ),
                {"lim": _WINDOW_DAYS + _TRAILING_DAYS},
            ).fetchall()
        result = label_from_daily_rows([
            {
                "day": r[0], "movers_50": r[1], "movers_100": r[2],
                "held_50": r[3], "faded_50": r[4],
            }
            for r in rows
        ])
    except Exception:
        logger.debug("[squeeze_regime] read failed (fail-open to neutral)", exc_info=True)
        return _NEUTRAL_SQUEEZE
    if not _bypass:
        _READ_MEMO["value"] = result
        _READ_MEMO["computed_at_monotonic"] = _time.monotonic()
    if _LAST_LOGGED_LABEL.get("label") != result.label:
        _LAST_LOGGED_LABEL["label"] = result.label
        logger.info(
            "[squeeze_regime] label=%s reason=%s hold_ratio=%s movers50=%d movers100=%d as_of=%s",
            result.label, result.reason,
            None if result.window_hold_ratio is None else round(result.window_hold_ratio, 3),
            result.window_movers_50, result.window_movers_100, result.as_of_day,
        )
    return result


def compute_daily_mover_stats(db: "Session", day: date) -> dict[str, Any] | None:
    """Isang UTC na araw -> mover hold/fade aggregates. None kapag WALANG history
    rows sa araw na iyon (non-trading day o na-prune na — huwag mag-persist ng
    phantom zero row).

    Tatlong bounded query: (1) mover set via partial covering index (index-only),
    (2) day span (dalawang index probe sa ix_mvh_observed_id — LAGING bounded sa
    isang araw; tingnan ang PG I/O starvation lesson), (3) huling obserbasyon ng
    bawat mover via ix_mvh_symbol_observed."""
    from sqlalchemy import text

    d0 = datetime(day.year, day.month, day.day)
    d1 = d0 + timedelta(days=1)

    span = db.execute(
        text(
            "SELECT MIN(observed_at), MAX(observed_at) FROM momentum_viability_history "
            "WHERE observed_at >= :d0 AND observed_at < :d1"
        ),
        {"d0": d0, "d1": d1},
    ).fetchone()
    if span is None or span[0] is None or span[1] is None:
        return None
    day_start, day_end = span[0], span[1]
    span_s = max(0.0, (day_end - day_start).total_seconds())
    eod_cutoff = day_start + timedelta(seconds=span_s * _EOD_RESOLVE_FRAC)

    # Ang literal na threshold (hindi bind param) ang nagpapahintulot sa planner
    # na gamitin ang mig369 partial index (ang predicate ay dapat ma-imply).
    movers = db.execute(
        text(
            "SELECT symbol, MAX(change_pct) AS max_pct "
            "FROM momentum_viability_history "
            f"WHERE observed_at >= :d0 AND observed_at < :d1 AND change_pct >= {_MOVER_PCT:.1f} "
            "AND symbol NOT LIKE '%-USD%' GROUP BY symbol"
        ),
        {"d0": d0, "d1": d1},
    ).fetchall()
    max_by_sym = {str(r[0]).upper(): float(r[1]) for r in movers if r[1] is not None}
    if not max_by_sym:
        return {
            "movers_50": 0, "movers_100": 0, "held_50": 0, "faded_50": 0,
            "unresolved_50": 0, "mover_symbols": [],
            "detail": {"movers": [], "day_span_s": int(span_s)},
        }

    syms = sorted(max_by_sym)
    last_rows = db.execute(
        text(
            "SELECT DISTINCT ON (symbol) symbol, observed_at, change_pct "
            "FROM momentum_viability_history "
            "WHERE observed_at >= :d0 AND observed_at < :d1 AND symbol = ANY(:syms) "
            "ORDER BY symbol, observed_at DESC"
        ),
        {"d0": d0, "d1": d1, "syms": syms},
    ).fetchall()
    last_by_sym = {str(r[0]).upper(): (r[1], float(r[2]) if r[2] is not None else None) for r in last_rows}

    held = faded = unresolved = 0
    detail_movers: list[dict[str, Any]] = []
    for sym in syms:
        max_pct = max_by_sym[sym]
        last_at, last_pct = last_by_sym.get(sym, (None, None))
        resolved = last_at is not None and last_pct is not None and last_at >= eod_cutoff
        is_held = bool(resolved and last_pct >= _HOLD_RETAIN_FRAC * max_pct)
        if not resolved:
            unresolved += 1
        elif is_held:
            held += 1
        else:
            faded += 1
        detail_movers.append({
            "s": sym, "max": round(max_pct, 1),
            "last": None if last_pct is None else round(last_pct, 1),
            "resolved": bool(resolved), "held": is_held,
        })
    detail_movers.sort(key=lambda m: -(m["max"] or 0.0))
    return {
        "movers_50": len(syms),
        "movers_100": sum(1 for v in max_by_sym.values() if v >= _BIG_MOVER_PCT),
        "held_50": held, "faded_50": faded, "unresolved_50": unresolved,
        "mover_symbols": syms,
        "detail": {"movers": detail_movers[:50], "day_span_s": int(span_s)},
    }


def _ftd_aggregate(db: "Session", day: date, mover_symbols: list[str]) -> dict[str, Any]:
    """Market-level FTD observability para sa daily row: ang pinakabagong
    settlement date <= day (may ~30-araw na lag ang SEC files) — total fail
    quantity, bilang ng simbolo, at ang subset sa mga mover ng araw. FAIL-OPEN:
    walang table / walang rows => lahat None."""
    from sqlalchemy import text

    out: dict[str, Any] = {
        "ftd_settlement_date": None, "ftd_total_qty": None,
        "ftd_symbols": None, "ftd_mover_qty": None,
    }
    try:
        row = db.execute(
            text(
                "SELECT settlement_date, SUM(fail_qty), COUNT(DISTINCT symbol), "
                "COALESCE(SUM(fail_qty) FILTER (WHERE symbol = ANY(:syms)), 0) "
                "FROM sec_fails_to_deliver "
                "WHERE settlement_date = (SELECT MAX(settlement_date) "
                "FROM sec_fails_to_deliver WHERE settlement_date <= :day) "
                "GROUP BY settlement_date"
            ),
            {"day": day, "syms": mover_symbols or [""]},
        ).fetchone()
        if row is not None:
            out["ftd_settlement_date"] = row[0]
            out["ftd_total_qty"] = int(row[1] or 0)
            out["ftd_symbols"] = int(row[2] or 0)
            out["ftd_mover_qty"] = int(row[3] or 0)
    except Exception:
        logger.debug("[squeeze_regime] FTD aggregate failed (fail-open)", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    return out


def refresh_squeeze_regime_daily(db: "Session", *, now: datetime | None = None) -> dict[str, str]:
    """BATCH producer (scheduler daily o operator): i-compute at i-upsert ang mga
    nawawalang daily row sa huling ``_BACKFILL_DAYS`` (ang kahapon ay LAGING
    nire-recompute — idempotent upsert ang naghi-heal ng partial na compute).
    HINDI ito tinatawag sa anumang entry/decision path."""
    from sqlalchemy import text

    report: dict[str, str] = {}
    if not _enabled():
        return {"skipped": "flag_off"}
    today = _now_utc(now).date()
    try:
        existing = {
            r[0] for r in db.execute(
                text("SELECT day FROM momentum_squeeze_regime_daily WHERE day >= :lo"),
                {"lo": today - timedelta(days=_BACKFILL_DAYS)},
            ).fetchall()
        }
    except Exception:
        logger.warning("[squeeze_regime] daily table unreadable (refresh aborted)", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return {"error": "daily_table_unreadable"}

    for k in range(1, _BACKFILL_DAYS + 1):
        day = today - timedelta(days=k)
        if day in existing and k > 1:
            continue
        try:
            stats = compute_daily_mover_stats(db, day)
            if stats is None:
                report[day.isoformat()] = "no_rows"
                continue
            ftd = _ftd_aggregate(db, day, stats["mover_symbols"])
            db.execute(
                text(
                    "INSERT INTO momentum_squeeze_regime_daily "
                    "(day, movers_50, movers_100, held_50, faded_50, unresolved_50, "
                    " ftd_settlement_date, ftd_total_qty, ftd_symbols, ftd_mover_qty, "
                    " detail_json, computed_at) "
                    "VALUES (:day, :m50, :m100, :held, :faded, :unres, "
                    " :ftd_sd, :ftd_total, :ftd_syms, :ftd_mover, "
                    " CAST(:detail AS jsonb), NOW()) "
                    "ON CONFLICT (day) DO UPDATE SET "
                    " movers_50 = EXCLUDED.movers_50, movers_100 = EXCLUDED.movers_100, "
                    " held_50 = EXCLUDED.held_50, faded_50 = EXCLUDED.faded_50, "
                    " unresolved_50 = EXCLUDED.unresolved_50, "
                    " ftd_settlement_date = EXCLUDED.ftd_settlement_date, "
                    " ftd_total_qty = EXCLUDED.ftd_total_qty, "
                    " ftd_symbols = EXCLUDED.ftd_symbols, "
                    " ftd_mover_qty = EXCLUDED.ftd_mover_qty, "
                    " detail_json = EXCLUDED.detail_json, computed_at = NOW()"
                ),
                {
                    "day": day, "m50": stats["movers_50"], "m100": stats["movers_100"],
                    "held": stats["held_50"], "faded": stats["faded_50"],
                    "unres": stats["unresolved_50"],
                    "ftd_sd": ftd["ftd_settlement_date"], "ftd_total": ftd["ftd_total_qty"],
                    "ftd_syms": ftd["ftd_symbols"], "ftd_mover": ftd["ftd_mover_qty"],
                    "detail": json.dumps(stats["detail"], default=str),
                },
            )
            db.commit()
            report[day.isoformat()] = (
                f"movers={stats['movers_50']} held={stats['held_50']} "
                f"faded={stats['faded_50']} unresolved={stats['unresolved_50']}"
            )
            logger.info(
                "[squeeze_regime] daily %s: movers50=%d movers100=%d held=%d faded=%d "
                "unresolved=%d ftd_total=%s ftd_movers=%s",
                day.isoformat(), stats["movers_50"], stats["movers_100"],
                stats["held_50"], stats["faded_50"], stats["unresolved_50"],
                ftd["ftd_total_qty"], ftd["ftd_mover_qty"],
            )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("[squeeze_regime] daily %s failed (fail-open)", day.isoformat(), exc_info=True)
            report[day.isoformat()] = "error"
    # I-invalidate ang reader memo para agad makita ng hot path ang bagong araw,
    # tapos i-log ang nabuong label (ang refresh ang natural na daily log point).
    _READ_MEMO["value"] = None
    _READ_MEMO["computed_at_monotonic"] = None
    reg = read_squeeze_regime(db)
    logger.info(
        "[squeeze_regime] refresh done: label=%s reason=%s hold_ratio=%s movers50=%d as_of=%s days=%s",
        reg.label, reg.reason,
        None if reg.window_hold_ratio is None else round(reg.window_hold_ratio, 3),
        reg.window_movers_50, reg.as_of_day, len(report),
    )
    return report
