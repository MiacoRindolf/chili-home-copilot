"""Phase 1 — point-in-time feature collection for pattern-survival.

Each daily snapshot writes one row per active pattern into
``pattern_survival_features``. The 30-day-ahead label backfill and the
LightGBM training loop live in Phase 2.

Features fall into five buckets:

  1. Lifecycle context  — lifecycle stage, age_days, promoted_at
  2. Realized 30d perf  — trades, hit_rate, expectancy, sharpe, drawdown,
                          recent PnL slope
  3. CPCV evidence      — DSR, PBO, n_paths, promotion_confident bool
  4. Regime / diversity — current regime tag, family Herfindahl, count
  5. Drift              — max feature PSI, recert_overdue bool

All features come from existing tables (``scan_pattern``, ``trade``,
``trading_pattern_outcome``, ``regime_snapshot``,
``pattern_regime_performance_ledger``, ``recert_status``). No new
ingestion is required at Phase 1.

Networking: zero. This is a SQL-only feature collector that runs in the
scheduler container.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class PatternSurvivalFeatures:
    """In-memory representation of one snapshot row before persistence."""
    scan_pattern_id: int
    snapshot_date: date
    pattern_lifecycle: Optional[str] = None
    age_days: Optional[int] = None
    promoted_at: Optional[datetime] = None
    trades_30d: Optional[int] = None
    hit_rate_30d: Optional[float] = None
    expectancy_30d_pct: Optional[float] = None
    sharpe_30d: Optional[float] = None
    max_drawdown_30d_pct: Optional[float] = None
    pnl_slope_14d: Optional[float] = None
    cpcv_dsr: Optional[float] = None
    cpcv_pbo: Optional[float] = None
    cpcv_n_paths: Optional[int] = None
    cpcv_promotion_confident: Optional[bool] = None
    regime_at_snapshot: Optional[str] = None
    family_concentration_herfindahl: Optional[float] = None
    family_active_count: Optional[int] = None
    feature_psi_max: Optional[float] = None
    recert_overdue: Optional[bool] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_insert_params(self) -> dict[str, Any]:
        return {
            "spid": self.scan_pattern_id,
            "sd": self.snapshot_date,
            "lc": self.pattern_lifecycle,
            "ad": self.age_days,
            "pa": self.promoted_at,
            "t30": self.trades_30d,
            "hr30": self.hit_rate_30d,
            "exp30": self.expectancy_30d_pct,
            "sh30": self.sharpe_30d,
            "dd30": self.max_drawdown_30d_pct,
            "ps14": self.pnl_slope_14d,
            "dsr": self.cpcv_dsr,
            "pbo": self.cpcv_pbo,
            "np": self.cpcv_n_paths,
            "cc": self.cpcv_promotion_confident,
            "rg": self.regime_at_snapshot,
            "hh": self.family_concentration_herfindahl,
            "fc": self.family_active_count,
            "psi": self.feature_psi_max,
            "ro": self.recert_overdue,
            "ej": json.dumps(self.extras) if self.extras else None,
        }


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _collect_lifecycle(db: Session, pattern_id: int) -> tuple[Optional[str], Optional[int], Optional[datetime]]:
    """Return (lifecycle_stage, age_days, promoted_at).

    The repo's actual column naming: lifecycle_stage (not lifecycle),
    lifecycle_changed_at as proxy for promoted_at (no dedicated column),
    created_at as the fallback origin date.
    """
    row = db.execute(
        text(
            """
            SELECT lifecycle_stage, lifecycle_changed_at,
                   EXTRACT(DAY FROM (NOW() - COALESCE(lifecycle_changed_at,
                                                      created_at))) AS age_days
            FROM scan_patterns
            WHERE id = :p
            LIMIT 1
            """
        ),
        {"p": pattern_id},
    ).fetchone()
    if row is None:
        return None, None, None
    age = int(row[2]) if row[2] is not None else None
    return (row[0], age, row[1])


def _collect_realized_30d(db: Session, pattern_id: int) -> dict[str, Optional[float]]:
    """Aggregate realized perf over the last 30 days from closed trades.

    PnL pct is derived from (pnl / (entry_price * quantity)) since the
    table doesn't store realized_pnl_pct directly.
    """
    row = db.execute(
        text(
            """
            WITH t AS (
                SELECT pnl,
                       CASE WHEN entry_price IS NOT NULL
                                 AND quantity IS NOT NULL
                                 AND entry_price * quantity > 0
                            THEN pnl / (entry_price * quantity) * 100.0
                       END AS pnl_pct
                FROM trading_trades
                WHERE scan_pattern_id = :p
                  AND exit_date >= NOW() - INTERVAL '30 days'
                  AND pnl IS NOT NULL
            )
            SELECT
                COUNT(*)::int AS trades,
                AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                AVG(pnl_pct) AS expectancy_pct,
                NULLIF(STDDEV_POP(pnl_pct), 0) AS pnl_pct_std,
                MIN(pnl_pct) AS worst_trade_pct
            FROM t
            """
        ),
        {"p": pattern_id},
    ).fetchone()
    if row is None:
        return {"trades_30d": 0, "hit_rate_30d": None,
                "expectancy_30d_pct": None, "sharpe_30d": None,
                "max_drawdown_30d_pct": None}
    trades = int(row[0] or 0)
    hr = _safe_float(row[1])
    exp_pct = _safe_float(row[2])
    sd_pct = _safe_float(row[3])
    worst = _safe_float(row[4])
    sharpe = (exp_pct / sd_pct) if (exp_pct is not None and sd_pct and sd_pct > 0) else None
    return {
        "trades_30d": trades,
        "hit_rate_30d": hr,
        "expectancy_30d_pct": exp_pct,
        "sharpe_30d": sharpe,
        "max_drawdown_30d_pct": worst,
    }


def _collect_pnl_slope_14d(db: Session, pattern_id: int) -> Optional[float]:
    """Linear regression slope of cumulative PnL over the last 14 days.

    Positive = pattern improving recently, negative = degrading.
    """
    rows = db.execute(
        text(
            """
            SELECT exit_date::date AS day, SUM(pnl) AS pnl_usd
            FROM trading_trades
            WHERE scan_pattern_id = :p
              AND exit_date >= NOW() - INTERVAL '14 days'
              AND pnl IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"p": pattern_id},
    ).fetchall()
    if not rows or len(rows) < 3:
        return None
    # Build cumulative
    cum: list[float] = []
    running = 0.0
    for r in rows:
        running += float(r[1] or 0)
        cum.append(running)
    n = len(cum)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(cum) / n
    num = sum((xs[i] - mean_x) * (cum[i] - mean_y) for i in range(n))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den <= 0:
        return None
    return num / den


def _collect_cpcv_evidence(db: Session, pattern_id: int) -> dict[str, Any]:
    """Pull the latest CPCV evidence row for this pattern, if any.

    Joins two sources:
      * cpcv_shadow_eval_log (latest pbo / cpcv_n_paths /
        would_pass_cpcv) — keeps shadow evaluation history
      * scan_patterns (cpcv_median_sharpe — used as DSR proxy until a
        true DSR column lands)
    """
    cpcv_dsr_proxy = None
    pbo = None
    n_paths = None
    confident = None

    try:
        row = db.execute(
            text(
                """
                SELECT pbo, cpcv_n_paths, would_pass_cpcv
                FROM cpcv_shadow_eval_log
                WHERE scan_pattern_id = :p
                ORDER BY evaluated_at DESC
                LIMIT 1
                """
            ),
            {"p": pattern_id},
        ).fetchone()
        if row is not None:
            pbo = _safe_float(row[0])
            n_paths = int(row[1]) if row[1] is not None else None
            confident = bool(row[2]) if row[2] is not None else None
    except Exception as e:
        logger.debug("[pattern_survival] cpcv shadow eval query failed: %s", e)

    try:
        row2 = db.execute(
            text(
                "SELECT cpcv_median_sharpe FROM scan_patterns WHERE id = :p"
            ),
            {"p": pattern_id},
        ).fetchone()
        if row2 is not None:
            cpcv_dsr_proxy = _safe_float(row2[0])
    except Exception as e:
        logger.debug("[pattern_survival] scan_patterns sharpe query failed: %s", e)

    return {
        "cpcv_dsr": cpcv_dsr_proxy,
        "cpcv_pbo": pbo,
        "cpcv_n_paths": n_paths,
        "cpcv_promotion_confident": confident,
    }


def _collect_regime_and_diversity(db: Session) -> tuple[Optional[str], Optional[float], Optional[int]]:
    """Snapshot-wide regime + family-concentration context.

    Same regime / Herfindahl applies to every pattern in this snapshot
    pass — they're both a function of the active book, not the pattern.
    """
    regime = None
    try:
        row = db.execute(
            text(
                "SELECT regime FROM regime_snapshot "
                "ORDER BY as_of DESC LIMIT 1"
            )
        ).fetchone()
        if row is not None:
            regime = row[0]
    except Exception as e:
        logger.debug("[pattern_survival] regime query failed: %s", e)

    fam_h, fam_n = None, None
    try:
        rows = db.execute(
            text(
                """
                SELECT COALESCE(sp.hypothesis_family, 'unknown') AS fam,
                       SUM(t.pnl) AS pnl_30d
                FROM trading_trades t
                LEFT JOIN scan_patterns sp ON sp.id = t.scan_pattern_id
                WHERE t.exit_date >= NOW() - INTERVAL '30 days'
                  AND t.pnl IS NOT NULL
                GROUP BY 1
                """
            )
        ).fetchall()
        if rows:
            pnls = [abs(float(r[1] or 0)) for r in rows]
            total = sum(pnls)
            if total > 0:
                fam_h = sum((p / total) ** 2 for p in pnls)
            fam_n = len([p for p in pnls if p > 0])
    except Exception as e:
        logger.debug("[pattern_survival] diversity query failed: %s", e)
    return regime, fam_h, fam_n


def snapshot_pattern_features(
    db: Session,
    *,
    scan_pattern_id: int,
    snapshot_date: Optional[date] = None,
) -> Optional[int]:
    """Build features for one pattern and persist into pattern_survival_features.

    Idempotent on (scan_pattern_id, snapshot_date) — re-running the same
    day overwrites prior values. Returns the row id, or None on failure.
    """
    sd = snapshot_date or datetime.now(timezone.utc).date()

    lifecycle, age_days, promoted_at = _collect_lifecycle(db, scan_pattern_id)
    realized = _collect_realized_30d(db, scan_pattern_id)
    pnl_slope = _collect_pnl_slope_14d(db, scan_pattern_id)
    cpcv = _collect_cpcv_evidence(db, scan_pattern_id)
    regime, fam_h, fam_n = _collect_regime_and_diversity(db)

    feats = PatternSurvivalFeatures(
        scan_pattern_id=scan_pattern_id,
        snapshot_date=sd,
        pattern_lifecycle=lifecycle,
        age_days=age_days,
        promoted_at=promoted_at,
        trades_30d=realized.get("trades_30d"),
        hit_rate_30d=realized.get("hit_rate_30d"),
        expectancy_30d_pct=realized.get("expectancy_30d_pct"),
        sharpe_30d=realized.get("sharpe_30d"),
        max_drawdown_30d_pct=realized.get("max_drawdown_30d_pct"),
        pnl_slope_14d=pnl_slope,
        cpcv_dsr=cpcv.get("cpcv_dsr"),
        cpcv_pbo=cpcv.get("cpcv_pbo"),
        cpcv_n_paths=cpcv.get("cpcv_n_paths"),
        cpcv_promotion_confident=cpcv.get("cpcv_promotion_confident"),
        regime_at_snapshot=regime,
        family_concentration_herfindahl=fam_h,
        family_active_count=fam_n,
        feature_psi_max=None,        # Phase 2 — wire from drift_monitor
        recert_overdue=None,         # Phase 2 — wire from recert_status
    )

    try:
        params = feats.to_insert_params()
        row = db.execute(
            text(
                """
                INSERT INTO pattern_survival_features
                    (scan_pattern_id, snapshot_date, pattern_lifecycle,
                     age_days, promoted_at, trades_30d, hit_rate_30d,
                     expectancy_30d_pct, sharpe_30d, max_drawdown_30d_pct,
                     pnl_slope_14d, cpcv_dsr, cpcv_pbo, cpcv_n_paths,
                     cpcv_promotion_confident, regime_at_snapshot,
                     family_concentration_herfindahl, family_active_count,
                     feature_psi_max, recert_overdue, features_json)
                VALUES (:spid, :sd, :lc, :ad, :pa, :t30, :hr30, :exp30,
                        :sh30, :dd30, :ps14, :dsr, :pbo, :np, :cc, :rg,
                        :hh, :fc, :psi, :ro, CAST(:ej AS jsonb))
                ON CONFLICT (scan_pattern_id, snapshot_date)
                DO UPDATE SET
                    pattern_lifecycle = EXCLUDED.pattern_lifecycle,
                    age_days = EXCLUDED.age_days,
                    promoted_at = EXCLUDED.promoted_at,
                    trades_30d = EXCLUDED.trades_30d,
                    hit_rate_30d = EXCLUDED.hit_rate_30d,
                    expectancy_30d_pct = EXCLUDED.expectancy_30d_pct,
                    sharpe_30d = EXCLUDED.sharpe_30d,
                    max_drawdown_30d_pct = EXCLUDED.max_drawdown_30d_pct,
                    pnl_slope_14d = EXCLUDED.pnl_slope_14d,
                    cpcv_dsr = EXCLUDED.cpcv_dsr,
                    cpcv_pbo = EXCLUDED.cpcv_pbo,
                    cpcv_n_paths = EXCLUDED.cpcv_n_paths,
                    cpcv_promotion_confident = EXCLUDED.cpcv_promotion_confident,
                    regime_at_snapshot = EXCLUDED.regime_at_snapshot,
                    family_concentration_herfindahl =
                        EXCLUDED.family_concentration_herfindahl,
                    family_active_count = EXCLUDED.family_active_count,
                    feature_psi_max = EXCLUDED.feature_psi_max,
                    recert_overdue = EXCLUDED.recert_overdue,
                    features_json = EXCLUDED.features_json
                RETURNING id
                """
            ),
            params,
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[pattern_survival] persist features for pattern %s failed: %s",
            scan_pattern_id, e,
        )
        return None


def run_pattern_survival_snapshot_job(db: Session) -> dict[str, Any]:
    """Daily job: snapshot features for every live + challenged pattern.

    Flag-gated by ``chili_pattern_survival_classifier_enabled``. When OFF,
    returns immediately with ``{"skipped": "flag_off"}``. When ON, iterates
    over patterns in lifecycle in ('live', 'challenged') and snapshots each.
    """
    from ....config import settings

    if not getattr(settings, "chili_pattern_survival_classifier_enabled", False):
        return {"skipped": "flag_off"}

    try:
        rows = db.execute(
            text(
                "SELECT id FROM scan_patterns "
                "WHERE lifecycle_stage IN ('live', 'challenged') "
                "ORDER BY id"
            )
        ).fetchall()
    except Exception as e:
        logger.warning("[pattern_survival] enumerate patterns failed: %s", e)
        return {"error": str(e)[:200]}

    snapshot_d = datetime.now(timezone.utc).date()
    ok = 0
    fail = 0
    for r in rows or []:
        pid = int(r[0])
        result = snapshot_pattern_features(
            db, scan_pattern_id=pid, snapshot_date=snapshot_d,
        )
        if result is not None:
            ok += 1
        else:
            fail += 1

    logger.info(
        "[pattern_survival] snapshot_job: %d ok, %d failed (date=%s)",
        ok, fail, snapshot_d,
    )
    return {
        "snapshot_date": snapshot_d.isoformat(),
        "patterns_snapshotted": ok,
        "patterns_failed": fail,
    }
