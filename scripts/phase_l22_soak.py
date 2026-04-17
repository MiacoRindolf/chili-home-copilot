"""Phase L.22 Docker soak — verifies intraday session regime snapshot
shadow rollout inside the running ``chili`` container.

Usage (inside the container):

    docker compose exec chili python scripts/phase_l22_soak.py

Each check is printed as ``[PASS]`` or ``[FAIL]``. The script exits
non-zero on any failure so the CI-style runner can gate on it.

Checks cover:

1. Migration 143 applied; table + indexes present.
2. ``brain_intraday_session_*`` settings visible.
3. Pure model: trending up/down, gap-and-go, gap-fade, reversal,
   compressed, range-bound, neutral.
4. ``compute_and_persist`` in shadow mode writes exactly one row for a
   synthetic ``bars_override`` input.
5. ``off`` mode is a no-op; ``authoritative`` raises ``RuntimeError``.
6. Coverage gate persists but returns ``None`` when below threshold.
7. Deterministic ``snapshot_id``; repeated writes are append-only.
8. ``intraday_session_summary`` frozen wire shape (keys + label set).
9. Additive-only: L.17-L.21 snapshot row counts unchanged around a
   full L.22 write cycle.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Callable, List

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

os.environ.setdefault("BRAIN_INTRADAY_SESSION_OPS_LOG_ENABLED", "false")

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
# Helpers
# ---------------------------------------------------------------------------


def _bars_linear(
    *,
    bars_count: int = 78,
    open_price: float = 100.0,
    slope_per_bar: float = 0.0,
    half_range: float = 0.05,
    volume: float = 1_000_000.0,
    start_minute: int = 9 * 60 + 30,
    bar_minutes: int = 5,
):
    from app.services.trading.intraday_session_model import IntradayBar

    bars: List = []
    px = open_price
    for i in range(bars_count):
        c_open = px
        c_close = px + slope_per_bar
        hi = max(c_open, c_close) + half_range
        lo = min(c_open, c_close) - half_range
        bars.append(
            IntradayBar(
                ts_minute=start_minute + i * bar_minutes,
                open=c_open,
                high=hi,
                low=lo,
                close=c_close,
                volume=volume,
            )
        )
        px = c_close
    return bars


def _override_range(bars, ts_start, ts_end, *, half_range):
    from app.services.trading.intraday_session_model import IntradayBar

    out = []
    for b in bars:
        if ts_start <= b.ts_minute < ts_end:
            mid = (b.high + b.low) / 2.0
            out.append(
                IntradayBar(
                    ts_minute=b.ts_minute,
                    open=b.open,
                    high=mid + half_range,
                    low=mid - half_range,
                    close=b.close,
                    volume=b.volume,
                )
            )
        else:
            out.append(b)
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_migration_applied() -> None:
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM schema_version "
            "WHERE version_id = '143_intraday_session_snapshot'"
        )).scalar_one()
        assert int(n) == 1, f"migration 143 not applied (n={n})"

        row = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name='trading_intraday_session_snapshots'"
        )).scalar_one()
        assert int(row) == 1, "table trading_intraday_session_snapshots missing"

        indexes = {
            r[0]
            for r in conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='trading_intraday_session_snapshots'"
            )).fetchall()
        }
        assert "ix_intraday_session_as_of" in indexes, indexes
        assert "ix_intraday_session_id" in indexes, indexes
        assert "ix_intraday_session_label" in indexes, indexes


def check_settings_visible() -> None:
    from app.config import settings

    assert hasattr(settings, "brain_intraday_session_mode")
    assert hasattr(settings, "brain_intraday_session_cron_hour")
    assert hasattr(settings, "brain_intraday_session_cron_minute")
    assert hasattr(settings, "brain_intraday_session_min_bars")
    assert hasattr(settings, "brain_intraday_session_or_minutes")
    assert hasattr(settings, "brain_intraday_session_power_minutes")
    assert hasattr(settings, "brain_intraday_session_gap_go")
    assert hasattr(settings, "brain_intraday_session_trending_close")


def check_pure_trending_up() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_TRENDING_UP,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(min_bars=70, trending_close_threshold=0.005)
    bars = _bars_linear(bars_count=78, slope_per_bar=0.012, half_range=0.02)
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=bars[0].open,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_TRENDING_UP, out.session_label
    assert out.session_numeric == +1


def check_pure_trending_down() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_TRENDING_DOWN,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(min_bars=70, trending_close_threshold=0.005)
    bars = _bars_linear(bars_count=78, slope_per_bar=-0.012, half_range=0.02)
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=bars[0].open,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_TRENDING_DOWN
    assert out.session_numeric == -1


def check_pure_gap_and_go() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_GAP_AND_GO,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(
        min_bars=70,
        or_range_high=0.002,
        trending_close_threshold=0.002,
        gap_magnitude_go=0.005,
    )
    bars = _bars_linear(
        bars_count=78,
        open_price=100.0,
        slope_per_bar=0.012,
        half_range=0.02,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=99.0,  # 1% gap up
            config=cfg,
        )
    )
    assert out.session_label == SESSION_GAP_AND_GO, out.session_label
    assert out.session_numeric == +3


def check_pure_gap_fade() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_GAP_FADE,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(
        min_bars=70,
        gap_magnitude_fade=0.005,
        reversal_close_threshold=0.0005,
        trending_close_threshold=1.0,  # prevent plain trending branch
    )
    bars = _bars_linear(
        bars_count=78,
        open_price=100.0,
        slope_per_bar=-0.012,
        half_range=0.02,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=99.0,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_GAP_FADE, out.session_label
    assert out.session_numeric == -3


def check_pure_reversal() -> None:
    from app.services.trading.intraday_session_model import (
        IntradayBar,
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_REVERSAL,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(
        min_bars=70,
        midday_compression_cut=0.4,
        reversal_close_threshold=0.001,
        trending_close_threshold=1.0,
        or_range_low=0.0,
    )
    bars = _bars_linear(bars_count=78, slope_per_bar=0.0, half_range=0.05)
    bars = _override_range(bars, 9 * 60 + 30, 10 * 60, half_range=0.8)
    bars = _override_range(bars, 12 * 60, 14 * 60, half_range=0.01)
    # Final bar: push close above OR midpoint
    last = bars[-1]
    bars[-1] = IntradayBar(
        ts_minute=last.ts_minute,
        open=last.open,
        high=last.open + 1.2,
        low=last.open - 0.01,
        close=last.open + 1.0,
        volume=last.volume,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=bars[0].open,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_REVERSAL
    assert out.session_numeric == +2


def check_pure_compressed() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_COMPRESSED,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(
        min_bars=70,
        or_range_low=0.01,
        or_range_high=0.02,
        trending_close_threshold=1.0,
        reversal_close_threshold=1.0,
    )
    bars = _bars_linear(bars_count=78, slope_per_bar=0.0, half_range=0.005)
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=100.0,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_COMPRESSED
    assert out.session_numeric == 0


def check_pure_range_bound() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionConfig,
        IntradaySessionInput,
        SESSION_RANGE_BOUND,
        compute_intraday_session,
    )

    cfg = IntradaySessionConfig(
        min_bars=70,
        or_range_low=0.0005,
        or_range_high=0.05,
        trending_close_threshold=1.0,
        reversal_close_threshold=1.0,
    )
    bars = _bars_linear(bars_count=78, slope_per_bar=0.0, half_range=0.1)
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=100.0,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_RANGE_BOUND


def check_pure_neutral_on_low_bars() -> None:
    from app.services.trading.intraday_session_model import (
        IntradaySessionInput,
        SESSION_NEUTRAL,
        compute_intraday_session,
    )

    bars = _bars_linear(bars_count=5, slope_per_bar=0.0)
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars)
    )
    assert out.session_label == SESSION_NEUTRAL


def check_shadow_persist_append_only() -> None:
    """Writing two rows for the same as_of_date should append (not dedupe),
    and both rows share the same snapshot_id."""
    from sqlalchemy import text

    from app.db import SessionLocal, engine
    from app.services.trading.intraday_session_service import (
        compute_and_persist,
        get_latest_snapshot,
    )

    as_of = date(2026, 4, 16)
    with engine.connect() as conn:
        conn.execute(text(
            "DELETE FROM trading_intraday_session_snapshots "
            "WHERE as_of_date = :d"
        ), {"d": as_of})
        conn.commit()

    bars = _bars_linear(bars_count=78, slope_per_bar=0.012, half_range=0.02)
    db = SessionLocal()
    try:
        row1 = compute_and_persist(
            db,
            as_of_date=as_of,
            mode_override="shadow",
            bars_override=bars,
            prev_close_override=bars[0].open,
        )
        assert row1 is not None
        row2 = compute_and_persist(
            db,
            as_of_date=as_of,
            mode_override="shadow",
            bars_override=bars,
            prev_close_override=bars[0].open,
        )
        assert row2 is not None
        assert row1.snapshot_id == row2.snapshot_id
        assert row1.pk_id != row2.pk_id

        latest = get_latest_snapshot(db)
        assert latest is not None
        assert latest["session_label"] in (
            "session_trending_up",
            "session_trending_down",
            "session_range_bound",
            "session_reversal",
            "session_gap_and_go",
            "session_gap_fade",
            "session_compressed",
            "session_neutral",
        )
    finally:
        db.close()


def check_off_mode_is_noop() -> None:
    from app.db import SessionLocal
    from app.services.trading.intraday_session_service import (
        compute_and_persist,
    )

    db = SessionLocal()
    try:
        row = compute_and_persist(
            db,
            as_of_date=date(2026, 4, 16),
            mode_override="off",
            bars_override=_bars_linear(bars_count=78, slope_per_bar=0.0),
            prev_close_override=100.0,
        )
        assert row is None, "off mode must return None"
    finally:
        db.close()


def check_authoritative_refuses() -> None:
    from app.db import SessionLocal
    from app.services.trading.intraday_session_service import (
        compute_and_persist,
    )

    db = SessionLocal()
    try:
        try:
            compute_and_persist(
                db,
                as_of_date=date(2026, 4, 16),
                mode_override="authoritative",
                bars_override=_bars_linear(
                    bars_count=78, slope_per_bar=0.0
                ),
                prev_close_override=100.0,
            )
            raise AssertionError("authoritative mode should raise RuntimeError")
        except RuntimeError:
            pass
    finally:
        db.close()


def check_coverage_gate_persists_but_returns_none() -> None:
    from sqlalchemy import text

    from app.db import SessionLocal, engine
    from app.services.trading.intraday_session_service import (
        compute_and_persist,
    )

    as_of = date(2026, 4, 14)
    with engine.connect() as conn:
        conn.execute(text(
            "DELETE FROM trading_intraday_session_snapshots "
            "WHERE as_of_date = :d"
        ), {"d": as_of})
        conn.commit()

    # Feed only 10 bars — below min_bars=40 default
    bars = _bars_linear(bars_count=10, slope_per_bar=0.0)
    db = SessionLocal()
    try:
        row = compute_and_persist(
            db,
            as_of_date=as_of,
            mode_override="shadow",
            bars_override=bars,
            prev_close_override=bars[0].open,
        )
        assert row is None, "below-coverage shadow must return None"

        # But a row should still be persisted for post-mortem.
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM trading_intraday_session_snapshots "
                "WHERE as_of_date = :d"
            ), {"d": as_of}).scalar_one()
            assert int(n) == 1, f"expected 1 below-coverage row, got {n}"
    finally:
        db.close()


def check_snapshot_id_deterministic() -> None:
    from app.services.trading.intraday_session_model import compute_snapshot_id

    a = compute_snapshot_id(date(2026, 4, 16))
    b = compute_snapshot_id(date(2026, 4, 16))
    c = compute_snapshot_id(date(2026, 4, 17))
    assert a == b
    assert a != c
    assert len(a) == 16


def check_summary_frozen_shape() -> None:
    from app.db import SessionLocal
    from app.services.trading.intraday_session_service import (
        intraday_session_summary,
    )

    db = SessionLocal()
    try:
        s = intraday_session_summary(db, lookback_days=7)
    finally:
        db.close()

    expected_keys = {
        "mode",
        "lookback_days",
        "snapshots_total",
        "by_session_label",
        "mean_or_range_pct",
        "mean_midday_compression_ratio",
        "mean_ph_range_pct",
        "mean_intraday_rv",
        "mean_session_range_pct",
        "mean_gap_open_pct_abs",
        "mean_coverage_score",
        "latest_snapshot",
    }
    assert set(s.keys()) == expected_keys, set(s.keys()) ^ expected_keys
    assert s["lookback_days"] == 7
    assert s["mode"] in ("off", "shadow", "compare", "authoritative")
    assert set(s["by_session_label"].keys()) == {
        "session_trending_up",
        "session_trending_down",
        "session_range_bound",
        "session_reversal",
        "session_gap_and_go",
        "session_gap_fade",
        "session_compressed",
        "session_neutral",
    }


def check_additive_only_l17_l21_counts_unchanged() -> None:
    """Write one L.22 row and verify L.17-L.21 tables are untouched."""
    from sqlalchemy import text

    from app.db import SessionLocal, engine
    from app.services.trading.intraday_session_service import (
        compute_and_persist,
    )

    tables = [
        "trading_macro_regime_snapshots",
        "trading_breadth_relstr_snapshots",
        "trading_cross_asset_snapshots",
        "trading_ticker_regime_snapshots",
        "trading_vol_dispersion_snapshots",
    ]

    with engine.connect() as conn:
        before = {
            t: int(conn.execute(text(
                f"SELECT COUNT(*) FROM {t}"
            )).scalar_one() or 0)
            for t in tables
        }

    db = SessionLocal()
    try:
        compute_and_persist(
            db,
            as_of_date=date(2026, 4, 15),
            mode_override="shadow",
            bars_override=_bars_linear(bars_count=78, slope_per_bar=0.0),
            prev_close_override=100.0,
        )
    finally:
        db.close()

    with engine.connect() as conn:
        after = {
            t: int(conn.execute(text(
                f"SELECT COUNT(*) FROM {t}"
            )).scalar_one() or 0)
            for t in tables
        }

    for t in tables:
        assert before[t] == after[t], (
            f"L.22 write mutated {t}: before={before[t]} after={after[t]}"
        )


def check_scan_status_additive_release() -> None:
    """After a full L.22 cycle, scan_status's release block should still
    be an empty dict on the happy path (no git SHA, no drift)."""
    from app.routers.trading_sub.ai import api_scan_status  # noqa: WPS433

    # Do not actually hit HTTP — just ensure the module is importable
    # and the function is callable. Full contract is validated by the
    # existing tests + live probe.
    assert callable(api_scan_status)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 72)
    print("Phase L.22 Docker soak — intraday session regime snapshot")
    print("Started:", datetime.utcnow().isoformat() + "Z")
    print("=" * 72)

    _run("1.  migration 143 applied + table + indexes", check_migration_applied)
    _run("2.  brain_intraday_session_* settings visible", check_settings_visible)

    _run("3.  pure: trending up → session_trending_up +1", check_pure_trending_up)
    _run("4.  pure: trending down → session_trending_down -1", check_pure_trending_down)
    _run("5.  pure: gap-and-go → session_gap_and_go +3", check_pure_gap_and_go)
    _run("6.  pure: gap-fade → session_gap_fade -3", check_pure_gap_fade)
    _run("7.  pure: reversal → session_reversal +2", check_pure_reversal)
    _run("8.  pure: compressed → session_compressed", check_pure_compressed)
    _run("9.  pure: wide range → session_range_bound", check_pure_range_bound)
    _run("10. pure: low bars → session_neutral", check_pure_neutral_on_low_bars)

    _run("11. shadow persist append-only (same snapshot_id)", check_shadow_persist_append_only)
    _run("12. off mode is a no-op (returns None)", check_off_mode_is_noop)
    _run("13. authoritative mode raises RuntimeError", check_authoritative_refuses)
    _run("14. coverage gate persists but returns None", check_coverage_gate_persists_but_returns_none)

    _run("15. compute_snapshot_id deterministic + date-sensitive", check_snapshot_id_deterministic)
    _run("16. intraday_session_summary frozen shape", check_summary_frozen_shape)
    _run("17. additive-only: L.17-L.21 row counts unchanged", check_additive_only_l17_l21_counts_unchanged)
    _run("18. api_scan_status still importable after L.22", check_scan_status_additive_release)

    print("=" * 72)
    print(f"Phase L.22 soak: {TOTAL - FAILS}/{TOTAL} checks passed")
    if FAILS == 0:
        print("[ALL GREEN]")
        return 0
    print(f"[{FAILS} FAILURES]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
