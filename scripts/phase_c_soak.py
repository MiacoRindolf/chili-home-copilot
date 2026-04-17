"""Phase C soak: verify PIT audit in shadow mode.

Runs inside the chili container:
    docker compose exec -T chili python scripts/phase_c_soak.py
"""
from __future__ import annotations

import json
import logging
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from app.db import SessionLocal
from app.models.trading import PitAuditLog, ScanPattern, UniverseSnapshot
from app.services.trading import pit_audit, universe_snapshot
from app.config import settings


def _mk_pattern(db, *, name, conditions, lifecycle_stage="validated", active=True):
    p = ScanPattern(
        name=name,
        description=f"soak {name}",
        rules_json=json.dumps({"conditions": conditions}),
        origin="mined",
        active=active,
        lifecycle_stage=lifecycle_stage,
        confidence=0.6,
        evidence_count=25,
    )
    db.add(p)
    db.flush()
    return p


def main() -> int:
    print(f"[soak] BRAIN_PIT_AUDIT_MODE={settings.brain_pit_audit_mode}")
    print(f"[soak] BRAIN_PIT_AUDIT_OPS_LOG_ENABLED={settings.brain_pit_audit_ops_log_enabled}")

    db = SessionLocal()
    try:
        audits_before = db.query(PitAuditLog).count()
        universe_before = db.query(UniverseSnapshot).count()
        print(f"[soak] audit rows before: {audits_before}")
        print(f"[soak] universe rows before: {universe_before}")

        clean = _mk_pattern(db, name="SOAK-PIT-clean",
                            conditions=[{"indicator": "rsi_14", "op": "<", "value": 30},
                                        {"indicator": "macd_histogram", "op": ">", "value": 0}])
        bad = _mk_pattern(db, name="SOAK-PIT-lookahead",
                          conditions=[{"indicator": "rsi_14", "op": "<", "value": 30},
                                      {"indicator": "future_return_5d", "op": ">", "value": 0.02}])
        unk = _mk_pattern(db, name="SOAK-PIT-unknown",
                          conditions=[{"indicator": "my_secret_feature", "op": ">", "value": 0.5}])
        db.commit()

        results = pit_audit.audit_and_record_active(db)
        db.commit()
        print(f"[soak] audited {len(results)} patterns")
        for r in results:
            if r.name and r.name.startswith("SOAK-PIT-"):
                print(f"  - {r.name}: pit={len(r.pit_fields)} non_pit={len(r.non_pit_fields)} unknown={len(r.unknown_fields)} agree={r.agree_bool}")

        audits_after = db.query(PitAuditLog).count()
        print(f"[soak] audit rows after: {audits_after} (delta={audits_after - audits_before})")

        universe_snapshot.record_snapshot(
            db,
            as_of_date=date.today(),
            ticker="SOAK-AAPL",
            asset_class="equity",
            status="active",
            primary_exchange="NASDAQ",
            source="phase_c_soak",
        )
        db.commit()
        universe_after = db.query(UniverseSnapshot).count()
        print(f"[soak] universe rows after: {universe_after} (delta={universe_after - universe_before})")

        summary = pit_audit.audit_summary(db, lookback_hours=1)
        print("[soak] summary:")
        print(f"  mode={summary['mode']}")
        print(f"  audits_total={summary['audits_total']}")
        print(f"  patterns_audited={summary['patterns_audited']}")
        print(f"  patterns_clean={summary['patterns_clean']}")
        print(f"  patterns_violating={summary['patterns_violating']}")
        print(f"  forbidden_hits_by_field={summary['forbidden_hits_by_field']}")
        print(f"  unknown_hits_by_field={summary['unknown_hits_by_field']}")

        rc = 0
        if audits_after < audits_before + 3:
            print("[soak] FAIL: expected >=3 audit rows for the 3 synthetic patterns")
            rc = 3
        if summary["patterns_violating"] < 2:
            print("[soak] FAIL: expected >=2 violating patterns (lookahead + unknown)")
            rc = 3
        if universe_after != universe_before + 1:
            print("[soak] FAIL: universe snapshot not written (or duplicated)")
            rc = 3
        if rc == 0:
            print("[soak] OK")
        return rc
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
