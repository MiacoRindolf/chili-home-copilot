"""Drain-loop backport para sa _prune_operational_time_log (2026-08-17 audit).

Ang 11GB/29.8M na momentum_viability_history ay resulta ng isang-batch-kada-
araw na prune (50k/araw) laban sa ~508k/araw na ingestion — ang parehong bug
na naayos sa _prune_exit_parity_log pero hindi na-backport. Ang loop ay
dapat (a) maubos ang buong eligible set sa isang sweep kapag may cap,
(b) huminto sa cap, at (c) manatiling byte-identical na isang-batch kapag
walang cap (ang tatlong ibang table na gumagamit ng helper)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.services.trading.data_retention import _prune_operational_time_log


def _seed_old_rows(db, n: int) -> None:
    old = datetime.utcnow() - timedelta(days=60)
    db.execute(text(
        "INSERT INTO momentum_viability_history "
        "(symbol, variant_id, observed_at, live_eligible) "
        "SELECT 'DRAIN', 1, :old - make_interval(secs => g), true "
        "FROM generate_series(1, :n) g"
    ), {"old": old, "n": n})
    db.commit()


def _count(db) -> int:
    return int(db.execute(text(
        "SELECT count(*) FROM momentum_viability_history WHERE symbol='DRAIN'"
    )).scalar())


def test_drain_loop_clears_backlog_in_one_sweep(db):
    _seed_old_rows(db, 2500)
    out = _prune_operational_time_log(
        db, table="momentum_viability_history", ts_col="observed_at",
        retain_days=35, batch_size=1000, dry_run=False,
        max_rows_per_sweep=10_000,
    )
    db.commit()
    assert out["deleted"] == 2500
    assert out["batches"] >= 3, "dapat maramihang batch sa isang sweep"
    assert _count(db) == 0


def test_sweep_cap_bounds_the_drain(db):
    _seed_old_rows(db, 2500)
    out = _prune_operational_time_log(
        db, table="momentum_viability_history", ts_col="observed_at",
        retain_days=35, batch_size=1000, dry_run=False,
        max_rows_per_sweep=2000,
    )
    db.commit()
    assert out["deleted"] == 2000
    assert _count(db) == 500


def test_no_cap_is_legacy_single_batch(db):
    """Walang cap ⇒ ang orihinal na isang-batch na gawi (ang 3 ibang table)."""
    _seed_old_rows(db, 2500)
    out = _prune_operational_time_log(
        db, table="momentum_viability_history", ts_col="observed_at",
        retain_days=35, batch_size=1000, dry_run=False,
    )
    db.commit()
    assert out["deleted"] == 1000
    assert out["batches"] == 1
    assert _count(db) == 1500


def test_dry_run_deletes_nothing(db):
    _seed_old_rows(db, 1500)
    out = _prune_operational_time_log(
        db, table="momentum_viability_history", ts_col="observed_at",
        retain_days=35, batch_size=1000, dry_run=True,
        max_rows_per_sweep=10_000,
    )
    db.commit()
    assert out["deleted"] == 0
    assert _count(db) == 1500
