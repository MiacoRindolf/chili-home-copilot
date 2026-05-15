"""Scheduler-side activation test for triple-barrier labeling
(Phase C of f-evidence-fidelity-architecture, 2026-05-14).

Verifies that the 4-hourly cron wrapper
:func:`run_triple_barrier_label_cycle` actually exercises
``label_snapshots`` end-to-end -- seeds MarketSnapshot rows old enough
to be picked up, fakes forward-bar fetches, runs the cycle once, and
asserts both DB rows and report shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import MarketSnapshot, TripleBarrierLabelRow
from app.services.trading.cron_jobs.triple_barrier_label import (
    DEFAULT_LIMIT,
    DEFAULT_MIN_LOOKBACK_DAYS,
    DEFAULT_SIDE,
    run_triple_barrier_label_cycle,
)


_REPORT_KEYS = {
    "mode",
    "requested",
    "written",
    "skipped_existing",
    "missing_data",
    "labels_tp",
    "labels_sl",
    "labels_timeout",
    "errors",
}


def _tp_ohlcv() -> list[dict[str, float]]:
    """Forward OHLCV (no date) that hits TP first (long, tp default 0.015).
    Entry 100.0 -> tp at 101.5; high 103 trips on bar 0."""
    return [
        {"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.5},
        {"open": 102.5, "high": 103.5, "low": 102.0, "close": 103.0},
    ]


def _sl_ohlcv() -> list[dict[str, float]]:
    """Forward OHLCV (no date) that hits SL first (long, sl default 0.010).
    Entry 100.0 -> sl at 99.0; low 98.5 trips on bar 0."""
    return [
        {"open": 100.0, "high": 100.5, "low": 98.5, "close": 99.0},
        {"open": 99.0, "high": 99.5, "low": 98.0, "close": 98.5},
    ]


def _stamp_dates(ohlcv: list[dict[str, float]], start: str | None) -> list[dict]:
    """Attach ISO dates strictly after ``start`` (the labeler discards
    bars with date <= from_date)."""
    from datetime import date as _date_t

    if not start:
        return list(ohlcv)
    base = _date_t.fromisoformat(str(start)[:10])
    out: list[dict] = []
    for i, bar in enumerate(ohlcv, start=1):
        stamped = dict(bar)
        stamped["date"] = (base + timedelta(days=i)).isoformat()
        out.append(stamped)
    return out


def _seed_snapshot(db, *, ticker: str, days_back: int, close: float = 100.0) -> int:
    snap = MarketSnapshot(
        ticker=ticker,
        snapshot_date=datetime.utcnow() - timedelta(days=days_back),
        close_price=close,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return int(snap.id)


@pytest.fixture
def fake_forward_bars(monkeypatch):
    """Map ticker -> forward-OHLCV list (no dates). The fake fetcher
    stamps dates strictly after the requested ``start`` so the labeler's
    bar-date filter accepts them. Patches the labeler's
    ``market_data.fetch_ohlcv`` reference so we don't hit the network."""
    by_ticker: dict[str, list[dict[str, float]]] = {}

    def _fetch(ticker, interval="1d", *, start=None, end=None, **_):  # noqa: ARG001
        raw = by_ticker.get(str(ticker).upper(), [])
        return _stamp_dates(raw, start)

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv",
        _fetch,
        raising=True,
    )
    return by_ticker


def test_defaults_match_brief():
    """Brief D1 wires label_snapshots(limit=500, side='long',
    min_lookback_days=10). Lock those in so a future renaming doesn't
    silently change the cadence semantics."""
    assert DEFAULT_LIMIT == 500
    assert DEFAULT_SIDE == "long"
    assert DEFAULT_MIN_LOOKBACK_DAYS == 10


def test_cycle_writes_rows_and_returns_report(db, fake_forward_bars):
    fake_forward_bars["AAA"] = _tp_ohlcv()
    fake_forward_bars["BBB"] = _sl_ohlcv()
    # snapshots old enough to clear the min_lookback_days gate
    _seed_snapshot(db, ticker="AAA", days_back=15)
    _seed_snapshot(db, ticker="BBB", days_back=15)

    report = run_triple_barrier_label_cycle(db)

    assert set(report.keys()) == _REPORT_KEYS
    assert report["mode"] == "shadow"
    assert report["requested"] == 2
    assert report["written"] == 2
    assert report["labels_tp"] == 1
    assert report["labels_sl"] == 1
    assert report["labels_timeout"] == 0
    assert report["missing_data"] == 0
    assert report["errors"] == 0

    rows = db.query(TripleBarrierLabelRow).order_by(TripleBarrierLabelRow.ticker).all()
    assert [r.ticker for r in rows] == ["AAA", "BBB"]
    assert {r.barrier_hit for r in rows} == {"tp", "sl"}
    assert {r.mode for r in rows} == {"shadow"}
    assert {r.side for r in rows} == {"long"}


def test_cycle_skips_too_recent_snapshots(db, fake_forward_bars):
    """min_lookback_days=10 must exclude fresh snapshots even when
    forward bars are available -- otherwise barriers are evaluated
    against bars that don't exist yet in prod."""
    fake_forward_bars["FRESH"] = _tp_ohlcv()
    _seed_snapshot(db, ticker="FRESH", days_back=2)  # too recent

    report = run_triple_barrier_label_cycle(db)

    assert report["requested"] == 0
    assert report["written"] == 0
    assert db.query(TripleBarrierLabelRow).count() == 0


def test_cycle_is_idempotent(db, fake_forward_bars):
    """Re-running over the same snapshot produces zero new rows; the
    second cycle's report must show skipped_existing>0 instead."""
    fake_forward_bars["IDEM"] = _tp_ohlcv()
    _seed_snapshot(db, ticker="IDEM", days_back=20)

    first = run_triple_barrier_label_cycle(db)
    second = run_triple_barrier_label_cycle(db)

    assert first["written"] == 1
    assert second["written"] == 0
    assert second["skipped_existing"] == 1
    assert db.query(TripleBarrierLabelRow).filter_by(ticker="IDEM").count() == 1


def test_cycle_off_mode_writes_nothing(db, fake_forward_bars, monkeypatch):
    """Hard constraint: when brain_triple_barrier_mode is 'off', the
    labeler must not insert. The cron wrapper just forwards the report,
    so its written count must be 0."""
    monkeypatch.setattr(
        "app.services.trading.triple_barrier_labeler.settings.brain_triple_barrier_mode",
        "off",
        raising=False,
    )
    fake_forward_bars["OFFM"] = _tp_ohlcv()
    _seed_snapshot(db, ticker="OFFM", days_back=15)

    report = run_triple_barrier_label_cycle(db)

    assert report["mode"] == "off"
    assert report["written"] == 0
    assert db.query(TripleBarrierLabelRow).count() == 0


def test_cycle_passes_limit_through(db, fake_forward_bars):
    """``limit`` argument is honored -- only ``limit`` rows requested."""
    for i in range(5):
        fake_forward_bars[f"LIM{i}"] = _tp_ohlcv()
        _seed_snapshot(db, ticker=f"LIM{i}", days_back=15 + i)

    report = run_triple_barrier_label_cycle(db, limit=2)

    assert report["requested"] == 2
    assert report["written"] == 2
    assert db.query(TripleBarrierLabelRow).count() == 2


def test_scheduler_job_registered_in_module():
    """The 4-hourly triple_barrier_label_cycle wiring lives in
    trading_scheduler.py with the brief's mandatory overlap-prevention
    contract (max_instances=1, coalesce=True). We verify the call-site
    statically rather than booting APScheduler, which would pull in
    100+ unrelated jobs and require a full DB setup.
    """
    import ast
    from pathlib import Path

    src = Path("app/services/trading_scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Find the add_job(...) call whose id kwarg == 'triple_barrier_label_cycle'.
    found: ast.Call | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "add_job"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        ident = kwargs.get("id")
        if isinstance(ident, ast.Constant) and ident.value == "triple_barrier_label_cycle":
            found = node
            break

    assert found is not None, (
        "trading_scheduler.py must register a job with id='triple_barrier_label_cycle'"
    )

    kwargs = {kw.arg: kw.value for kw in found.keywords if kw.arg is not None}

    mi = kwargs.get("max_instances")
    assert isinstance(mi, ast.Constant) and mi.value == 1, (
        "triple_barrier_label_cycle must have max_instances=1 (overlap guard)"
    )

    co = kwargs.get("coalesce")
    assert isinstance(co, ast.Constant) and co.value is True, (
        "triple_barrier_label_cycle must have coalesce=True (drop missed runs)"
    )

    trig = kwargs.get("trigger")
    assert trig is not None, "trigger kwarg required"
    # IntervalTrigger(hours=4) — match the brief's cadence.
    assert isinstance(trig, ast.Call) and isinstance(trig.func, ast.Name) and trig.func.id == "IntervalTrigger", (
        "trigger must be IntervalTrigger(hours=4)"
    )
    trig_kwargs = {kw.arg: kw.value for kw in trig.keywords if kw.arg is not None}
    hours = trig_kwargs.get("hours")
    assert isinstance(hours, ast.Constant) and hours.value == 4, (
        "IntervalTrigger must use hours=4 (Phase C cadence)"
    )
