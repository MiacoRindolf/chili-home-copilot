"""Phase L.18 Docker soak - breadth + cross-sectional RS snapshot (shadow).

Verifies inside the running ``chili`` container that:

  1. Migration 139 applied (``trading_breadth_relstr_snapshots`` exists).
  2. ``BRAIN_BREADTH_RELSTR_MODE`` is visible on ``settings`` (and all
     related tuning thresholds).
  3. ``compute_breadth_relstr`` pure classifier returns the frozen
     output shape for risk_on / risk_off / mixed synthetic readings.
  4. ``compute_and_persist`` writes exactly one row when forced to
     shadow mode with synthetic readings.
  5. ``compute_and_persist`` is a no-op when the current mode is
     ``off`` and hard-refuses ``authoritative``.
  6. Coverage-score gate: compute_and_persist returns ``None`` and
     skips persistence when the coverage is below
     ``brain_breadth_relstr_min_coverage_score``.
  7. Determinism: same ``as_of_date`` yields identical ``snapshot_id``;
     a subsequent sweep with the same date is append-only (no silent
     overwrite).
  8. ``breadth_relstr_summary`` returns the frozen wire shape.
  9. Additive-only: L.17's ``trading_macro_regime_snapshots`` is not
     mutated by any L.18 write (row count is stable around the sweep).
"""
from __future__ import annotations

import os
import sys
from datetime import date

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.trading.breadth_relstr_model import (  # noqa: E402
    ALL_SYMBOLS,
    BREADTH_MIXED,
    BREADTH_RISK_OFF,
    BREADTH_RISK_ON,
    SECTOR_SYMBOLS,
    SECTOR_XLE,
    SECTOR_XLK,
    SECTOR_XLU,
    SYMBOL_IWM,
    SYMBOL_QQQ,
    SYMBOL_SPY,
    TREND_DOWN,
    TREND_FLAT,
    TREND_MISSING,
    TREND_UP,
    BreadthRelstrInput,
    UniverseMember,
    compute_breadth_relstr,
    compute_snapshot_id,
)
from app.services.trading.breadth_relstr_service import (  # noqa: E402
    breadth_relstr_summary,
    compute_and_persist,
)

SOAK_AS_OF = date(2026, 4, 17)
SOAK_AS_OF_2 = date(2026, 4, 18)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_l18_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_l18_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_breadth_relstr_snapshots "
        "WHERE as_of_date IN (:a, :b)"
    ), {"a": SOAK_AS_OF, "b": SOAK_AS_OF_2})
    db.commit()


def _member_up(sym: str, mom20: float = 0.04) -> UniverseMember:
    return UniverseMember(
        symbol=sym,
        missing=False,
        last_close=101.0,
        prev_close=100.0,
        momentum_20d=mom20,
        trend=TREND_UP,
        direction=TREND_UP,
        new_high_20d=(mom20 >= 0.03),
    )


def _member_down(sym: str, mom20: float = -0.04) -> UniverseMember:
    return UniverseMember(
        symbol=sym,
        missing=False,
        last_close=99.0,
        prev_close=100.0,
        momentum_20d=mom20,
        trend=TREND_DOWN,
        direction=TREND_DOWN,
        new_low_20d=(mom20 <= -0.03),
    )


def _member_flat(sym: str) -> UniverseMember:
    return UniverseMember(
        symbol=sym,
        missing=False,
        last_close=100.0,
        prev_close=100.0,
        momentum_20d=0.002,
        trend=TREND_FLAT,
        direction=TREND_FLAT,
    )


def _member_missing(sym: str) -> UniverseMember:
    return UniverseMember(
        symbol=sym,
        missing=True,
        trend=TREND_MISSING,
        direction=TREND_MISSING,
    )


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_breadth_relstr_snapshots')"
    )).scalar_one()
    _assert(
        row is not None,
        "trading_breadth_relstr_snapshots exists",
    )

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '139_breadth_relstr_snapshot'"
    )).fetchone()
    _assert(
        row is not None,
        "migration 139_breadth_relstr_snapshot recorded",
    )

    for attr in (
        "brain_breadth_relstr_mode",
        "brain_breadth_relstr_ops_log_enabled",
        "brain_breadth_relstr_cron_hour",
        "brain_breadth_relstr_cron_minute",
        "brain_breadth_relstr_min_coverage_score",
        "brain_breadth_relstr_trend_up_threshold",
        "brain_breadth_relstr_strong_trend_threshold",
        "brain_breadth_relstr_tilt_threshold",
        "brain_breadth_relstr_risk_on_ratio",
        "brain_breadth_relstr_risk_off_ratio",
        "brain_breadth_relstr_lookback_days",
    ):
        _assert(hasattr(settings, attr), f"settings.{attr} exists")


def _check_pure_model() -> None:
    # Risk-on: every sector up, XLK strongest so it leads RS, SPY/QQQ/IWM up.
    risk_on_members: list[UniverseMember] = []
    for s in SECTOR_SYMBOLS:
        mom = 0.06 if s == SECTOR_XLK else 0.03
        risk_on_members.append(_member_up(s, mom20=mom))
    # SPY/QQQ/IWM up but weaker than XLK so XLK leads.
    risk_on_members.append(_member_up(SYMBOL_SPY, mom20=0.025))
    risk_on_members.append(_member_up(SYMBOL_QQQ, mom20=0.03))
    risk_on_members.append(_member_up(SYMBOL_IWM, mom20=0.035))

    out_on = compute_breadth_relstr(BreadthRelstrInput(
        as_of_date=SOAK_AS_OF,
        members=risk_on_members,
    ))
    _assert(
        out_on.snapshot_id == compute_snapshot_id(SOAK_AS_OF),
        "snapshot_id deterministic",
    )
    _assert(
        out_on.breadth_label == BREADTH_RISK_ON,
        f"breadth_label=broad_risk_on when every member advancing "
        f"(got {out_on.breadth_label!r})",
    )
    _assert(out_on.breadth_numeric == 1, "breadth_numeric=1 when risk_on")
    _assert(
        out_on.members_sampled == len(ALL_SYMBOLS),
        f"members_sampled={len(ALL_SYMBOLS)} when all present",
    )
    _assert(
        out_on.members_advancing == len(ALL_SYMBOLS),
        "members_advancing==basket size when all up",
    )
    _assert(
        abs(out_on.advance_ratio - 1.0) < 1e-9,
        "advance_ratio=1.0 when all up",
    )
    _assert(
        abs(out_on.coverage_score - 1.0) < 1e-9,
        "coverage_score=1.0 when full basket present",
    )
    _assert(
        out_on.leader_sector == SECTOR_XLK,
        f"leader_sector=XLK when XLK has strongest RS "
        f"(got {out_on.leader_sector!r})",
    )

    # Risk-off: every sector down, SPY/QQQ/IWM down.
    risk_off_members: list[UniverseMember] = []
    for s in SECTOR_SYMBOLS:
        mom = -0.06 if s == SECTOR_XLE else -0.03
        risk_off_members.append(_member_down(s, mom20=mom))
    risk_off_members.append(_member_down(SYMBOL_SPY, mom20=-0.025))
    risk_off_members.append(_member_down(SYMBOL_QQQ, mom20=-0.03))
    risk_off_members.append(_member_down(SYMBOL_IWM, mom20=-0.035))

    out_off = compute_breadth_relstr(BreadthRelstrInput(
        as_of_date=SOAK_AS_OF,
        members=risk_off_members,
    ))
    _assert(
        out_off.breadth_label == BREADTH_RISK_OFF,
        f"breadth_label=broad_risk_off when every member declining "
        f"(got {out_off.breadth_label!r})",
    )
    _assert(out_off.breadth_numeric == -1, "breadth_numeric=-1 when risk_off")
    _assert(
        out_off.laggard_sector == SECTOR_XLE,
        f"laggard_sector=XLE when XLE has worst RS "
        f"(got {out_off.laggard_sector!r})",
    )

    # Mixed: half up, half down -> advance_ratio ~ 0.5, neither threshold.
    mixed_members: list[UniverseMember] = []
    for i, s in enumerate(SECTOR_SYMBOLS):
        if i % 2 == 0:
            mixed_members.append(_member_up(s, mom20=0.02))
        else:
            mixed_members.append(_member_down(s, mom20=-0.02))
    mixed_members.append(_member_flat(SYMBOL_SPY))
    mixed_members.append(_member_flat(SYMBOL_QQQ))
    mixed_members.append(_member_flat(SYMBOL_IWM))
    out_mid = compute_breadth_relstr(BreadthRelstrInput(
        as_of_date=SOAK_AS_OF,
        members=mixed_members,
    ))
    _assert(
        out_mid.breadth_label == BREADTH_MIXED,
        f"breadth_label=mixed when A/D neutral "
        f"(got {out_mid.breadth_label!r})",
    )
    _assert(out_mid.breadth_numeric == 0, "breadth_numeric=0 when mixed")

    # Partial coverage below 0.5 -> mixed fallback on the pure model.
    partial = [
        _member_up(SECTOR_XLK),
        _member_up(SECTOR_XLU),
    ] + [
        _member_missing(s) for s in ALL_SYMBOLS
        if s not in (SECTOR_XLK, SECTOR_XLU)
    ]
    out_partial = compute_breadth_relstr(BreadthRelstrInput(
        as_of_date=SOAK_AS_OF,
        members=partial,
    ))
    _assert(
        out_partial.symbols_sampled == 2,
        "partial symbols_sampled=2",
    )
    _assert(
        out_partial.symbols_missing == len(ALL_SYMBOLS) - 2,
        f"partial symbols_missing={len(ALL_SYMBOLS) - 2}",
    )
    _assert(
        abs(out_partial.coverage_score - (2 / len(ALL_SYMBOLS))) < 1e-6,
        f"partial coverage_score=2/{len(ALL_SYMBOLS)}",
    )


def _all_up_basket() -> list[UniverseMember]:
    members: list[UniverseMember] = []
    for s in SECTOR_SYMBOLS:
        mom = 0.06 if s == SECTOR_XLK else 0.03
        members.append(_member_up(s, mom20=mom))
    members.append(_member_up(SYMBOL_SPY, mom20=0.025))
    members.append(_member_up(SYMBOL_QQQ, mom20=0.03))
    members.append(_member_up(SYMBOL_IWM, mom20=0.035))
    return members


def _check_persist_shadow(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        members_override=_all_up_basket(),
    )
    _assert(row is not None, "compute_and_persist writes row in shadow")
    assert row is not None
    _assert(row.mode == "shadow", "persisted row mode=shadow")
    _assert(row.as_of_date == SOAK_AS_OF, "persisted row as_of_date matches")
    _assert(
        row.breadth_label == BREADTH_RISK_ON,
        f"persisted row breadth_label=broad_risk_on "
        f"(got {row.breadth_label!r})",
    )

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots "
        "WHERE as_of_date = :d AND mode = 'shadow'"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(count == 1, "exactly 1 row persisted for soak as_of")


def _check_off_and_authoritative(db) -> None:
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="off",
        members_override=_all_up_basket(),
    )
    _assert(row is None, "off mode is a no-op")

    raised = False
    try:
        compute_and_persist(
            db,
            as_of_date=SOAK_AS_OF,
            mode_override="authoritative",
            members_override=_all_up_basket(),
        )
    except RuntimeError:
        raised = True
    _assert(raised, "authoritative mode raises RuntimeError (refused)")


def _check_coverage_gate(db) -> None:
    # Partial: only 2/14 resolved -> below default min_coverage_score=0.5.
    partial = [
        _member_up(SECTOR_XLK),
        _member_up(SECTOR_XLU),
    ] + [
        _member_missing(s) for s in ALL_SYMBOLS
        if s not in (SECTOR_XLK, SECTOR_XLU)
    ]
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF_2,
        mode_override="shadow",
        members_override=partial,
    )
    _assert(row is None, "coverage_below_min skips persistence")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF_2}).scalar_one()
    _assert(count == 0, "no row persisted when coverage_below_min")


def _check_determinism_and_append_only(db) -> None:
    # Second write with the same as_of should append a new row (no upsert).
    row = compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        members_override=_all_up_basket(),
    )
    _assert(row is not None, "append-only second write succeeds")

    count = db.execute(text(
        "SELECT COUNT(*) FROM trading_breadth_relstr_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).scalar_one()
    _assert(count == 2, "append-only: 2 rows after second write")

    rows = db.execute(text(
        "SELECT snapshot_id FROM trading_breadth_relstr_snapshots "
        "WHERE as_of_date = :d"
    ), {"d": SOAK_AS_OF}).fetchall()
    ids = {r[0] for r in rows}
    _assert(len(ids) == 1, "same as_of shares same snapshot_id")
    _assert(
        compute_snapshot_id(SOAK_AS_OF) in ids,
        "snapshot_id matches compute_snapshot_id(as_of)",
    )


def _check_summary(db) -> None:
    payload = breadth_relstr_summary(db, lookback_days=14)
    expected_keys = {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_breadth_label",
        "by_leader_sector",
        "by_laggard_sector",
        "mean_advance_ratio",
        "mean_coverage_score",
        "latest_snapshot",
    }
    _assert(
        set(payload.keys()) == expected_keys,
        "breadth_relstr_summary frozen top-level shape",
    )
    _assert(
        set(payload["by_breadth_label"].keys()) == {
            "broad_risk_on", "mixed", "broad_risk_off",
        },
        "by_breadth_label has all three breadth labels",
    )
    _assert(payload["lookback_days"] == 14, "lookback_days echoes arg")
    _assert(
        isinstance(payload["by_leader_sector"], dict),
        "by_leader_sector is a dict",
    )
    _assert(
        isinstance(payload["by_laggard_sector"], dict),
        "by_laggard_sector is a dict",
    )


def _check_l17_additive(db) -> None:
    # Baseline: macro_regime_snapshots row count before any L.18 activity.
    pre = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)

    # Trigger one more L.18 shadow write.
    compute_and_persist(
        db,
        as_of_date=SOAK_AS_OF,
        mode_override="shadow",
        members_override=_all_up_basket(),
    )

    post = int(db.execute(text(
        "SELECT COUNT(*) FROM trading_macro_regime_snapshots"
    )).scalar_one() or 0)
    _assert(
        post == pre,
        f"L.17 macro snapshots unchanged by L.18 writes (pre={pre} post={post})",
    )


def main() -> int:
    print("[phase_l18_soak] starting")
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_schema_and_settings(db)
        _check_pure_model()
        _check_persist_shadow(db)
        _check_off_and_authoritative(db)
        _check_coverage_gate(db)
        _check_determinism_and_append_only(db)
        _check_summary(db)
        _check_l17_additive(db)
        _cleanup(db)
        print("[phase_l18_soak] ALL GREEN")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
