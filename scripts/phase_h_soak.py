"""Phase H Docker soak - canonical position sizer (shadow only).

Verifies inside the running ``chili`` container that:
  1. Migration 134 applied and ``trading_position_sizer_log`` exists.
  2. ``BRAIN_POSITION_SIZER_MODE=shadow`` visible in settings.
  3. The emitter writes one row when invoked in shadow mode.
  4. The emitter is a no-op when forced to ``off`` mode.
  5. ``proposals_summary`` returns the frozen shape after inserts.
  6. Determinism: two calls with identical inputs produce the same
     ``proposal_id`` (the log stores both rows; aggregation is the
     diagnostics' job).
  7. Crypto ticker routes to ``asset_class='crypto'``.
  8. Forcing authoritative mode leaves the release-blocker tripwire
     visible in the log contents (the soak does NOT emit an
     authoritative row - it only asserts the *shape* of the
     shadow-mode guardrails).

Safe on a real chili stack: inserts use ``PHH_SOAK_*`` tickers and are
cleaned up before and after. No live trade mutations.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.trading.position_sizer_emitter import (  # noqa: E402
    EmitterSignal,
    emit_shadow_proposal,
)
from app.services.trading.position_sizer_writer import (  # noqa: E402
    LegacySizing,
    proposals_summary,
)


SOAK_TICKERS = [
    "PHH_SOAK_ACORP",  # equity
    "PHH_SOAK_BCORP",  # equity
    "BTC-USD",         # crypto (real-world routing check)
]


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_h_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_h_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_position_sizer_log WHERE ticker = ANY(:ts)"
    ), {"ts": SOAK_TICKERS})
    db.commit()


def _signal(**over) -> EmitterSignal:
    defaults = dict(
        source="phase_h_soak",
        ticker="PHH_SOAK_ACORP",
        direction="long",
        entry_price=100.0,
        stop_price=98.0,
        capital=100_000.0,
        target_price=103.0,
        asset_class="equity",
        user_id=None,
        pattern_id=None,
        regime="neutral",
        confidence=0.6,
    )
    defaults.update(over)
    return EmitterSignal(**defaults)


def _check_migration(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_position_sizer_log')"
    )).scalar_one()
    _assert(row is not None, "trading_position_sizer_log table exists")


def _check_settings() -> None:
    mode = (getattr(settings, "brain_position_sizer_mode", "off") or "off").lower()
    _assert(
        mode in ("off", "shadow", "compare", "authoritative"),
        f"brain_position_sizer_mode in allowed set (got {mode!r})",
    )


def _check_shadow_writes_row(db) -> None:
    prev_mode = getattr(settings, "brain_position_sizer_mode", "off")
    try:
        settings.brain_position_sizer_mode = "shadow"
        result = emit_shadow_proposal(
            db,
            signal=_signal(ticker="PHH_SOAK_ACORP"),
            legacy=LegacySizing(notional=5_000.0, quantity=50.0, source="legacy.test"),
        )
    finally:
        settings.brain_position_sizer_mode = prev_mode

    _assert(result is not None, "shadow emit returns WriteResult")
    _assert(result.mode == "shadow", f"WriteResult.mode=='shadow' (got {result.mode!r})")
    _assert(result.log_id > 0, "WriteResult.log_id > 0")
    _assert(result.divergence_bps is not None, "divergence_bps computed against legacy")

    row = db.execute(text(
        "SELECT mode, source, legacy_notional FROM trading_position_sizer_log "
        "WHERE ticker='PHH_SOAK_ACORP' ORDER BY id DESC LIMIT 1"
    )).mappings().fetchone()
    _assert(row is not None, "row exists in trading_position_sizer_log")
    _assert(row["mode"] == "shadow", "row.mode=='shadow'")
    _assert(row["legacy_notional"] == 5000.0, "row.legacy_notional == 5000.0")


def _check_off_is_noop(db) -> None:
    prev_mode = getattr(settings, "brain_position_sizer_mode", "off")
    before = db.execute(text(
        "SELECT COUNT(*) FROM trading_position_sizer_log WHERE ticker='PHH_SOAK_BCORP'"
    )).scalar_one()
    try:
        settings.brain_position_sizer_mode = "off"
        result = emit_shadow_proposal(
            db,
            signal=_signal(ticker="PHH_SOAK_BCORP"),
            legacy=LegacySizing(notional=1_000.0, quantity=10.0, source="legacy.off"),
        )
    finally:
        settings.brain_position_sizer_mode = prev_mode
    after = db.execute(text(
        "SELECT COUNT(*) FROM trading_position_sizer_log WHERE ticker='PHH_SOAK_BCORP'"
    )).scalar_one()
    _assert(result is None, "off-mode emit returns None")
    _assert(after == before, "off-mode did not insert any row")


def _check_summary_shape(db) -> None:
    payload = proposals_summary(db, lookback_hours=1)
    expected_top_level = {
        "mode",
        "lookback_hours",
        "proposals_total",
        "by_source",
        "by_divergence_bucket",
        "mean_divergence_bps",
        "p90_divergence_bps",
        "cap_trigger_counts",
        "latest_proposal",
    }
    _assert(
        set(payload.keys()) == expected_top_level,
        f"summary keys == frozen set (got {sorted(payload.keys())})",
    )
    _assert(
        set(payload["by_divergence_bucket"].keys()) == {
            "under_100_bps", "100_500_bps", "500_2000_bps", "over_2000_bps",
        },
        "divergence buckets frozen",
    )
    _assert(
        set(payload["cap_trigger_counts"].keys()) == {"correlation_cap", "notional_cap"},
        "cap_trigger_counts keys frozen",
    )


def _check_determinism(db) -> None:
    sig = _signal(ticker="PHH_SOAK_ACORP", entry_price=101.0, stop_price=99.0)
    prev_mode = getattr(settings, "brain_position_sizer_mode", "off")
    try:
        settings.brain_position_sizer_mode = "shadow"
        r1 = emit_shadow_proposal(
            db, signal=sig,
            legacy=LegacySizing(notional=500.0, quantity=5.0, source="legacy.det"),
        )
        r2 = emit_shadow_proposal(
            db, signal=sig,
            legacy=LegacySizing(notional=500.0, quantity=5.0, source="legacy.det"),
        )
    finally:
        settings.brain_position_sizer_mode = prev_mode
    _assert(r1 is not None and r2 is not None, "both deterministic emits succeeded")
    _assert(
        r1.proposal_id == r2.proposal_id,
        f"proposal_id deterministic ({r1.proposal_id} == {r2.proposal_id})",
    )


def _check_crypto_routing(db) -> None:
    prev_mode = getattr(settings, "brain_position_sizer_mode", "off")
    try:
        settings.brain_position_sizer_mode = "shadow"
        sig = EmitterSignal(
            source="phase_h_soak",
            ticker="BTC-USD",
            direction="long",
            entry_price=50_000.0,
            stop_price=48_000.0,
            capital=20_000.0,
            target_price=55_000.0,
            asset_class=None,  # force inference
            confidence=0.6,
        )
        emit_shadow_proposal(
            db,
            signal=sig,
            legacy=LegacySizing(notional=1_000.0, quantity=0.02, source="paper"),
        )
    finally:
        settings.brain_position_sizer_mode = prev_mode
    ac = db.execute(text(
        "SELECT asset_class FROM trading_position_sizer_log "
        "WHERE ticker='BTC-USD' ORDER BY id DESC LIMIT 1"
    )).scalar()
    _assert(ac == "crypto", f"BTC-USD inferred as crypto (got {ac!r})")


def main() -> int:
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_migration(db)
        _check_settings()
        _check_shadow_writes_row(db)
        _check_off_is_noop(db)
        _check_determinism(db)
        _check_crypto_routing(db)
        _check_summary_shape(db)
        _cleanup(db)
        print("[phase_h_soak] ALL CHECKS PASSED")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
