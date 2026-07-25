"""READ-ONLY rehearsal of the captured-paper selection admission against the LIVE DB.

2026-07-24: four admission bugs (empty-first-read, universe pin, correlation
pin, hub-sha race) were each discovered by a ~35-minute LIVE activation cycle.
This harness exercises the REAL ``read_snapshot()`` code path — hub probe,
viability-universe probe, fundamentals receipts, in-transaction admission and
per-row snapshot build — against the live database WITHOUT any cutover, host
mutation, or broker contact, so the next admission-layer defect surfaces in
seconds instead of a live fire.

Strictly read-only: the source's own read path opens REPEATABLE READ, READ
ONLY transactions; fundamentals are served by an in-process fake receipt (the
admission logic never depends on fundamentals content).  The source object is
constructed via ``object.__new__`` with ONLY the attributes the read path
touches, derived from the live variant rows themselves — this is a diagnostic
rehearsal, not an authority path, and it never publishes anything.

Usage:
    python -B -m scripts.rehearse_captured_admission [--iterations N] [--sleep S]
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone

UTC = timezone.utc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=15.0)
    parser.add_argument(
        "--database-url",
        default="postgresql://chili:chili@localhost:5433/chili",
    )
    parser.add_argument("--context-max-age", type=float, default=180.0)
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
    from app.services.trading.momentum_neural import (
        captured_paper_selection_source as source_module,
    )
    from app.services.trading.momentum_neural.captured_paper_initial_admission import (
        captured_paper_initial_variant_sha256,
    )
    from app.services.trading.momentum_neural.viability import (
        ViabilitySettingsProjection,
    )
    from app.services.yf_session import (
        FundamentalsProviderState,
        FundamentalsReceipt,
        FundamentalsReceiptOrigin,
        FundamentalsReceiptStatus,
    )

    engine = create_engine(args.database_url, pool_pre_ping=True)

    # --- live variant material: the SAME ids the producer writes ---
    from datetime import timedelta

    with Session(bind=engine) as db:
        # ONLY the variants the producer is writing NOW (the all-time distinct
        # set includes retired ids and would poison the route-completeness
        # requirement).
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        recent_variant_ids = sorted(
            int(row[0])
            for row in db.query(MomentumSymbolViability.variant_id)
            .filter(
                MomentumSymbolViability.scope == "symbol",
                MomentumSymbolViability.updated_at >= cutoff,
            )
            .distinct()
            .all()
        )
        variants = (
            db.query(MomentumStrategyVariant)
            .filter(MomentumStrategyVariant.id.in_(recent_variant_ids))
            .order_by(MomentumStrategyVariant.id.asc())
            .all()
        )
        source_to_target = {}
        for row in variants:
            if not bool(row.is_active):
                continue
            family = str(row.family or "")
            if str(row.variant_key or "") != family:
                continue
            source_to_target[int(row.id)] = (
                int(row.id),
                family,
                captured_paper_initial_variant_sha256(row),
            )
    print(
        f"[rehearse] live variants: {sorted(source_to_target)} "
        f"({len(source_to_target)} routes)",
        flush=True,
    )
    if not source_to_target:
        print("[rehearse] NO live variants — cannot rehearse", flush=True)
        return 2

    # --- real settings projection from the runtime config ---
    from app.config import settings as runtime_settings

    settings_projection = ViabilitySettingsProjection.from_runtime(runtime_settings)

    def fake_fundamentals(symbol: str) -> FundamentalsReceipt:
        return FundamentalsReceipt(
            symbol=symbol,
            status=FundamentalsReceiptStatus.FRESH_DATA,
            provider_state=FundamentalsProviderState.AVAILABLE,
            origin=FundamentalsReceiptOrigin.CACHE,
            observed_at=datetime.now(UTC),
            data={"short_name": symbol},
            cache_ttl_seconds=86_400.0,
        )

    from types import SimpleNamespace

    stub_sha = "0" * 64
    src = object.__new__(source_module.SqlAlchemyCapturedViabilitySnapshotSource)
    src._bind = engine
    src._source_to_target = source_to_target
    src._last_hub_snapshot_sha256 = None
    src.context_max_age_seconds = float(args.context_max_age)
    src.tenbeat_entry_tilt_weight = 0.0
    src.fundamentals_reader = fake_fundamentals
    src.wall_clock = lambda: datetime.now(UTC)
    src.settings_projection = settings_projection
    src.expected_account_id = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
    src.activation_generation = "rehearsal"
    # snapshot-build stubs (rehearsal-only provenance placeholders)
    src.selection_authority = SimpleNamespace(authority_sha256=stub_sha)
    src.policy_sha256 = stub_sha
    src.service_settings_projection_sha256 = stub_sha
    src.candidate_code_build_sha256 = stub_sha
    src._config_payload = settings_projection.to_dict()
    src._feature_flags_payload = {"rehearsal": True}
    src._code_payload = {"rehearsal": True}

    passes = 0
    reasons: dict[str, int] = {}
    for iteration in range(1, args.iterations + 1):
        started = time.monotonic()
        try:
            snapshots = src.read_snapshot()
            elapsed = time.monotonic() - started
            symbols = sorted({snap.symbol for snap in snapshots})
            passes += 1
            print(
                f"[rehearse] iter={iteration} PASS in {elapsed:.1f}s: "
                f"{len(snapshots)} snapshots across {len(symbols)} symbols "
                f"{symbols[:8]}",
                flush=True,
            )
        except source_module.CapturedPaperSelectionSourceUnavailable as exc:
            elapsed = time.monotonic() - started
            reasons[exc.reason] = reasons.get(exc.reason, 0) + 1
            print(
                f"[rehearse] iter={iteration} reject in {elapsed:.1f}s: "
                f"{exc.reason}",
                flush=True,
            )
        except Exception:
            print(f"[rehearse] iter={iteration} UNEXPECTED:", flush=True)
            traceback.print_exc()
            return 3
        # a successful read advances the dedupe marker; reset so every
        # iteration exercises the full path against the next hub tick
        src._last_hub_snapshot_sha256 = None
        if iteration < args.iterations:
            time.sleep(args.sleep)

    print(
        f"[rehearse] DONE: {passes}/{args.iterations} passes; rejects={reasons}",
        flush=True,
    )
    return 0 if passes > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
