"""Backfill HMM regime_snapshot rows and tag trading_snapshots (Q1.T2).

Viterbi decode over ~10 years of regime features, persist to ``regime_snapshot``,
and set ``trading_snapshots.regime`` / ``regime_posterior`` where ``bar_start_at`` matches.

Usage (repo root, conda ``chili-env``)::

    conda run -n chili-env python scripts/backfill_regime.py --dry-run
    conda run -n chili-env python scripts/backfill_regime.py --commit

Dry-run (default): builds features, fits model, logs row counts — **no DB writes**
(session rolled back on exit). The backfill script does **not** gate on
``chili_regime_classifier_enabled`` (operators may rehearse on a clone); enable the flag
for live weekly retrains and snapshot auto-tagging in the app.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trading import MarketSnapshot, RegimeSnapshot  # noqa: E402
from app.services.trading.regime_classifier import (  # noqa: E402
    FEATURE_NAMES,
    assert_regime_posterior_row_consistent,
    build_regime_features,
    fit_regime_model,
    load_latest_regime_artifact,
    regime_and_posterior_for_sequence,
    relabel_by_mean_return,
    save_regime_artifact,
)

logger = logging.getLogger("backfill_regime")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Backfill regime_snapshot + trading_snapshots tags.")
    ap.add_argument("--commit", action="store_true", help="Persist to DB (default dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Explicit dry-run.")
    ap.add_argument("--years", type=int, default=10, help="Feature/decode horizon (default 10).")
    args = ap.parse_args()
    if args.commit and args.dry_run:
        logger.error("Pass only one of --commit or --dry-run.")
        return 1
    do_commit = bool(args.commit)

    now = datetime.utcnow()
    start = now - timedelta(days=int(args.years) * 366 + 400)

    db = SessionLocal()
    try:
        feat = build_regime_features(db, start, now, log_missing_yield=not do_commit)
        if feat.empty or len(feat) < 200:
            logger.error("Insufficient feature rows: %s", len(feat))
            return 1

        from pandas.tseries.offsets import BDay

        import pandas as pd

        train_end = pd.Timestamp.now("UTC").replace(tzinfo=None).normalize() - BDay(21)
        train_start = train_end - pd.DateOffset(years=5)
        feat_train = feat.loc[train_start : train_end]
        if feat_train.empty or len(feat_train) < 200:
            logger.error("Insufficient training window rows: %s", len(feat_train))
            return 1

        rs = int(getattr(settings, "chili_regime_classifier_random_state", 42) or 42)
        n_iter = int(getattr(settings, "chili_regime_classifier_n_iter", 200) or 200)
        warm = None
        art = load_latest_regime_artifact()
        if art:
            warm = art.get("model")

        model, ver = fit_regime_model(
            feat_train,
            n_iter=n_iter,
            random_state=rs,
            warm_start_model=warm,
            train_start=train_start.to_pydatetime(),
            train_end=train_end.to_pydatetime(),
        )
        label_map = relabel_by_mean_return(model)
        if do_commit:
            save_regime_artifact(model, dict(label_map), ver)

        X = feat[list(FEATURE_NAMES)].values.astype(float)
        _, states = model.decode(X)
        decoded_labels = [label_map[int(states[i])] for i in range(len(feat))]
        regime_dist = Counter(decoded_labels)
        logger.info(
            "regime_distribution (Viterbi path, n=%s): bull=%s chop=%s bear=%s",
            len(decoded_labels),
            regime_dist.get("bull", 0),
            regime_dist.get("chop", 0),
            regime_dist.get("bear", 0),
        )

        regime_list, post_list = regime_and_posterior_for_sequence(model, X, label_map)
        regime_dist_m = Counter(regime_list)
        logger.info(
            "regime_distribution (marginal argmax, n=%s): bull=%s chop=%s bear=%s",
            len(regime_list),
            regime_dist_m.get("bull", 0),
            regime_dist_m.get("chop", 0),
            regime_dist_m.get("bear", 0),
        )

        sanity_idx = list(range(min(10, len(X))))
        if len(X) > 10:
            sanity_idx.append(len(X) - 1)
        try:
            for j in sanity_idx:
                assert_regime_posterior_row_consistent(regime_list[j], post_list[j])
        except AssertionError as e:
            logger.error("regime/posterior sanity check failed after fit: %s", e)
            return 1

        n_snap = 0
        n_tag = 0
        n_would_tag = 0
        for i, ts in enumerate(feat.index):
            lab = regime_list[i]
            post = post_list[i]
            xrow = X[i]
            feat_row = {k: float(xrow[j]) for j, k in enumerate(FEATURE_NAMES)}
            ts_db = ts.to_pydatetime()
            if ts_db.tzinfo:
                ts_db = ts_db.replace(tzinfo=None)
            n_would_tag += db.query(MarketSnapshot).filter(MarketSnapshot.bar_start_at == ts_db).count()
            if do_commit:
                prev = db.query(RegimeSnapshot).filter(RegimeSnapshot.as_of == ts_db).first()
                if prev is None:
                    db.add(
                        RegimeSnapshot(
                            as_of=ts_db,
                            regime=lab,
                            posterior=post,
                            features=feat_row,
                            model_version=ver,
                        )
                    )
                else:
                    prev.regime = lab
                    prev.posterior = post
                    prev.features = feat_row
                    prev.model_version = ver
                n_snap += 1

                q = db.query(MarketSnapshot).filter(MarketSnapshot.bar_start_at == ts_db)
                for snap in q.all():
                    snap.regime = lab
                    snap.regime_posterior = post
                    n_tag += 1

        if do_commit:
            db.commit()

        logger.info("--- regime backfill summary ---")
        logger.info("feature_rows_evaluated=%s train_rows=%s model_version=%s", len(feat), len(feat_train), ver)
        if do_commit:
            logger.info("regime_snapshot_rows_written=%s trading_snapshots_tagged=%s", n_snap, n_tag)
        else:
            logger.info(
                "would_write_regime_snapshot_rows=%s would_tag_trading_snapshots=%s",
                len(feat),
                n_would_tag,
            )
        logger.info("commit=%s", do_commit)
        return 0
    finally:
        if not do_commit:
            try:
                db.rollback()
            except Exception:
                pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
