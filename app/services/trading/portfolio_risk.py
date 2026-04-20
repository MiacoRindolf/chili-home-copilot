"""Portfolio-level risk controls for the trading brain.

Enforces hard caps before any new position is opened:
- Max concurrent open positions
- Portfolio heat cap (total risk across all open positions)
- Sector/asset-class concentration limits
- Per-trade risk sizing (fixed fractional)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)


@dataclass
class RiskBudget:
    """Snapshot of current portfolio risk exposure."""
    open_positions: int = 0
    crypto_positions: int = 0
    stock_positions: int = 0
    total_heat_pct: float = 0.0
    available_heat_pct: float = 0.0
    can_open_new: bool = True
    rejection_reason: str | None = None
    sector_exposure: dict[str, float] = field(default_factory=dict)
    portfolio_var_pct: float | None = None
    portfolio_cvar_pct: float | None = None


@dataclass
class RiskLimits:
    max_open_positions: int = 10
    max_crypto_positions: int = 5
    max_stock_positions: int = 8
    max_portfolio_heat_pct: float = 6.0  # total risk as % of capital
    max_risk_per_trade_pct: float = 1.0  # max 1% capital per trade
    max_same_ticker: int = 2
    max_sector_pct: float = 40.0         # max % of open positions in one sector
    max_avg_correlation: float = 0.75    # reject if avg correl with open positions exceeds this


def get_risk_limits(settings: Any | None = None) -> RiskLimits:
    """Build limits from app settings, falling back to conservative defaults."""
    if settings is None:
        from ...config import settings as _s
        settings = _s
    return RiskLimits(
        max_open_positions=int(getattr(settings, "brain_risk_max_positions", 10)),
        max_crypto_positions=int(getattr(settings, "brain_risk_max_crypto", 5)),
        max_stock_positions=int(getattr(settings, "brain_risk_max_stocks", 8)),
        max_portfolio_heat_pct=float(getattr(settings, "brain_risk_max_heat_pct", 6.0)),
        max_risk_per_trade_pct=float(getattr(settings, "brain_risk_per_trade_pct", 1.0)),
        max_same_ticker=int(getattr(settings, "brain_risk_max_same_ticker", 2)),
        max_sector_pct=float(getattr(settings, "brain_risk_max_sector_pct", 40.0)),
        max_avg_correlation=float(getattr(settings, "brain_risk_max_avg_correlation", 0.75)),
    )


def compute_trade_risk_pct(
    entry_price: float,
    stop_price: float,
    quantity: float,
    capital: float,
) -> float:
    """Return risk as a percentage of capital for a single trade."""
    if capital <= 0 or entry_price <= 0:
        return 0.0
    risk_per_share = abs(entry_price - stop_price)
    total_risk = risk_per_share * quantity
    return round(total_risk / capital * 100, 4)


def get_portfolio_risk_snapshot(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    limits: RiskLimits | None = None,
) -> RiskBudget:
    """Calculate current portfolio risk exposure from open trades."""
    if limits is None:
        limits = get_risk_limits()

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    budget = RiskBudget()
    budget.open_positions = len(open_trades)
    budget.crypto_positions = sum(1 for t in open_trades if t.ticker.endswith("-USD"))
    budget.stock_positions = budget.open_positions - budget.crypto_positions

    total_heat = 0.0
    for t in open_trades:
        stop = _infer_stop(t)
        if stop and capital > 0:
            risk = abs(t.entry_price - stop) * t.quantity
            total_heat += risk / capital * 100
    budget.total_heat_pct = round(total_heat, 2)
    budget.available_heat_pct = round(max(0, limits.max_portfolio_heat_pct - total_heat), 2)

    if budget.open_positions >= limits.max_open_positions:
        budget.can_open_new = False
        budget.rejection_reason = f"Max positions ({limits.max_open_positions}) reached"
    elif budget.total_heat_pct >= limits.max_portfolio_heat_pct:
        budget.can_open_new = False
        budget.rejection_reason = f"Portfolio heat {budget.total_heat_pct:.1f}% >= cap {limits.max_portfolio_heat_pct}%"

    # Enrich with sector exposure (lightweight — no market data fetch)
    try:
        budget.sector_exposure = compute_sector_exposure(db, user_id)
    except Exception:
        pass

    # VaR is expensive (fetches OHLCV); compute only when positions exist
    if budget.open_positions > 0:
        try:
            budget.portfolio_var_pct = estimate_portfolio_var(db, user_id, capital)
        except Exception:
            pass
        try:
            budget.portfolio_cvar_pct = estimate_portfolio_cvar(db, user_id, capital)
        except Exception:
            pass

    return budget


def check_new_trade_allowed(
    db: Session,
    user_id: int | None,
    ticker: str,
    capital: float = 100_000.0,
    limits: RiskLimits | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason) for opening a new position in *ticker*."""
    try:
        from .governance import is_kill_switch_active
        if is_kill_switch_active():
            return False, "Kill switch is active — all trading halted"
    except Exception:
        logger.error("[risk] Kill-switch check failed — blocking trade as precaution", exc_info=True)
        return False, "Kill-switch check failed — trade blocked as safety precaution"

    if is_breaker_tripped():
        return False, f"Circuit breaker active: {_breaker_reason}"

    tripped, reason = check_drawdown_breaker(db, user_id, capital)
    if tripped:
        return False, f"Circuit breaker triggered: {reason}"

    if limits is None:
        limits = get_risk_limits()

    budget = get_portfolio_risk_snapshot(db, user_id, capital, limits)
    if not budget.can_open_new:
        return False, budget.rejection_reason or "Risk limit exceeded"

    is_crypto = ticker.upper().endswith("-USD")
    if is_crypto and budget.crypto_positions >= limits.max_crypto_positions:
        return False, f"Crypto cap ({limits.max_crypto_positions}) reached"
    if not is_crypto and budget.stock_positions >= limits.max_stock_positions:
        return False, f"Stock cap ({limits.max_stock_positions}) reached"

    same_ticker_count = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.ticker == ticker.upper(),
        Trade.status == "open",
    ).count()
    if same_ticker_count >= limits.max_same_ticker:
        return False, f"Already {same_ticker_count} open positions in {ticker}"

    # Sector concentration check (scoped to user to prevent cross-user leakage)
    try:
        sector_q = db.query(Trade.sector).filter(
            Trade.ticker == ticker.upper(),
        )
        if user_id is not None:
            sector_q = sector_q.filter(Trade.user_id == user_id)
        new_trade_sector = sector_q.order_by(Trade.id.desc()).first()
        sector_label = new_trade_sector[0] if new_trade_sector else None
        allowed, reason = check_sector_concentration(db, user_id, sector_label, limits)
        if not allowed:
            return False, reason
    except Exception:
        logger.warning("[risk] sector concentration check failed — flagging trade", exc_info=True)
        return True, "ok (sector check unavailable — proceed with caution)"

    # Correlation risk check
    try:
        allowed, reason = check_correlation_risk(db, user_id, ticker, limits)
        if not allowed:
            return False, reason
    except Exception:
        logger.warning("[risk] correlation check failed — flagging trade", exc_info=True)
        return True, "ok (correlation check unavailable — proceed with caution)"

    return True, "ok"


def size_position(
    capital: float,
    entry_price: float,
    stop_price: float,
    risk_pct: float | None = None,
    limits: RiskLimits | None = None,
) -> int:
    """Calculate position size (shares) for a given risk budget.

    Uses fixed-fractional sizing: risk_pct% of capital = max loss.
    """
    if limits is None:
        limits = get_risk_limits()
    if risk_pct is None:
        risk_pct = limits.max_risk_per_trade_pct

    risk_amount = capital * (risk_pct / 100)
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0 or entry_price <= 0:
        return 0

    shares = int(risk_amount / risk_per_share)
    max_notional = capital * 0.20  # never more than 20% of capital in one position
    max_by_notional = int(max_notional / entry_price)
    return max(0, min(shares, max_by_notional))


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Calculate the Kelly Criterion fraction for optimal bet sizing.

    Returns a fraction of capital to risk (0 to 1). Capped at 25% (quarter-Kelly
    is standard practice to reduce variance).

    f* = (p * b - q) / b
    where p = win probability, q = loss probability, b = avg_win / avg_loss
    """
    if win_rate <= 0 or win_rate >= 1 or avg_win <= 0 or avg_loss <= 0:
        return 0.0

    p = win_rate
    q = 1 - p
    b = avg_win / avg_loss

    kelly = (p * b - q) / b
    kelly = max(0.0, kelly)

    # Quarter-Kelly for safety
    return min(0.25, kelly * 0.25)


def size_position_kelly(
    capital: float,
    entry_price: float,
    stop_price: float,
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    *,
    max_kelly_pct: float = 5.0,
    limits: RiskLimits | None = None,
    # Phase H shadow-emit context (all optional). Passing ``db`` + ``ticker``
    # enables a Phase H canonical sizer log row for divergence analysis.
    db: "Session | None" = None,
    user_id: int | None = None,
    ticker: str | None = None,
    pattern_id: int | None = None,
    target_price: float | None = None,
    regime: str | None = None,
    asset_class: str | None = None,
) -> dict[str, Any]:
    """Kelly-optimized position sizing with drawdown scaling.

    Returns dict with shares, kelly_fraction, risk_pct, and sizing rationale.
    """
    if limits is None:
        limits = get_risk_limits()

    kf = kelly_fraction(win_rate, avg_win_pct, avg_loss_pct)
    risk_pct = min(kf * 100, max_kelly_pct, limits.max_risk_per_trade_pct)

    shares = size_position(capital, entry_price, stop_price, risk_pct=risk_pct, limits=limits)

    # Phase H shadow hook. Defensive: never breaks Kelly sizing.
    if db is not None and ticker:
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_active

            if mode_is_active():
                _legacy_notional = (
                    float(shares) * float(entry_price)
                    if shares > 0 and entry_price > 0
                    else None
                )
                # Infer a rough calibrated-prob from the legacy win_rate.
                emit_shadow_proposal(
                    db,
                    signal=EmitterSignal(
                        source="portfolio_risk.size_position_kelly",
                        ticker=ticker,
                        direction="long",
                        entry_price=float(entry_price),
                        stop_price=float(stop_price),
                        capital=float(capital),
                        target_price=float(target_price) if target_price else None,
                        asset_class=asset_class,
                        user_id=user_id,
                        pattern_id=pattern_id,
                        regime=regime,
                        confidence=float(win_rate) if win_rate is not None else None,
                    ),
                    legacy=LegacySizing(
                        notional=_legacy_notional,
                        quantity=float(shares) if shares else None,
                        source="portfolio_risk.size_position_kelly",
                    ),
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug("[portfolio_risk.kelly] phase H shadow emit failed", exc_info=True)

    return {
        "shares": shares,
        "kelly_fraction": round(kf, 4),
        "risk_pct": round(risk_pct, 3),
        "notional": round(shares * entry_price, 2) if shares > 0 else 0,
        "risk_amount": round(capital * risk_pct / 100, 2),
        "method": "kelly_quarter",
    }


def size_with_drawdown_scaling(
    db: Session,
    user_id: int | None,
    capital: float,
    entry_price: float,
    stop_price: float,
    win_rate: float = 0.55,
    avg_win_pct: float = 2.0,
    avg_loss_pct: float = 1.0,
    limits: RiskLimits | None = None,
    # Phase H shadow-emit context (optional). Supply ``ticker`` to turn on.
    ticker: str | None = None,
    pattern_id: int | None = None,
    target_price: float | None = None,
    regime: str | None = None,
    asset_class: str | None = None,
) -> dict[str, Any]:
    """Kelly sizing scaled down by current drawdown severity.

    If the account is in drawdown, reduce position size proportionally.
    """
    from datetime import datetime, timedelta

    if limits is None:
        limits = get_risk_limits()

    # Note: we deliberately DO NOT pass the Phase H shadow-emit kwargs into
    # ``size_position_kelly`` here; the DD-scaled variant owns the final
    # ``scaled_shares``, so we emit once after scaling rather than twice.
    base = size_position_kelly(
        capital, entry_price, stop_price,
        win_rate, avg_win_pct, avg_loss_pct,
        limits=limits,
    )

    # Calculate drawdown scaling factor
    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)
    recent = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date >= thirty_days_ago,
    ).all()

    pnl_30d = sum(t.pnl or 0 for t in recent)
    dd_pct = abs(pnl_30d / capital * 100) if capital > 0 and pnl_30d < 0 else 0

    # Scale factor: 1.0 at no DD, 0.5 at 4% DD, 0.25 at 8% DD
    if dd_pct <= 0:
        scale = 1.0
    elif dd_pct >= 8:
        scale = 0.25
    else:
        scale = max(0.25, 1.0 - (dd_pct / 8.0) * 0.75)

    scaled_shares = max(0, int(base["shares"] * scale))
    base["dd_scale_factor"] = round(scale, 3)
    base["drawdown_30d_pct"] = round(dd_pct, 2)
    base["shares_before_dd_scale"] = base["shares"]
    base["shares"] = scaled_shares
    base["notional"] = round(scaled_shares * entry_price, 2)
    base["method"] = "kelly_quarter_dd_scaled"

    # Phase H shadow hook.
    if ticker:
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_active

            if mode_is_active():
                _legacy_notional = (
                    float(scaled_shares) * float(entry_price)
                    if scaled_shares > 0 and entry_price > 0
                    else None
                )
                emit_shadow_proposal(
                    db,
                    signal=EmitterSignal(
                        source="portfolio_risk.size_with_drawdown_scaling",
                        ticker=ticker,
                        direction="long",
                        entry_price=float(entry_price),
                        stop_price=float(stop_price),
                        capital=float(capital),
                        target_price=float(target_price) if target_price else None,
                        asset_class=asset_class,
                        user_id=user_id,
                        pattern_id=pattern_id,
                        regime=regime,
                        confidence=float(win_rate) if win_rate is not None else None,
                    ),
                    legacy=LegacySizing(
                        notional=_legacy_notional,
                        quantity=float(scaled_shares) if scaled_shares else None,
                        source="portfolio_risk.size_with_drawdown_scaling",
                    ),
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "[portfolio_risk.dd] phase H shadow emit failed", exc_info=True
            )

    return base


# ── Portfolio-level correlation & sector risk ────────────────────────

def compute_sector_exposure(
    db: Session,
    user_id: int | None,
) -> dict[str, float]:
    """Return % of open positions in each sector.

    Uses the ``Trade.sector`` column.  Tickers with no sector assigned
    are grouped under "unknown".
    """
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    if not open_trades:
        return {}

    counts: dict[str, int] = {}
    for t in open_trades:
        sector = (t.sector or "unknown").strip().lower()
        counts[sector] = counts.get(sector, 0) + 1

    total = len(open_trades)
    return {s: round(c / total * 100, 1) for s, c in counts.items()}


def compute_pairwise_correlation(
    tickers: list[str],
    lookback: int = 60,
) -> dict[str, dict[str, float]]:
    """Compute Pearson correlation matrix of daily returns for *tickers*.

    Returns nested dict: ``{ticker_a: {ticker_b: corr, ...}, ...}``.
    Tickers with insufficient data are silently excluded.
    """
    import numpy as np

    from .market_data import fetch_ohlcv_df

    if len(tickers) < 2:
        return {}

    returns_by_ticker: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, interval="1d", period=f"{lookback + 10}d")
            if df is None or len(df) < 20:
                continue
            close = df["Close"].values
            rets = list((close[1:] - close[:-1]) / close[:-1])
            returns_by_ticker[ticker] = rets[-lookback:]
        except Exception:
            continue

    valid = list(returns_by_ticker.keys())
    if len(valid) < 2:
        return {}

    # Align lengths to the shortest series
    min_len = min(len(returns_by_ticker[t]) for t in valid)
    matrix: dict[str, dict[str, float]] = {}
    for a in valid:
        matrix[a] = {}
        ra = np.array(returns_by_ticker[a][-min_len:])
        for b in valid:
            if a == b:
                matrix[a][b] = 1.0
                continue
            rb = np.array(returns_by_ticker[b][-min_len:])
            if np.std(ra) == 0 or np.std(rb) == 0:
                matrix[a][b] = 0.0
            else:
                matrix[a][b] = round(float(np.corrcoef(ra, rb)[0, 1]), 4)

    return matrix


def check_correlation_risk(
    db: Session,
    user_id: int | None,
    new_ticker: str,
    limits: RiskLimits | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` after checking correlation with open positions.

    If the average absolute correlation between *new_ticker* and all
    currently open tickers exceeds ``limits.max_avg_correlation``, the
    trade is rejected.
    """
    if limits is None:
        limits = get_risk_limits()

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    existing_tickers = list({t.ticker for t in open_trades})
    if len(existing_tickers) < 2:
        return True, "ok"

    try:
        all_tickers = existing_tickers + [new_ticker.upper()]
        corr_matrix = compute_pairwise_correlation(all_tickers)
        if not corr_matrix or new_ticker.upper() not in corr_matrix:
            return True, "ok"  # insufficient data, allow

        new_corrs = corr_matrix[new_ticker.upper()]
        peer_corrs = [
            abs(new_corrs.get(t, 0.0))
            for t in existing_tickers
            if t in new_corrs and t != new_ticker.upper()
        ]
        if not peer_corrs:
            return True, "ok"

        avg_corr = sum(peer_corrs) / len(peer_corrs)
        if avg_corr > limits.max_avg_correlation:
            return False, (
                f"Avg correlation {avg_corr:.2f} with open positions "
                f"exceeds limit {limits.max_avg_correlation}"
            )
    except Exception:
        logger.debug("[risk] correlation check failed; allowing trade", exc_info=True)

    return True, "ok"


def check_sector_concentration(
    db: Session,
    user_id: int | None,
    new_ticker_sector: str | None,
    limits: RiskLimits | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` after checking sector concentration.

    Projects what sector exposure would look like after adding one more
    position in *new_ticker_sector* and rejects if any sector would
    exceed ``limits.max_sector_pct``.
    """
    if limits is None:
        limits = get_risk_limits()

    sector = (new_ticker_sector or "unknown").strip().lower()

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    if not open_trades:
        return True, "ok"

    # Skip the sector cap entirely when the proposed trade has no known
    # sector. Treating "unknown" as a real sector bucket causes false
    # rejections whenever existing positions (e.g. broker-sync imported
    # rows without sector enrichment) dominate the "unknown" bucket. The
    # concentration gate exists to limit exposure to a REAL named sector;
    # "unknown" is a data-quality artifact, not a risk dimension.
    if sector == "unknown":
        return True, "ok"

    total_after = len(open_trades) + 1
    same_sector = sum(
        1 for t in open_trades
        if (t.sector or "unknown").strip().lower() == sector
    ) + 1  # +1 for the proposed trade

    projected_pct = same_sector / total_after * 100
    if projected_pct > limits.max_sector_pct:
        return False, (
            f"Sector '{sector}' would reach {projected_pct:.0f}% "
            f"({same_sector}/{total_after}), exceeds {limits.max_sector_pct}% cap"
        )

    return True, "ok"


def estimate_portfolio_var(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    confidence: float = 0.95,
    lookback: int = 60,
) -> float | None:
    """Estimate parametric Value-at-Risk (%) for current open positions.

    Uses a covariance-based approach on daily returns, assuming normal
    distribution.  Returns the estimated 1-day loss as a percentage of
    capital, or None if insufficient data.
    """
    import numpy as np
    from scipy.stats import norm

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()

    if not open_trades or capital <= 0:
        return None

    # Collect per-position weights (notional / capital) and returns
    from .market_data import fetch_ohlcv_df

    tickers = list({t.ticker for t in open_trades})
    weights: dict[str, float] = {}
    for t in open_trades:
        notional = t.entry_price * t.quantity
        weights[t.ticker] = weights.get(t.ticker, 0.0) + notional / capital

    returns_data: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, interval="1d", period=f"{lookback + 10}d")
            if df is None or len(df) < 20:
                continue
            close = df["Close"].values
            rets = list((close[1:] - close[:-1]) / close[:-1])
            returns_data[ticker] = rets[-lookback:]
        except Exception:
            continue

    valid = [t for t in tickers if t in returns_data]
    if len(valid) < 1:
        return None

    min_len = min(len(returns_data[t]) for t in valid)
    ret_matrix = np.array([returns_data[t][-min_len:] for t in valid])
    w = np.array([weights.get(t, 0.0) for t in valid])

    cov = np.cov(ret_matrix)
    if cov.ndim == 0:
        cov = np.array([[float(cov)]])

    portfolio_var = float(np.sqrt(w @ cov @ w))
    z = norm.ppf(confidence)
    var_pct = round(portfolio_var * z * 100, 4)

    return var_pct


def estimate_portfolio_cvar(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    confidence: float = 0.95,
    lookback: int = 60,
) -> float | None:
    """Estimate historical portfolio CVaR (%) from open-position return series."""
    import numpy as np
    from .market_data import fetch_ohlcv_df

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()
    if not open_trades or capital <= 0:
        return None

    tickers = list({t.ticker for t in open_trades})
    weights: dict[str, float] = {}
    for t in open_trades:
        notional = t.entry_price * t.quantity
        weights[t.ticker] = weights.get(t.ticker, 0.0) + notional / capital

    returns_data: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, interval="1d", period=f"{lookback + 10}d")
            if df is None or len(df) < 20:
                continue
            close = df["Close"].values
            rets = list((close[1:] - close[:-1]) / close[:-1])
            returns_data[ticker] = rets[-lookback:]
        except Exception:
            continue

    valid = [t for t in tickers if t in returns_data]
    if not valid:
        return None
    min_len = min(len(returns_data[t]) for t in valid)
    if min_len < 5:
        return None
    mat = np.array([returns_data[t][-min_len:] for t in valid])
    w = np.array([weights.get(t, 0.0) for t in valid])
    portfolio_returns = np.dot(mat.T, w)
    losses = -portfolio_returns
    var_cut = np.quantile(losses, confidence)
    tail = losses[losses >= var_cut]
    if len(tail) == 0:
        return None
    return round(float(np.mean(tail) * 100.0), 4)


def _infer_stop(trade: Trade) -> float | None:
    """Infer stop-loss price from trade tags/notes or use a default ATR-based estimate."""
    import json
    if trade.tags:
        try:
            tag_data = json.loads(trade.tags) if trade.tags.startswith("{") else {}
            sl = tag_data.get("stop_loss") or tag_data.get("stop")
            if sl:
                return float(sl)
        except Exception:
            pass
    if trade.indicator_snapshot:
        try:
            snap = json.loads(trade.indicator_snapshot)
            atr = snap.get("atr", {}).get("value")
            if atr:
                return trade.entry_price - (2.0 * float(atr))
        except Exception:
            pass
    # Default: assume 2% stop from entry
    return trade.entry_price * 0.98


# ── Drawdown Circuit Breaker ──────────────────────────────────────────

_breaker_tripped = False
_breaker_reason: str | None = None


@dataclass
class DrawdownLimits:
    max_5day_dd_pct: float = 3.0   # pause if 5-day P&L drops below -3%
    max_30day_dd_pct: float = 8.0  # pause if 30-day P&L drops below -8%
    max_consecutive_losses: int = 5
    cooldown_hours: int = 24       # how long the breaker stays tripped


# Phase 2: regime multipliers applied to the base drawdown thresholds.
# The baseline (env / default) matches the prior behavior in ``cautious``.
# ``risk_on`` tolerates more normal-volatility drawdown before tripping;
# ``risk_off`` trips earlier because early-stage losses in choppy regimes
# are stronger danger signals.
_REGIME_DD_MULTIPLIERS: dict[str, float] = {
    "risk_on": 1.5,
    "cautious": 1.0,
    "risk_off": 0.75,
}


def _read_current_regime(db: Session) -> str | None:
    try:
        from .runtime_surface_state import read_runtime_surface_state

        surface = read_runtime_surface_state(db, surface="regime")
        if not surface:
            return None
        r = str(surface.get("regime") or "").strip().lower()
        return r if r in _REGIME_DD_MULTIPLIERS else None
    except Exception:
        return None


def get_drawdown_limits(
    settings: Any | None = None,
    *,
    db: Session | None = None,
) -> DrawdownLimits:
    """Resolve effective drawdown limits, regime-scaled when a DB is supplied.

    Backwards-compatible: callers who don't pass ``db`` get the static
    settings-driven limits (same as before). The circuit-breaker path now
    always supplies ``db`` so thresholds track the brain's regime surface
    instead of being a flat env constant.
    """
    if settings is None:
        from ...config import settings as _s
        settings = _s
    base_5d = float(getattr(settings, "brain_risk_max_5d_dd_pct", 3.0))
    base_30d = float(getattr(settings, "brain_risk_max_30d_dd_pct", 8.0))
    mult = 1.0
    if db is not None:
        regime = _read_current_regime(db)
        if regime is not None:
            mult = _REGIME_DD_MULTIPLIERS.get(regime, 1.0)
    return DrawdownLimits(
        max_5day_dd_pct=base_5d * mult,
        max_30day_dd_pct=base_30d * mult,
        max_consecutive_losses=int(getattr(settings, "brain_risk_max_consec_losses", 5)),
        cooldown_hours=int(getattr(settings, "brain_risk_cooldown_hours", 24)),
    )


def _compute_unrealized_pnl(db: Session, user_id: int | None) -> float:
    """Compute mark-to-market unrealized P&L across open trades."""
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    ).all()
    if not open_trades:
        return 0.0

    total_unrealized = 0.0
    try:
        from .market_data import fetch_quote
    except ImportError:
        return 0.0

    for t in open_trades:
        try:
            q = fetch_quote(t.ticker)
            if q and q.get("price"):
                current_price = float(q["price"])
                qty = float(t.quantity or 0)
                entry = float(t.entry_price or 0)
                if qty > 0 and entry > 0:
                    total_unrealized += (current_price - entry) * qty
        except Exception:
            continue

    return total_unrealized


def check_drawdown_breaker(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    limits: DrawdownLimits | None = None,
) -> tuple[bool, str | None]:
    """Check if the circuit breaker should be tripped.

    Includes both realized (closed) and **unrealized (mark-to-market)** P&L
    to prevent the breaker from being blind to open losing positions.

    Returns (tripped: bool, reason: str | None).
    """
    global _breaker_tripped, _breaker_reason
    from datetime import datetime, timedelta

    if limits is None:
        # Phase 2: pass db so the regime multiplier kicks in (risk_off trips
        # earlier, risk_on more tolerant). Unchanged when no regime surface.
        limits = get_drawdown_limits(db=db)

    now = datetime.utcnow()

    # Mark-to-market unrealized P&L
    unrealized_pnl = 0.0
    try:
        unrealized_pnl = _compute_unrealized_pnl(db, user_id)
    except Exception:
        logger.debug("[circuit_breaker] Unrealized P&L computation failed", exc_info=True)

    unrealized_pct = (unrealized_pnl / capital * 100) if capital > 0 else 0

    # Trip immediately if unrealized loss exceeds 30-day threshold
    if unrealized_pct < -limits.max_30day_dd_pct:
        _breaker_tripped = True
        _breaker_reason = (
            f"Unrealized MTM drawdown {unrealized_pct:.1f}% "
            f"exceeds -{limits.max_30day_dd_pct}% limit"
        )
        _persist_breaker_state(True, _breaker_reason)
        logger.warning("[circuit_breaker] TRIPPED (MTM): %s", _breaker_reason)
        return True, _breaker_reason

    # 5-day rolling P&L (realized + unrealized)
    five_days_ago = now - timedelta(days=5)
    recent_5d = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date >= five_days_ago,
    ).all()
    pnl_5d_realized = sum(t.pnl or 0 for t in recent_5d)
    pnl_5d_total = pnl_5d_realized + unrealized_pnl
    pnl_5d_pct = (pnl_5d_total / capital * 100) if capital > 0 else 0

    if pnl_5d_pct < -limits.max_5day_dd_pct:
        _breaker_tripped = True
        _breaker_reason = (
            f"5-day drawdown {pnl_5d_pct:.1f}% (realized={pnl_5d_realized:.0f}, "
            f"unrealized={unrealized_pnl:.0f}) exceeds -{limits.max_5day_dd_pct}% limit"
        )
        _persist_breaker_state(True, _breaker_reason)
        logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
        return True, _breaker_reason

    # 30-day rolling P&L (realized + unrealized)
    thirty_days_ago = now - timedelta(days=30)
    recent_30d = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date >= thirty_days_ago,
    ).all()
    pnl_30d_realized = sum(t.pnl or 0 for t in recent_30d)
    pnl_30d_total = pnl_30d_realized + unrealized_pnl
    pnl_30d_pct = (pnl_30d_total / capital * 100) if capital > 0 else 0

    if pnl_30d_pct < -limits.max_30day_dd_pct:
        _breaker_tripped = True
        _breaker_reason = (
            f"30-day drawdown {pnl_30d_pct:.1f}% (realized={pnl_30d_realized:.0f}, "
            f"unrealized={unrealized_pnl:.0f}) exceeds -{limits.max_30day_dd_pct}% limit"
        )
        _persist_breaker_state(True, _breaker_reason)
        logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
        return True, _breaker_reason

    # Consecutive losses
    last_n = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
    ).order_by(Trade.exit_date.desc()).limit(limits.max_consecutive_losses).all()
    if len(last_n) >= limits.max_consecutive_losses:
        if all((t.pnl or 0) < 0 for t in last_n):
            _breaker_tripped = True
            _breaker_reason = f"{limits.max_consecutive_losses} consecutive losing trades"
            _persist_breaker_state(True, _breaker_reason)
            logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
            return True, _breaker_reason

    _breaker_tripped = False
    _breaker_reason = None
    return False, None


def is_breaker_tripped() -> bool:
    return _breaker_tripped


def get_breaker_status() -> dict[str, Any]:
    return {
        "tripped": _breaker_tripped,
        "reason": _breaker_reason,
    }


def reset_breaker() -> None:
    """Manually reset the circuit breaker (admin action). Persists to DB."""
    global _breaker_tripped, _breaker_reason
    _breaker_tripped = False
    _breaker_reason = None
    _persist_breaker_state(False, None)
    logger.info("[circuit_breaker] Manually reset")


def _persist_breaker_state(tripped: bool, reason: str | None) -> None:
    """Write circuit breaker state to trading_risk_state so it survives restarts."""
    try:
        from ...db import SessionLocal
        from sqlalchemy import text
        sess = SessionLocal()
        try:
            sess.execute(text(
                "INSERT INTO trading_risk_state (user_id, snapshot_date, breaker_tripped, breaker_reason, regime, capital) "
                "VALUES (:uid, NOW(), :tripped, :reason, 'circuit_breaker', 0) "
            ), {"uid": None, "tripped": tripped, "reason": reason or ""})
            sess.commit()
        finally:
            sess.close()
    except Exception:
        logger.debug("[circuit_breaker] Failed to persist breaker state to DB", exc_info=True)


def restore_breaker_from_db() -> None:
    """Restore circuit breaker state from DB on startup."""
    global _breaker_tripped, _breaker_reason
    try:
        from ...db import SessionLocal
        from sqlalchemy import text
        sess = SessionLocal()
        try:
            row = sess.execute(text(
                "SELECT breaker_tripped, breaker_reason FROM trading_risk_state "
                "WHERE regime = 'circuit_breaker' ORDER BY created_at DESC LIMIT 1"
            )).fetchone()
            if row and row[0]:
                _breaker_tripped = True
                _breaker_reason = row[1] or "restored from DB"
                logger.warning("[circuit_breaker] Breaker restored from DB: %s", _breaker_reason)
        finally:
            sess.close()
    except Exception:
        logger.debug("[circuit_breaker] Could not restore breaker from DB", exc_info=True)


# ── Unified Risk Gate ─────────────────────────────────────────────────


def unified_risk_check(
    db: Session,
    user_id: int | None,
    ticker: str,
    *,
    capital: float = 100_000.0,
    entry_price: float | None = None,
    stop_price: float | None = None,
    execution_path: str = "unknown",
) -> tuple[bool, str, dict[str, Any]]:
    """Single risk gate for ALL execution paths (proposals, momentum, paper, live).

    Returns (allowed: bool, reason: str, detail: dict).
    This replaces the need for each path to call different risk functions.
    """
    detail: dict[str, Any] = {"execution_path": execution_path}

    # 1. Kill switch
    try:
        from .governance import is_kill_switch_active
        if is_kill_switch_active():
            return False, "Kill switch active", detail
    except Exception:
        return False, "Kill-switch check failed — blocking as precaution", detail

    # 2. Circuit breaker (now includes MTM)
    tripped, reason = check_drawdown_breaker(db, user_id, capital)
    if tripped:
        detail["breaker_reason"] = reason
        return False, f"Circuit breaker: {reason}", detail

    # 3. Position limits
    limits = get_risk_limits()
    budget = get_portfolio_risk_snapshot(db, user_id, capital, limits)
    detail["portfolio_heat_pct"] = budget.total_heat_pct
    detail["open_positions"] = budget.open_positions

    if not budget.can_open_new:
        return False, budget.rejection_reason or "Risk limit exceeded", detail

    # 4. Asset-class caps
    is_crypto = ticker.upper().endswith("-USD")
    if is_crypto and budget.crypto_positions >= limits.max_crypto_positions:
        return False, f"Crypto cap ({limits.max_crypto_positions}) reached", detail
    if not is_crypto and budget.stock_positions >= limits.max_stock_positions:
        return False, f"Stock cap ({limits.max_stock_positions}) reached", detail

    # 5. Same-ticker limit
    same_count = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.ticker == ticker.upper(),
        Trade.status == "open",
    ).count()
    if same_count >= limits.max_same_ticker:
        return False, f"Already {same_count} open in {ticker}", detail

    # 6. Sector concentration (user-scoped)
    try:
        sector_q = db.query(Trade.sector).filter(Trade.ticker == ticker.upper())
        if user_id is not None:
            sector_q = sector_q.filter(Trade.user_id == user_id)
        sect = sector_q.order_by(Trade.id.desc()).first()
        sect_label = sect[0] if sect else None
        allowed, reason = check_sector_concentration(db, user_id, sect_label, limits)
        if not allowed:
            return False, reason, detail
    except Exception:
        pass

    # 7. Correlation
    try:
        allowed, reason = check_correlation_risk(db, user_id, ticker, limits)
        if not allowed:
            return False, reason, detail
    except Exception:
        pass

    # 8. Portfolio-level drawdown check (optimizer)
    try:
        from .portfolio_optimizer import check_portfolio_drawdown
        dd = check_portfolio_drawdown(db, user_id, capital)
        detail["portfolio_dd_pct"] = dd.get("dd_pct")
        if dd.get("breached"):
            return False, f"Portfolio drawdown {dd['dd_pct']:.1f}% breached limit", detail
    except Exception:
        pass

    # 9. Compute recommended size if entry/stop provided
    if entry_price and stop_price and entry_price > 0 and stop_price > 0:
        qty = size_position(capital, entry_price, stop_price, limits=limits)
        detail["recommended_quantity"] = qty
        if qty <= 0:
            return False, "Position size would be zero — risk too large", detail

    detail["allowed"] = True
    return True, "ok", detail
