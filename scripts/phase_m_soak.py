"""Phase M.1 Docker soak — verifies pattern x regime performance
ledger shadow rollout inside the running ``chili`` container.

Usage (inside the container):

    docker compose exec chili python scripts/phase_m_soak.py

Each check is printed as ``[PASS]`` or ``[FAIL]``. The script exits
non-zero on any failure so the CI-style runner can gate on it.

Checks cover:

1.  Migration 144 applied; table + indexes present.
2.  ``brain_pattern_regime_perf_*`` settings visible.
3.  Pure model: determinism of ``compute_ledger_run_id``.
4.  Pure model: single-trade fan-out to 8 cells.
5.  Pure model: confidence gate (``n_trades >= min_trades_per_cell``).
6.  Pure model: mixed wins/losses aggregation math.
7.  ``RegimeLookup.resolve`` most-recent-at-or-before semantics.
8.  ``RegimeLookup.resolve`` returns ``regime_unavailable`` when empty.
9.  ``compute_and_persist`` in ``off`` mode is a no-op.
10. ``compute_and_persist`` in ``authoritative`` mode raises
    ``RuntimeError`` (refused).
11. ``compute_and_persist`` in ``shadow`` mode writes all cells for a
    synthetic ``trades_override`` + ``lookup_override`` input.
12. Repeated ``compute_and_persist`` calls are append-only
    (deterministic ``ledger_run_id`` per ``as_of_date``).
13. ``pattern_regime_perf_summary`` frozen wire shape (11 top-level
    keys + 8-dim ``by_dimension``).
14. ``max_patterns`` cap truncates the pattern set (log signal only).
15. Additive-only: L.17-L.22 snapshot row counts unchanged around a
    full M.1 write cycle.
"""
from __future__ import annotations

import hashlib
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Tuple

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

os.environ.setdefault("BRAIN_PATTERN_REGIME_PERF_OPS_LOG_ENABLED", "false")

FAILS = 0
TOTAL = 0


def _run(label: str, fn: Callable[[], None]) -> None:
    global FAILS, TOTAL
    TOTAL += 1
    try:
        fn()
        print(f"[PASS] {label}")
    except AssertionError as exc:
        FAILS += 1
        print(f"[FAIL] {label}: {exc}")
    except Exception as exc:  # pragma: no cover - surface anything else
        FAILS += 1
        tb = traceback.format_exc().splitlines()[-3:]
        print(f"[FAIL] {label}: unexpected {type(exc).__name__}: {exc}")
        for line in tb:
            print(f"         {line}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_migration_144_applied() -> None:
    from sqlalchemy import inspect, text as sql_text

    from app.db import engine

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert (
        "trading_pattern_regime_performance_daily" in tables
    ), "table missing"

    with engine.connect() as conn:
        row = conn.execute(
            sql_text(
                "SELECT version_id FROM schema_version "
                "WHERE version_id = '144_pattern_regime_performance_ledger'"
            )
        ).fetchone()
        assert row is not None, "migration 144 not in schema_version"

    idx_names = {
        idx["name"]
        for idx in insp.get_indexes(
            "trading_pattern_regime_performance_daily"
        )
    }
    expected_indexes = {
        "ix_pattern_regime_perf_as_of",
        "ix_pattern_regime_perf_lookup",
        "ix_pattern_regime_perf_run",
        "ix_pattern_regime_perf_confident",
    }
    missing = expected_indexes - idx_names
    assert not missing, f"missing indexes: {missing}"


def check_settings_visible() -> None:
    from app.config import settings

    assert hasattr(settings, "brain_pattern_regime_perf_mode")
    assert hasattr(settings, "brain_pattern_regime_perf_ops_log_enabled")
    assert hasattr(settings, "brain_pattern_regime_perf_cron_hour")
    assert hasattr(settings, "brain_pattern_regime_perf_cron_minute")
    assert hasattr(settings, "brain_pattern_regime_perf_window_days")
    assert hasattr(
        settings, "brain_pattern_regime_perf_min_trades_per_cell"
    )
    assert hasattr(settings, "brain_pattern_regime_perf_max_patterns")
    assert hasattr(settings, "brain_pattern_regime_perf_lookback_days")


def check_ledger_run_id_determinism() -> None:
    from app.services.trading.pattern_regime_performance_model import (
        compute_ledger_run_id,
    )

    a = compute_ledger_run_id(as_of_date=date(2024, 1, 15), window_days=90)
    b = compute_ledger_run_id(as_of_date=date(2024, 1, 15), window_days=90)
    c = compute_ledger_run_id(as_of_date=date(2024, 1, 16), window_days=90)
    d = compute_ledger_run_id(as_of_date=date(2024, 1, 15), window_days=60)
    assert a == b, "same inputs must yield same id"
    assert a != c, "date must matter"
    assert a != d, "window_days must matter"
    expected = hashlib.sha256(
        b"pattern_regime_perf:2024-01-15:90"
    ).hexdigest()[:16]
    assert a == expected


def _single_label_lookup(label: str):
    from app.services.trading.pattern_regime_performance_model import (
        DEFAULT_DIMENSIONS,
        DIMENSION_TICKER_REGIME,
        RegimeLookup,
    )

    lookup = RegimeLookup()
    for d in DEFAULT_DIMENSIONS:
        if d == DIMENSION_TICKER_REGIME:
            continue
        lookup.market_wide[d] = [(date(2024, 1, 1), label)]
    lookup.ticker_keyed[DIMENSION_TICKER_REGIME] = {
        "ZZZ": [(date(2024, 1, 1), label)]
    }
    lookup.sort_inplace()
    return lookup


def check_single_trade_fan_out() -> None:
    from app.services.trading.pattern_regime_performance_model import (
        ClosedTradeRecord,
        DEFAULT_DIMENSIONS,
        PatternRegimePerfConfig,
        PatternRegimePerfInput,
        build_pattern_regime_cells,
    )

    trade = ClosedTradeRecord(
        pattern_id=7,
        ticker="ZZZ",
        entry_date=date(2024, 1, 10),
        exit_date=date(2024, 1, 12),
        pnl_pct=0.02,
        hold_days=2.0,
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2024, 1, 15),
        trades=[trade],
        lookup=_single_label_lookup("label_x"),
        config=PatternRegimePerfConfig(window_days=30, min_trades_per_cell=3),
    )
    out = build_pattern_regime_cells(inp)
    assert len(out.cells) == len(DEFAULT_DIMENSIONS), (
        f"expected {len(DEFAULT_DIMENSIONS)} cells, got {len(out.cells)}"
    )
    for cell in out.cells:
        assert cell.pattern_id == 7
        assert cell.regime_label == "label_x"
        assert cell.n_trades == 1
        assert cell.has_confidence is False  # below min_trades_per_cell=3


def check_confidence_gate() -> None:
    from app.services.trading.pattern_regime_performance_model import (
        ClosedTradeRecord,
        PatternRegimePerfConfig,
        PatternRegimePerfInput,
        build_pattern_regime_cells,
    )

    trades = [
        ClosedTradeRecord(
            pattern_id=1,
            ticker="AAA",
            entry_date=date(2024, 1, 5) + timedelta(days=i),
            exit_date=date(2024, 1, 6) + timedelta(days=i),
            pnl_pct=0.01,
            hold_days=1.0,
        )
        for i in range(5)
    ]
    inp = PatternRegimePerfInput(
        as_of_date=date(2024, 1, 20),
        trades=trades,
        lookup=_single_label_lookup("lab"),
        config=PatternRegimePerfConfig(window_days=30, min_trades_per_cell=3),
    )
    out = build_pattern_regime_cells(inp)
    assert all(c.has_confidence for c in out.cells), (
        "5 trades should pass min=3 gate in every cell"
    )

    inp2 = PatternRegimePerfInput(
        as_of_date=date(2024, 1, 20),
        trades=trades,
        lookup=_single_label_lookup("lab"),
        config=PatternRegimePerfConfig(window_days=30, min_trades_per_cell=10),
    )
    out2 = build_pattern_regime_cells(inp2)
    assert not any(c.has_confidence for c in out2.cells), (
        "5 trades should fail min=10 gate"
    )


def check_mixed_aggregation_math() -> None:
    from app.services.trading.pattern_regime_performance_model import (
        ClosedTradeRecord,
        PatternRegimePerfConfig,
        PatternRegimePerfInput,
        build_pattern_regime_cells,
    )

    pnls = [0.02, -0.01, 0.03, -0.02, 0.01]
    trades = [
        ClosedTradeRecord(
            pattern_id=42,
            ticker="ABC",
            entry_date=date(2024, 1, 5) + timedelta(days=i),
            exit_date=date(2024, 1, 6) + timedelta(days=i),
            pnl_pct=p,
            hold_days=1.0,
        )
        for i, p in enumerate(pnls)
    ]
    inp = PatternRegimePerfInput(
        as_of_date=date(2024, 1, 20),
        trades=trades,
        lookup=_single_label_lookup("r"),
        config=PatternRegimePerfConfig(window_days=30, min_trades_per_cell=3),
    )
    out = build_pattern_regime_cells(inp)
    cell = out.cells[0]
    assert cell.n_trades == 5
    assert cell.n_wins == 3
    assert abs((cell.hit_rate or 0.0) - 0.6) < 1e-6
    assert cell.sum_pnl is not None and abs(cell.sum_pnl - 0.03) < 1e-6
    assert cell.mean_pnl_pct is not None and abs(
        cell.mean_pnl_pct - 0.006
    ) < 1e-6
    assert cell.profit_factor is not None and cell.profit_factor > 1.0


def check_lookup_most_recent_at_or_before() -> None:
    from app.services.trading.pattern_regime_performance_model import (
        DIMENSION_MACRO_REGIME,
        RegimeLookup,
    )

    lookup = RegimeLookup()
    lookup.market_wide[DIMENSION_MACRO_REGIME] = [
        (date(2024, 1, 10), "A"),
        (date(2024, 1, 12), "B"),
        (date(2024, 1, 20), "C"),
    ]
    lookup.sort_inplace()
    assert lookup.resolve(
        dimension=DIMENSION_MACRO_REGIME,
        as_of_on_or_before=date(2024, 1, 11),
        ticker="X",
    ) == "A"
    assert lookup.resolve(
        dimension=DIMENSION_MACRO_REGIME,
        as_of_on_or_before=date(2024, 1, 12),
        ticker="X",
    ) == "B"
    assert lookup.resolve(
        dimension=DIMENSION_MACRO_REGIME,
        as_of_on_or_before=date(2024, 1, 19),
        ticker="X",
    ) == "B"
    assert lookup.resolve(
        dimension=DIMENSION_MACRO_REGIME,
        as_of_on_or_before=date(2024, 1, 5),
        ticker="X",
    ) == "regime_unavailable"


def check_lookup_unavailable() -> None:
    from app.services.trading.pattern_regime_performance_model import (
        DIMENSION_SESSION_LABEL,
        LABEL_UNAVAILABLE,
        RegimeLookup,
    )

    lookup = RegimeLookup()
    assert lookup.resolve(
        dimension=DIMENSION_SESSION_LABEL,
        as_of_on_or_before=date(2024, 1, 1),
        ticker="X",
    ) == LABEL_UNAVAILABLE


def _synthetic_trades_and_lookup(*, trade_count: int = 5):
    from app.services.trading.pattern_regime_performance_model import (
        ClosedTradeRecord,
    )

    trades = [
        ClosedTradeRecord(
            pattern_id=101 + (i % 2),
            ticker="ZZZ",
            entry_date=date(2024, 3, 1) + timedelta(days=i),
            exit_date=date(2024, 3, 2) + timedelta(days=i),
            pnl_pct=0.01 if i % 2 == 0 else -0.005,
            hold_days=1.0,
        )
        for i in range(trade_count)
    ]
    return trades, _single_label_lookup("test_label")


def check_off_mode_noop() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_performance_service import (
        compute_and_persist,
    )

    db = SessionLocal()
    try:
        trades, lookup = _synthetic_trades_and_lookup()
        ref = compute_and_persist(
            db,
            as_of_date=date(2024, 3, 15),
            mode_override="off",
            trades_override=trades,
            lookup_override=lookup,
        )
        assert ref is None, "off mode must return None"
    finally:
        db.close()


def check_authoritative_refused() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_performance_service import (
        compute_and_persist,
    )

    db = SessionLocal()
    try:
        trades, lookup = _synthetic_trades_and_lookup()
        raised = False
        try:
            compute_and_persist(
                db,
                as_of_date=date(2024, 3, 15),
                mode_override="authoritative",
                trades_override=trades,
                lookup_override=lookup,
            )
        except RuntimeError:
            raised = True
        assert raised, "authoritative mode must raise RuntimeError"
    finally:
        db.close()


def _count_rows(db, run_id: str) -> int:
    from sqlalchemy import text as sql_text

    return int(
        db.execute(
            sql_text(
                "SELECT COUNT(*) FROM "
                "trading_pattern_regime_performance_daily "
                "WHERE ledger_run_id = :rid"
            ),
            {"rid": run_id},
        ).scalar_one()
    )


def check_shadow_persists_cells() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_performance_model import (
        DEFAULT_DIMENSIONS,
        compute_ledger_run_id,
    )
    from app.services.trading.pattern_regime_performance_service import (
        compute_and_persist,
    )

    db = SessionLocal()
    try:
        trades, lookup = _synthetic_trades_and_lookup(trade_count=6)
        as_of = date(2024, 4, 1)
        run_id = compute_ledger_run_id(as_of_date=as_of, window_days=90)
        ref = compute_and_persist(
            db,
            as_of_date=as_of,
            mode_override="shadow",
            trades_override=trades,
            lookup_override=lookup,
        )
        assert ref is not None, "shadow mode must return ref"
        assert ref.ledger_run_id == run_id
        assert ref.mode == "shadow"
        # 2 patterns x 8 dimensions x 1 label = 16 cells
        assert ref.cells_persisted == 2 * len(DEFAULT_DIMENSIONS)
        assert _count_rows(db, run_id) == ref.cells_persisted
    finally:
        db.close()


def check_append_only_repeated_writes() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_performance_model import (
        compute_ledger_run_id,
    )
    from app.services.trading.pattern_regime_performance_service import (
        compute_and_persist,
    )

    db = SessionLocal()
    try:
        trades, lookup = _synthetic_trades_and_lookup(trade_count=4)
        as_of = date(2024, 4, 2)
        run_id = compute_ledger_run_id(as_of_date=as_of, window_days=90)
        ref1 = compute_and_persist(
            db,
            as_of_date=as_of,
            mode_override="shadow",
            trades_override=trades,
            lookup_override=lookup,
        )
        ref2 = compute_and_persist(
            db,
            as_of_date=as_of,
            mode_override="shadow",
            trades_override=trades,
            lookup_override=lookup,
        )
        assert ref1 is not None and ref2 is not None
        assert ref1.ledger_run_id == run_id
        assert ref2.ledger_run_id == run_id  # deterministic
        total = _count_rows(db, run_id)
        assert total == ref1.cells_persisted + ref2.cells_persisted, (
            f"append-only: expected "
            f"{ref1.cells_persisted + ref2.cells_persisted}, "
            f"got {total}"
        )
    finally:
        db.close()


def check_summary_frozen_shape() -> None:
    from app.db import SessionLocal
    from app.services.trading.pattern_regime_performance_service import (
        pattern_regime_perf_summary,
    )

    db = SessionLocal()
    try:
        s = pattern_regime_perf_summary(db, lookback_days=7)
    finally:
        db.close()

    expected_top = {
        "mode",
        "lookback_days",
        "window_days",
        "min_trades_per_cell",
        "latest_as_of_date",
        "latest_ledger_run_id",
        "ledger_rows_total",
        "confident_cells_total",
        "by_dimension",
        "top_pattern_label_expectancy",
        "bottom_pattern_label_expectancy",
    }
    missing = expected_top - set(s.keys())
    extra = set(s.keys()) - expected_top
    assert not missing and not extra, (
        f"summary keys drift: missing={missing} extra={extra}"
    )
    expected_dims = {
        "macro_regime",
        "breadth_label",
        "cross_asset_label",
        "ticker_regime",
        "vol_regime",
        "dispersion_label",
        "correlation_label",
        "session_label",
    }
    assert set(s["by_dimension"].keys()) == expected_dims
    for dim in expected_dims:
        d = s["by_dimension"][dim]
        assert set(d.keys()) == {
            "total_cells",
            "confident_cells",
            "by_label",
        }, dim


def check_max_patterns_cap_truncates() -> None:
    from app.services.trading.pattern_regime_performance_service import (
        _apply_max_patterns_cap,
    )
    from app.services.trading.pattern_regime_performance_model import (
        ClosedTradeRecord,
    )

    trades: List[ClosedTradeRecord] = []
    # 6 patterns, trade counts varying so ranking is unambiguous.
    for pid, cnt in enumerate([2, 5, 1, 7, 3, 4], start=1):
        for i in range(cnt):
            trades.append(
                ClosedTradeRecord(
                    pattern_id=pid,
                    ticker="T",
                    entry_date=date(2024, 5, 1) + timedelta(days=i),
                    exit_date=date(2024, 5, 2) + timedelta(days=i),
                    pnl_pct=0.0,
                    hold_days=1.0,
                )
            )
    kept, truncated = _apply_max_patterns_cap(trades, max_patterns=3)
    kept_patterns = sorted({t.pattern_id for t in kept})
    # Top 3 by count: pid=4 (7), pid=2 (5), pid=6 (4).
    assert kept_patterns == [2, 4, 6], (
        f"expected top-3 [2,4,6], got {kept_patterns}"
    )
    assert truncated == 3


def check_additive_only_l17_l22_counts_unchanged() -> None:
    from sqlalchemy import text as sql_text

    from app.db import SessionLocal
    from app.services.trading.pattern_regime_performance_service import (
        compute_and_persist,
    )

    peer_tables = [
        "trading_macro_regime_snapshots",
        "trading_breadth_relstr_snapshots",
        "trading_cross_asset_snapshots",
        "trading_ticker_regime_snapshots",
        "trading_vol_dispersion_snapshots",
        "trading_intraday_session_snapshots",
    ]
    db = SessionLocal()
    try:
        before: Dict[str, int] = {}
        for t in peer_tables:
            before[t] = int(
                db.execute(
                    sql_text(f"SELECT COUNT(*) FROM {t}")
                ).scalar_one()
            )
        trades, lookup = _synthetic_trades_and_lookup(trade_count=4)
        compute_and_persist(
            db,
            as_of_date=date(2024, 4, 3),
            mode_override="shadow",
            trades_override=trades,
            lookup_override=lookup,
        )
        after: Dict[str, int] = {}
        for t in peer_tables:
            after[t] = int(
                db.execute(
                    sql_text(f"SELECT COUNT(*) FROM {t}")
                ).scalar_one()
            )
    finally:
        db.close()

    diffs = {t: (before[t], after[t]) for t in peer_tables if before[t] != after[t]}
    assert not diffs, f"additive-only broken: {diffs}"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    _run("01. migration 144 applied + indexes present", check_migration_144_applied)
    _run("02. brain_pattern_regime_perf_* settings visible", check_settings_visible)
    _run("03. compute_ledger_run_id deterministic", check_ledger_run_id_determinism)
    _run("04. single trade fan-out to 8 cells", check_single_trade_fan_out)
    _run("05. confidence gate on min_trades_per_cell", check_confidence_gate)
    _run("06. mixed wins/losses aggregation math", check_mixed_aggregation_math)
    _run("07. RegimeLookup most-recent-at-or-before", check_lookup_most_recent_at_or_before)
    _run("08. RegimeLookup unavailable fallback", check_lookup_unavailable)
    _run("09. compute_and_persist off mode no-op", check_off_mode_noop)
    _run("10. compute_and_persist authoritative refused", check_authoritative_refused)
    _run("11. compute_and_persist shadow persists all cells", check_shadow_persists_cells)
    _run("12. repeated writes are append-only (deterministic run_id)", check_append_only_repeated_writes)
    _run("13. pattern_regime_perf_summary frozen shape", check_summary_frozen_shape)
    _run("14. max_patterns cap truncates", check_max_patterns_cap_truncates)
    _run("15. additive-only: L.17-L.22 row counts unchanged", check_additive_only_l17_l22_counts_unchanged)

    print()
    print(f"Phase M.1 soak: {TOTAL - FAILS}/{TOTAL} checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
