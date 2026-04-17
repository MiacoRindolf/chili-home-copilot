"""Phase K Docker soak - divergence panel + ops health (shadow).

Verifies inside the running ``chili`` container that:

  1. Migration 137 applied (``trading_pattern_divergence_log`` exists).
  2. ``BRAIN_DIVERGENCE_SCORER_MODE`` and ``BRAIN_OPS_HEALTH_ENABLED``
     are visible on ``settings``.
  3. ``evaluate_pattern`` writes one row when forced to shadow and is
     a no-op when forced to off, across green / yellow / red synthetic
     bundles.
  4. ``evaluate_pattern`` and ``run_sweep`` refuse ``authoritative``
     mode with :class:`RuntimeError`.
  5. Determinism: same (pattern, as_of_key) yields identical
     ``divergence_id``; a subsequent sweep with the same key adds an
     append-only row (no silent overwrite).
  6. ``divergence_summary`` returns the frozen wire shape.
  7. ``build_health_snapshot`` returns the frozen ops-health wire
     shape with all 15 expected phase keys in order (Phase A-K).
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
from app.services.trading.divergence_model import (  # noqa: E402
    LAYER_BRACKET,
    LAYER_EXIT,
    LAYER_LEDGER,
    LAYER_SIZER,
    LAYER_VENUE,
    LayerSignal,
    compute_divergence_id,
)
from app.services.trading.divergence_service import (  # noqa: E402
    DivergenceInputBundle,
    divergence_summary,
    evaluate_pattern,
    run_sweep,
)
from app.services.trading.ops_health_service import (  # noqa: E402
    build_health_snapshot,
)

SOAK_PATTERN_ID_GREEN = 999_901
SOAK_PATTERN_ID_YELLOW = 999_902
SOAK_PATTERN_ID_RED = 999_903
SOAK_AS_OF_KEY = "2026-04-17"


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_k_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_k_soak] OK  : {msg}")


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_pattern_divergence_log "
        "WHERE scan_pattern_id IN (:a, :b, :c)"
    ), {
        "a": SOAK_PATTERN_ID_GREEN,
        "b": SOAK_PATTERN_ID_YELLOW,
        "c": SOAK_PATTERN_ID_RED,
    })
    db.commit()


def _check_schema_and_settings(db) -> None:
    row = db.execute(text(
        "SELECT to_regclass('public.trading_pattern_divergence_log')"
    )).scalar_one()
    _assert(
        row is not None,
        "trading_pattern_divergence_log exists",
    )

    row = db.execute(text(
        "SELECT version_id FROM schema_version "
        "WHERE version_id = '137_divergence_panel'"
    )).fetchone()
    _assert(
        row is not None,
        "migration 137_divergence_panel recorded",
    )

    _assert(
        hasattr(settings, "brain_divergence_scorer_mode"),
        "settings.brain_divergence_scorer_mode exists",
    )
    _assert(
        hasattr(settings, "brain_ops_health_enabled"),
        "settings.brain_ops_health_enabled exists",
    )


def _green_bundle() -> DivergenceInputBundle:
    return DivergenceInputBundle(
        scan_pattern_id=SOAK_PATTERN_ID_GREEN,
        pattern_name="soak_green",
        signals=[
            LayerSignal(layer=LAYER_LEDGER, severity="green", sample_size=5),
            LayerSignal(layer=LAYER_EXIT, severity="green", sample_size=5),
            LayerSignal(layer=LAYER_VENUE, severity="green", sample_size=5),
            LayerSignal(layer=LAYER_BRACKET, severity="green", sample_size=5),
            LayerSignal(layer=LAYER_SIZER, severity="green", sample_size=5),
        ],
    )


def _yellow_bundle() -> DivergenceInputBundle:
    return DivergenceInputBundle(
        scan_pattern_id=SOAK_PATTERN_ID_YELLOW,
        pattern_name="soak_yellow",
        signals=[
            LayerSignal(
                layer=LAYER_LEDGER, severity="yellow",
                reason_code="ledger_disagree", sample_size=3,
            ),
            LayerSignal(layer=LAYER_EXIT, severity="green", sample_size=3),
            LayerSignal(
                layer=LAYER_VENUE, severity="yellow",
                reason_code="delta_bps=45", sample_size=3,
            ),
            LayerSignal(layer=LAYER_BRACKET, severity="green", sample_size=3),
            LayerSignal(layer=LAYER_SIZER, severity="green", sample_size=3),
        ],
    )


def _red_bundle() -> DivergenceInputBundle:
    return DivergenceInputBundle(
        scan_pattern_id=SOAK_PATTERN_ID_RED,
        pattern_name="soak_red",
        signals=[
            LayerSignal(
                layer=LAYER_LEDGER, severity="red",
                reason_code="ledger_disagree", sample_size=8,
            ),
            LayerSignal(
                layer=LAYER_BRACKET, severity="red",
                reason_code="kind=orphan_stop", sample_size=4,
            ),
            LayerSignal(
                layer=LAYER_SIZER, severity="red",
                reason_code="divergence_bps=320", sample_size=4,
            ),
        ],
    )


def _check_divergence_scorer(db) -> None:
    # Force off.
    settings.brain_divergence_scorer_mode = "off"
    res = evaluate_pattern(
        db, bundle=_green_bundle(), as_of_key=SOAK_AS_OF_KEY,
    )
    _assert(res is None, "evaluate_pattern in mode=off is no-op")

    res_sweep_off = run_sweep(db, bundles=[_green_bundle()])
    _assert(
        res_sweep_off == [],
        "run_sweep in mode=off returns empty list",
    )

    settings.brain_divergence_scorer_mode = "shadow"
    res_green = evaluate_pattern(
        db, bundle=_green_bundle(), as_of_key=SOAK_AS_OF_KEY,
    )
    _assert(
        res_green is not None,
        "evaluate_pattern in mode=shadow writes a row (green)",
    )
    _assert(
        res_green.severity == "green",
        f"green severity observed ({res_green.severity})",
    )

    res_yellow = evaluate_pattern(
        db, bundle=_yellow_bundle(), as_of_key=SOAK_AS_OF_KEY,
    )
    _assert(res_yellow is not None, "evaluate_pattern writes yellow row")
    _assert(
        res_yellow.severity in ("yellow", "red"),
        f"yellow signals produce non-green severity "
        f"({res_yellow.severity})",
    )

    res_red = evaluate_pattern(
        db, bundle=_red_bundle(), as_of_key=SOAK_AS_OF_KEY,
    )
    _assert(res_red is not None, "evaluate_pattern writes red row")
    _assert(
        res_red.severity == "red",
        f"red signals produce red severity ({res_red.severity})",
    )

    count = db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_divergence_log
         WHERE scan_pattern_id IN (:a, :b, :c)
    """), {
        "a": SOAK_PATTERN_ID_GREEN,
        "b": SOAK_PATTERN_ID_YELLOW,
        "c": SOAK_PATTERN_ID_RED,
    }).scalar_one()
    _assert(
        int(count or 0) == 3,
        f"three divergence rows persisted (got {count})",
    )

    id1 = compute_divergence_id(
        scan_pattern_id=SOAK_PATTERN_ID_RED, as_of_key=SOAK_AS_OF_KEY,
    )
    id2 = compute_divergence_id(
        scan_pattern_id=SOAK_PATTERN_ID_RED, as_of_key=SOAK_AS_OF_KEY,
    )
    _assert(
        id1 == id2,
        "divergence_id deterministic for same (pattern, as_of_key)",
    )
    _assert(
        res_red.divergence_id == id1,
        "evaluate_pattern produces deterministic divergence_id",
    )

    # Append-only: a second sweep on the same pattern/as_of_key should
    # still succeed (write another row). Shadow is observational; no
    # dedupe in K.1.
    res_red_again = evaluate_pattern(
        db, bundle=_red_bundle(), as_of_key=SOAK_AS_OF_KEY,
    )
    _assert(
        res_red_again is not None,
        "evaluate_pattern is append-only (second write succeeds)",
    )
    count2 = db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_divergence_log
         WHERE scan_pattern_id = :pid
    """), {"pid": SOAK_PATTERN_ID_RED}).scalar_one()
    _assert(
        int(count2 or 0) == 2,
        f"append-only: red pattern has 2 rows (got {count2})",
    )

    # Authoritative refusal (evaluate_pattern).
    settings.brain_divergence_scorer_mode = "authoritative"
    try:
        evaluate_pattern(
            db, bundle=_green_bundle(), as_of_key=SOAK_AS_OF_KEY,
        )
        _assert(
            False,
            "evaluate_pattern refused authoritative (did not raise)",
        )
    except RuntimeError:
        _assert(True, "evaluate_pattern refused authoritative mode")

    # Authoritative refusal (run_sweep).
    try:
        run_sweep(db, bundles=[_green_bundle()])
        _assert(
            False,
            "run_sweep refused authoritative (did not raise)",
        )
    except RuntimeError:
        _assert(True, "run_sweep refused authoritative mode")

    settings.brain_divergence_scorer_mode = "shadow"


def _check_summary(db) -> None:
    summary = divergence_summary(db, lookback_days=14)
    expected_keys = {
        "mode", "lookback_days", "divergence_events_total",
        "by_severity", "patterns_red", "patterns_yellow",
        "mean_score", "layers_tracked", "latest_divergence",
    }
    _assert(
        set(summary.keys()) == expected_keys,
        f"divergence_summary frozen shape ({sorted(summary.keys())})",
    )
    _assert(
        set(summary["by_severity"].keys()) == {"green", "yellow", "red"},
        "divergence_summary.by_severity frozen shape",
    )
    _assert(
        summary["divergence_events_total"] >= 3,
        f"divergence_events_total >= 3 "
        f"(got {summary['divergence_events_total']})",
    )
    _assert(
        summary["patterns_red"] >= 1,
        f"patterns_red >= 1 (got {summary['patterns_red']})",
    )
    _assert(
        isinstance(summary["layers_tracked"], list)
        and len(summary["layers_tracked"]) == 5,
        "layers_tracked is 5-item list",
    )


def _check_ops_health(db) -> None:
    snap = build_health_snapshot(db, lookback_days=14)
    expected_keys = {
        "overall_severity", "lookback_days",
        "scheduler", "governance", "phases", "enabled",
    }
    _assert(
        set(snap.keys()) == expected_keys,
        f"ops_health snapshot frozen top-level shape "
        f"({sorted(snap.keys())})",
    )
    _assert(
        set(snap["scheduler"].keys()) == {"running", "job_count"},
        "ops_health.scheduler frozen shape",
    )
    _assert(
        set(snap["governance"].keys()) == {
            "kill_switch_engaged", "pending_approvals",
        },
        "ops_health.governance frozen shape",
    )

    expected_phase_keys = [
        "ledger", "exit_engine", "net_edge", "pit", "triple_barrier",
        "execution_cost", "venue_truth", "bracket_intent",
        "bracket_reconciliation", "position_sizer", "risk_dial",
        "capital_reweight", "drift_monitor", "recert_queue", "divergence",
    ]
    actual_phase_keys = [p["key"] for p in snap["phases"]]
    _assert(
        actual_phase_keys == expected_phase_keys,
        f"ops_health.phases has 15 keys in stable order "
        f"(got {actual_phase_keys})",
    )

    required_phase_fields = {
        "key", "present", "mode", "red_count", "yellow_count", "notes",
    }
    for p in snap["phases"]:
        _assert(
            set(p.keys()) == required_phase_fields,
            f"ops_health phase={p.get('key')} frozen fields "
            f"({sorted(p.keys())})",
        )

    divergence_phase = next(
        p for p in snap["phases"] if p["key"] == "divergence"
    )
    _assert(
        divergence_phase["present"] is True,
        "ops_health divergence phase marked present after soak writes",
    )
    _assert(
        divergence_phase["red_count"] >= 1,
        f"ops_health divergence red_count >= 1 "
        f"(got {divergence_phase['red_count']})",
    )

    _assert(
        snap["overall_severity"] in {"green", "yellow", "red"},
        f"overall_severity is a known value "
        f"({snap['overall_severity']})",
    )


def main() -> int:
    print("[phase_k_soak] starting Phase K soak check")
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_schema_and_settings(db)
        _check_divergence_scorer(db)
        _check_summary(db)
        _check_ops_health(db)
        print("[phase_k_soak] ALL CHECKS PASSED")
        return 0
    finally:
        try:
            _cleanup(db)
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
