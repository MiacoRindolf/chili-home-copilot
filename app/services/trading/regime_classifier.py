"""Q1.T2: 3-state Gaussian HMM regime labels (bull / chop / bear) for macro features.

Feature vector (point-in-time, no look-ahead):
  0. SPY daily log return
  1. 21-day realized volatility of SPY (annualized from daily log returns)
  2. 126-day SPY momentum: log(close_t / close_{t-126})
  3. VIX spot (``^VIX`` close; macro table VIX overrides when present)
  4. Yield slope: ``trading_macro_regime_snapshots.yield_curve_slope_proxy`` (not FRED DGS10−DGS2)

See docs/ROADMAP_DEVIATION_003.md for the yield-proxy deviation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pickle
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
from sqlalchemy import Date, cast, func
from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)

FEATURE_NAMES = (
    "spy_log_return",
    "spy_realized_vol_21",
    "spy_mom_126",
    "vix",
    "yield_curve_slope",
)
FEATURE_SPEC_V1 = "v1|" + "|".join(FEATURE_NAMES)
FEATURE_IDX_SPY_RETURN = 0

REGIME_ORDER = ("bull", "chop", "bear")

_REPO_ROOT = Path(__file__).resolve().parents[3]


def regime_models_dir() -> Path:
    d = _REPO_ROOT / "regime_models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def compute_model_version_hash(
    *,
    train_start: datetime,
    train_end: datetime,
    feature_spec: str,
    random_state: int,
) -> str:
    import hmmlearn

    payload = {
        "train_start": pd.Timestamp(train_start).isoformat(),
        "train_end": pd.Timestamp(train_end).isoformat(),
        "feature_spec": feature_spec,
        "hmmlearn": hmmlearn.__version__,
        "random_state": int(random_state),
    }
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{h}"


def _normalize_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_macro_yield_vix_map(
    db: Session, start_d: date, end_d: date
) -> dict[date, tuple[float | None, float | None]]:
    from ...models.trading import MacroRegimeSnapshot

    rows = (
        db.query(
            MacroRegimeSnapshot.as_of_date,
            MacroRegimeSnapshot.yield_curve_slope_proxy,
            MacroRegimeSnapshot.vix,
        )
        .filter(
            MacroRegimeSnapshot.as_of_date >= start_d,
            MacroRegimeSnapshot.as_of_date <= end_d,
        )
        .all()
    )
    out: dict[date, tuple[float | None, float | None]] = {}
    for ad, yld, vx in rows:
        if ad is None:
            continue
        d = ad if isinstance(ad, date) else ad.date()
        out[d] = (yld, vx)
    return out


def build_regime_features(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    log_missing_yield: bool = True,
) -> pd.DataFrame:
    """Return rows indexed by bar ``as_of`` (UTC-naive) with the five numeric features.

    Rows with any missing feature are dropped (point-in-time strict).
    """
    from .market_data import fetch_ohlcv_df

    start_n = _normalize_utc(start)
    end_n = _normalize_utc(end)
    buf_start = start_n - timedelta(days=400)
    start_s = buf_start.date().isoformat()
    end_s = (end_n + timedelta(days=5)).date().isoformat()

    spy = fetch_ohlcv_df("SPY", interval="1d", start=start_s, end=end_s)
    vix = fetch_ohlcv_df("^VIX", interval="1d", start=start_s, end=end_s)
    if spy is None or spy.empty or "Close" not in spy.columns:
        logger.warning("[regime_classifier] SPY OHLCV empty for feature build")
        return pd.DataFrame(columns=list(FEATURE_NAMES))

    spy = spy.sort_index()
    close = spy["Close"].astype(float)
    log_ret = np.log(close / close.shift(1))
    rv21 = log_ret.rolling(21).std() * np.sqrt(252.0)
    mom126 = np.log(close / close.shift(126))

    vix_close = None
    if vix is not None and not vix.empty and "Close" in vix.columns:
        vix_s = vix.sort_index()["Close"].astype(float)

        def _vix_on(ts: pd.Timestamp) -> float | None:
            try:
                if ts in vix_s.index:
                    return float(vix_s.loc[ts])
            except Exception:
                pass
            try:
                i = vix_s.index.get_indexer([ts], method="pad")[0]
                if i >= 0:
                    return float(vix_s.iloc[i])
            except Exception:
                pass
            return None

        vix_close = _vix_on

    macro = _load_macro_yield_vix_map(
        db, buf_start.date(), end_n.date() + timedelta(days=1)
    )

    records: list[tuple[pd.Timestamp, float, float, float, float, float]] = []
    for ts, _ in close.loc[start_n : end_n].items():
        ts = pd.Timestamp(ts)
        ts_naive = ts.tz_localize(None) if ts.tzinfo else ts
        d = ts_naive.date()
        lr = log_ret.loc[ts]
        vol = rv21.loc[ts]
        mom = mom126.loc[ts]
        yld_px, vix_macro = macro.get(d, (None, None))
        vx = float(vix_macro) if vix_macro is not None and not pd.isna(vix_macro) else None
        if vx is None and vix_close is not None:
            vx = vix_close(ts_naive)
        try:
            y = float(yld_px) if yld_px is not None and not pd.isna(yld_px) else None
        except (TypeError, ValueError):
            y = None
        if any(
            v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v)))
            for v in (lr, vol, mom, vx, y)
        ):
            if log_missing_yield and y is None:
                logger.warning(
                    "[regime_classifier] skip as_of=%s: missing yield_curve_slope_proxy "
                    "(macro row for date %s)",
                    ts_naive,
                    d,
                )
            continue
        records.append(
            (
                ts_naive,
                float(lr),
                float(vol),
                float(mom),
                float(vx),
                float(y),
            )
        )

    if not records:
        return pd.DataFrame(columns=list(FEATURE_NAMES))
    idx = pd.DatetimeIndex([r[0] for r in records], name="as_of")
    df = pd.DataFrame(
        {
            FEATURE_NAMES[0]: [r[1] for r in records],
            FEATURE_NAMES[1]: [r[2] for r in records],
            FEATURE_NAMES[2]: [r[3] for r in records],
            FEATURE_NAMES[3]: [r[4] for r in records],
            FEATURE_NAMES[4]: [r[5] for r in records],
        },
        index=idx,
    )
    return df.sort_index()


def fit_regime_model(
    feature_df: pd.DataFrame,
    n_components: int = 3,
    covariance_type: str = "full",
    n_iter: int = 200,
    random_state: int = 42,
    prior_params: dict | None = None,
    *,
    warm_start_model: Any | None = None,
    train_start: datetime | None = None,
    train_end: datetime | None = None,
) -> tuple[Any, str]:
    """Fit and return (GaussianHMM, model_version_hash)."""
    from hmmlearn.hmm import GaussianHMM

    if prior_params:
        logger.debug("[regime_classifier] prior_params ignored in v1 (reserved for future)")
    if feature_df.empty or len(feature_df) < n_components * 10:
        raise ValueError("feature_df too small for HMM fit")
    X = feature_df[list(FEATURE_NAMES)].values.astype(float)
    init_params = "stmc" if warm_start_model is None else ""
    model = GaussianHMM(
        n_components=n_components,
        covariance_type=covariance_type,
        n_iter=n_iter,
        random_state=random_state,
        init_params=init_params,
        min_covar=1e-3,
    )
    if warm_start_model is not None:
        model.means_ = np.array(warm_start_model.means_, copy=True)
        model.transmat_ = np.array(warm_start_model.transmat_, copy=True)
        model.startprob_ = np.array(warm_start_model.startprob_, copy=True)
        try:
            wc = np.array(warm_start_model.covars_, copy=True)
            if covariance_type == getattr(warm_start_model, "covariance_type", None):
                model.covars_ = wc
        except Exception:
            pass
    model.fit(X)
    ts0 = train_start or feature_df.index.min().to_pydatetime()
    ts1 = train_end or feature_df.index.max().to_pydatetime()
    ver = compute_model_version_hash(
        train_start=_normalize_utc(ts0),
        train_end=_normalize_utc(ts1),
        feature_spec=FEATURE_SPEC_V1,
        random_state=random_state,
    )
    return model, ver


def relabel_by_mean_return(model: Any) -> dict[int, str]:
    """Map raw HMM state indices -> bull/chop/bear via mean SPY log return ordering."""
    means = np.asarray(model.means_, dtype=float)
    if means.shape[0] != 3 or means.shape[1] <= FEATURE_IDX_SPY_RETURN:
        raise ValueError("expected 3-state model with SPY return feature")
    order = np.argsort(means[:, FEATURE_IDX_SPY_RETURN])
    return {
        int(order[0]): "bear",
        int(order[1]): "chop",
        int(order[2]): "bull",
    }


def predict_regime(
    model: Any,
    features: np.ndarray,
    label_map: Mapping[int, str],
) -> tuple[str, dict[str, float]]:
    """Return (label, posterior_dict) where values sum to ~1."""
    x = np.asarray(features, dtype=float).reshape(1, -1)
    _, post = model.score_samples(x)
    probs = np.asarray(post[0], dtype=float)
    posterior_dict = {label_map[i]: float(probs[i]) for i in range(len(probs))}
    label = max(posterior_dict, key=posterior_dict.get)
    return label, posterior_dict


def viterbi_labels(
    model: Any, X: np.ndarray, label_map: Mapping[int, str]
) -> list[str]:
    _, states = model.decode(X.astype(float))
    return [label_map[int(s)] for s in states]


def current_regime(
    db_session: Session,
) -> tuple[str | None, dict[str, float] | None, str | None]:
    """Latest ``regime_snapshot`` row, or three Nones."""
    from ...models.trading import RegimeSnapshot

    row = (
        db_session.query(RegimeSnapshot)
        .order_by(RegimeSnapshot.as_of.desc())
        .first()
    )
    if row is None:
        return None, None, None
    post = row.posterior if isinstance(row.posterior, dict) else {}
    return row.regime, dict(post), row.model_version


def attach_regime_to_market_snapshot(db: Session, snap: Any) -> None:
    """Fill ``regime`` / ``regime_posterior`` from ``regime_snapshot`` when flag ON."""
    if not getattr(settings, "chili_regime_classifier_enabled", False):
        return
    if snap.bar_start_at is None:
        return
    from ...models.trading import RegimeSnapshot

    bs = _normalize_utc(snap.bar_start_at)
    row = (
        db.query(RegimeSnapshot)
        .filter(RegimeSnapshot.as_of == bs)
        .first()
    )
    if row is None:
        row = (
            db.query(RegimeSnapshot)
            .filter(cast(RegimeSnapshot.as_of, Date) == bs.date())
            .order_by(RegimeSnapshot.as_of.desc())
            .first()
        )
    if row is None:
        return
    snap.regime = row.regime
    snap.regime_posterior = dict(row.posterior) if row.posterior else {}


def save_regime_artifact(model: Any, label_map: dict[int, str], version: str) -> Path:
    path = regime_models_dir() / f"regime_{version.replace(':', '_')}.pkl"
    with open(path, "wb") as f:
        pickle.dump({"model": model, "label_map": label_map, "version": version}, f)
    return path


def load_latest_regime_artifact() -> dict[str, Any] | None:
    d = regime_models_dir()
    files = sorted(d.glob("regime_sha256_*.pkl"))
    if not files:
        return None
    with open(files[-1], "rb") as f:
        return pickle.load(f)


def _trade_simple_return(tr: Any) -> float | None:
    try:
        ep = float(tr.entry_price or 0)
        xp = float(tr.exit_price or 0)
    except (TypeError, ValueError):
        return None
    if ep <= 0 or xp <= 0:
        return None
    if (tr.direction or "long").lower() == "short":
        return (ep - xp) / ep
    return (xp - ep) / ep


def build_regime_scanner_sharpe_heatmap(db: Session) -> dict[str, Any]:
    """30d realized Sharpe by regime × scanner (closed trades)."""
    from datetime import datetime as dt_module

    from ...models.trading import RegimeSnapshot, ScanPattern, Trade
    from .promotion_gate import SCANNER_BUCKETS, infer_scanner_bucket

    now = dt_module.utcnow()
    cutoff = now - timedelta(days=30)
    trades = (
        db.query(Trade)
        .filter(
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= cutoff,
            Trade.scan_pattern_id.isnot(None),
        )
        .all()
    )
    pat_cache: dict[int, Any] = {}
    cells: dict[tuple[str, str], list[float]] = {}

    for tr in trades:
        ret = _trade_simple_return(tr)
        if ret is None:
            continue
        pid = int(tr.scan_pattern_id)
        if pid not in pat_cache:
            pat_cache[pid] = db.query(ScanPattern).filter(ScanPattern.id == pid).first()
        pat = pat_cache[pid]
        if pat is None:
            continue
        scanner = infer_scanner_bucket(pat)
        if scanner not in SCANNER_BUCKETS:
            continue
        ent = tr.entry_date
        if ent is None:
            continue
        ent = _normalize_utc(ent)
        rrow = (
            db.query(RegimeSnapshot)
            .filter(cast(RegimeSnapshot.as_of, Date) == ent.date())
            .order_by(RegimeSnapshot.as_of.desc())
            .first()
        )
        if rrow is None:
            continue
        reg = rrow.regime
        if reg not in REGIME_ORDER:
            continue
        cells.setdefault((reg, scanner), []).append(ret)

    sharpes: list[list[float | None]] = []
    counts: list[list[int]] = []
    for reg in REGIME_ORDER:
        s_row: list[float | None] = []
        n_row: list[int] = []
        for sc in SCANNER_BUCKETS:
            xs = cells.get((reg, sc), [])
            n = len(xs)
            n_row.append(n)
            if n < 10:
                s_row.append(None)
                continue
            a = np.asarray(xs, dtype=float)
            mu = float(np.mean(a))
            sd = float(np.std(a, ddof=1))
            if sd < 1e-12:
                s_row.append(None)
            else:
                s_row.append(mu / sd * np.sqrt(252.0))
        sharpes.append(s_row)
        counts.append(n_row)

    mv_row = (
        db.query(RegimeSnapshot)
        .order_by(RegimeSnapshot.as_of.desc())
        .first()
    )
    return {
        "ok": True,
        "model_version": mv_row.model_version if mv_row else None,
        "as_of": mv_row.as_of.isoformat() if mv_row and mv_row.as_of else None,
        "regimes": list(REGIME_ORDER),
        "scanners": list(SCANNER_BUCKETS),
        "sharpe_matrix": sharpes,
        "n_trades_matrix": counts,
    }


def run_weekly_regime_retrain(db: Session) -> dict[str, Any]:
    """Rolling 5y train ending 21 business days ago; decode through yesterday."""
    from ...models.trading import RegimeSnapshot

    if not getattr(settings, "chili_regime_classifier_enabled", False):
        return {"ok": False, "reason": "flag_off"}

    from pandas.tseries.offsets import BDay

    now = pd.Timestamp.now("UTC").replace(tzinfo=None).normalize()
    train_end = now - BDay(21)
    train_start = train_end - pd.DateOffset(years=5)
    feat_train = build_regime_features(
        db,
        train_start.to_pydatetime(),
        train_end.to_pydatetime(),
        log_missing_yield=False,
    )
    if feat_train.empty or len(feat_train) < 200:
        logger.warning("[regime_classifier] weekly retrain: insufficient features (%s)", len(feat_train))
        return {"ok": False, "reason": "insufficient_features", "n": len(feat_train)}

    rs = int(getattr(settings, "chili_regime_classifier_random_state", 42) or 42)
    n_iter = int(getattr(settings, "chili_regime_classifier_n_iter", 200) or 200)
    prior_art = load_latest_regime_artifact()
    warm = prior_art["model"] if prior_art else None

    model, ver = fit_regime_model(
        feat_train,
        n_iter=n_iter,
        random_state=rs,
        warm_start_model=warm,
        train_start=train_start.to_pydatetime(),
        train_end=train_end.to_pydatetime(),
    )
    label_map = relabel_by_mean_return(model)
    save_regime_artifact(model, dict(label_map), ver)

    decode_end = now - BDay(1)
    mx = db.query(func.max(RegimeSnapshot.as_of)).scalar()
    if mx is not None:
        decode_from = pd.Timestamp(mx).normalize() + BDay(1)
        if decode_from > decode_end:
            return {"ok": True, "model_version": ver, "rows": 0, "note": "decode_up_to_date"}
    else:
        decode_from = feat_train.index.min()
    feat_decode = build_regime_features(
        db,
        decode_from.to_pydatetime(),
        decode_end.to_pydatetime(),
        log_missing_yield=False,
    )
    if feat_decode.empty:
        return {"ok": False, "reason": "decode_empty"}

    X = feat_decode[list(FEATURE_NAMES)].values.astype(float)
    _, states = model.decode(X)
    for i, ts in enumerate(feat_decode.index):
        raw = int(states[i])
        lab = label_map[raw]
        xrow = X[i]
        _, post = predict_regime(model, xrow, label_map)
        feat_row = {k: float(xrow[j]) for j, k in enumerate(FEATURE_NAMES)}
        ts_db = ts.to_pydatetime()
        if ts_db.tzinfo:
            ts_db = ts_db.astimezone(timezone.utc).replace(tzinfo=None)
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
    db.commit()
    return {"ok": True, "model_version": ver, "rows": len(feat_decode)}
