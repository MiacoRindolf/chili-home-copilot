"""Best-effort Pine Script v5 export for CHILI ``ScanPattern`` rule JSON.

CHILI evaluates a flat indicator snapshot; TradingView uses series. Mapped
indicators follow CHILI's naming where possible. Unmapped conditions compile
to ``false`` with a comment so the script stays valid — replace manually for
parity tests.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

# Bump when export semantics change (helps verify the running server loaded this module).
PINE_EXPORT_REV = 4

# Maps indicator keys to (pine_var, declaration_line). Use {n} for regex-built keys.
_FIXED_DECLS: dict[str, tuple[str, str]] = {
    "rsi_14": ("v_rsi14", "v_rsi14 = ta.rsi(close, 14)"),
    "macd_hist": (
        "v_macd_hist",
        "[_macdL, _macdS, v_macd_hist] = ta.macd(close, 12, 26, 9)",
    ),
    "adx": ("v_adx", "v_adx = ta.adx(high, low, close, 14)"),
    "rel_vol": ("v_rel_vol", "v_rel_vol = volume / ta.sma(volume, 20)"),
    "ibs": (
        "v_ibs",
        "v_ibs = (close - low) / math.max(high - low, syminfo.mintick)",
    ),
}


def _regex_decl(key: str) -> tuple[str, str] | None:
    m = re.match(r"^rsi_(\d+)$", key)
    if m:
        n = int(m.group(1))
        vn = f"v_rsi{n}"
        return (vn, f"{vn} = ta.rsi(close, {n})")
    m = re.match(r"^ema_(\d+)$", key)
    if m:
        n = int(m.group(1))
        vn = f"v_ema{n}"
        return (vn, f"{vn} = ta.ema(close, {n})")
    m = re.match(r"^sma_(\d+)$", key)
    if m:
        n = int(m.group(1))
        vn = f"v_sma{n}"
        return (vn, f"{vn} = ta.sma(close, {n})")
    return None


def _series_expr(key: str, decl_vars: dict[str, str]) -> str | None:
    """Pine expression for the LHS of a comparison (or `close` for price)."""
    if key == "price":
        return "close"
    if key in decl_vars:
        return decl_vars[key]
    if key in _FIXED_DECLS:
        return _FIXED_DECLS[key][0]
    rx = _regex_decl(key)
    if rx:
        return rx[0]
    return None


def _ensure_decl(key: str, decl_lines: list[str], decl_vars: dict[str, str]) -> bool:
    """Append declaration if we can map this key. Returns False if unknown."""
    if key == "price":
        return True
    if key in decl_vars:
        return True
    if key in _FIXED_DECLS:
        vn, line = _FIXED_DECLS[key]
        if key not in decl_vars:
            decl_lines.append(line)
            decl_vars[key] = vn
        return True
    rx = _regex_decl(key)
    if rx:
        vn, line = rx
        if key not in decl_vars:
            decl_lines.append(line)
            decl_vars[key] = vn
        return True
    return False


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if v is None:
        return "na"
    return str(v)


def _resistance_retests_var_name(lookback: int, tol_pct: float) -> str:
    return f"v_rr_L{lookback}_t{int(round(float(tol_pct) * 100))}"


def _ensure_resistance_retests_decl(
    lookback: int,
    tol_pct: float,
    decl_lines: list[str],
    emitted_rr: dict[tuple[int, float], str],
    warnings: list[str],
    idx: int,
) -> str:
    """Declare CHILI-style retest proxy series once per (lookback, tolerance)."""
    key = (lookback, tol_pct)
    if key in emitted_rr:
        return emitted_rr[key]
    vn = _resistance_retests_var_name(lookback, tol_pct)
    decl_lines.append(f"{vn} = f_chili_resistance_retests({lookback}, {tol_pct})")
    emitted_rr[key] = vn
    warnings.append(
        f"Condition {idx}: resistance_retests uses a Pine proxy (ta.highest-based band touches); "
        "CHILI may use a different resistance level from its scanner — validate manually."
    )
    return vn


def _normalize_indicator_key(raw: Any) -> str:
    """Strip whitespace; map common aliases so CHILI / LLM typos still export."""
    s = str(raw or "").strip()
    if s in ("resistance_retest", "resistance_retest_count", "resistanceRetests"):
        return "resistance_retests"
    return s


def _normalize_op(raw: Any) -> str:
    o = str(raw or "").strip()
    if o in ("\u2265", "≥"):
        return ">="
    if o in ("\u2264", "≤"):
        return "<="
    return o


def _cond_to_pine(
    cond: dict[str, Any],
    decl_lines: list[str],
    decl_vars: dict[str, str],
    warnings: list[str],
    idx: int,
    emitted_rr: dict[tuple[int, float], str],
) -> str:
    """One boolean Pine sub-expression or ``false`` with comment if unmapped."""
    ind = _normalize_indicator_key(cond.get("indicator"))
    op = _normalize_op(cond.get("op"))
    value = cond.get("value")
    _ref = cond.get("ref")
    ref = str(_ref).strip() if _ref is not None else None
    if ref == "":
        ref = None

    params = cond.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if ind == "resistance_retests":
        lookback = int(params.get("lookback", 20))
        lookback = max(2, min(lookback, 500))
        tol_pct = float(params.get("tolerance_pct", 1.5))
        tol_pct = max(0.01, min(tol_pct, 50.0))
        vn = _ensure_resistance_retests_decl(
            lookback, tol_pct, decl_lines, emitted_rr, warnings, idx
        )
        try:
            threshold = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            threshold = 0.0
        if op == ">=":
            return f"({vn} >= {int(round(threshold))})"
        if op == ">":
            return f"({vn} > {int(round(threshold))})"
        if op == "<=":
            return f"({vn} <= {int(round(threshold))})"
        if op == "<":
            return f"({vn} < {int(round(threshold))})"
        if op == "==":
            return f"({vn} == {int(round(threshold))})"
        warnings.append(
            f"Condition {idx}: resistance_retests op {op!r} not translated to Pine."
        )
        return f"false  // UNMAPPED resistance_retests: {json.dumps(cond)}"

    if params:
        warnings.append(
            f"Condition {idx} ({ind}): params {params} are not translated to Pine "
            "(CHILI uses these in its own engine)."
        )

    lhs = _series_expr(ind, decl_vars)
    if lhs is None:
        warnings.append(
            f"Condition {idx}: indicator '{ind}' is not mapped to Pine — "
            "replace `cond_{idx}` manually."
        )
        return f"false  // UNMAPPED: {json.dumps(cond)}"

    if ref:
        if not _ensure_decl(ref, decl_lines, decl_vars):
            warnings.append(
                f"Condition {idx}: ref indicator '{ref}' is not mapped to Pine."
            )
            return f"false  // UNMAPPED ref: {ref}"
        rhs = _series_expr(ref, decl_vars)
        if rhs is None:
            return f"false  // UNMAPPED ref: {ref}"
        try:
            if op == ">":
                return f"({lhs} > {rhs})"
            if op == ">=":
                return f"({lhs} >= {rhs})"
            if op == "<":
                return f"({lhs} < {rhs})"
            if op == "<=":
                return f"({lhs} <= {rhs})"
            if op == "==":
                return f"({lhs} == {rhs})"
            if op == "!=":
                return f"({lhs} != {rhs})"
        except Exception:
            pass
        warnings.append(f"Condition {idx}: unsupported op '{op}' for ref comparison.")
        return f"false  // UNMAPPED: {json.dumps(cond)}"

    # value comparisons
    try:
        if op == ">":
            return f"({lhs} > {_format_value(value)})"
        if op == ">=":
            return f"({lhs} >= {_format_value(value)})"
        if op == "<":
            return f"({lhs} < {_format_value(value)})"
        if op == "<=":
            return f"({lhs} <= {_format_value(value)})"
        if op == "==":
            return f"({lhs} == {_format_value(value)})"
        if op == "!=":
            return f"({lhs} != {_format_value(value)})"
        if op == "between" and isinstance(value, list) and len(value) == 2:
            lo, hi = value[0], value[1]
            return f"({lhs} >= {float(lo)} and {lhs} <= {float(hi)})"
        if op == "any_of" and isinstance(value, list):
            parts = [f"{lhs} == {_format_value(x)}" for x in value]
            return "(" + " or ".join(parts) + ")"
        if op == "not_in" and isinstance(value, list):
            parts = [f"{lhs} != {_format_value(x)}" for x in value]
            return "(" + " and ".join(parts) + ")"
    except (TypeError, ValueError):
        pass

    warnings.append(f"Condition {idx}: could not translate op={op!r} value={value!r}.")
    return f"false  // UNMAPPED: {json.dumps(cond)}"


def _chili_exit_params_for_pine(
    conditions: list[dict[str, Any]],
    timeframe: str | None,
    exit_config_str: str | None,
) -> tuple[float, int, bool, str, int, float]:
    """Match ``run_pattern_backtest`` / ``DynamicPatternStrategy`` exit defaults.

    Returns ``(atr_mult, max_bars, use_bos, backtest_interval_key, bos_grace, bos_buffer_pct)``.

    ``bos_grace`` / ``bos_buffer_pct`` match ``exit_config`` keys ``bos_grace_bars`` /
    ``bos_buffer_pct`` when set; else defaults used by ``DynamicPatternStrategy.init``
    before vol-based tuning (Pine cannot replicate ATR/price tuning without OHLC).
    """
    bos_grace = 3
    bos_buf = 0.003
    try:
        from ..backtest_service import _classify_exit_params, get_backtest_params

        tf = (timeframe or "1d").strip()
        bp = get_backtest_params(tf)
        iv = str(bp.get("interval", "1d"))
        atr_m, max_b, use_bos = _classify_exit_params(conditions, timeframe=iv)
        if exit_config_str:
            try:
                ec = json.loads(exit_config_str)
                if ec.get("atr_mult") is not None:
                    atr_m = float(ec["atr_mult"])
                if ec.get("max_bars") is not None:
                    max_b = int(ec["max_bars"])
                if ec.get("use_bos") is not None:
                    use_bos = bool(ec["use_bos"])
                if ec.get("bos_grace_bars") is not None:
                    bos_grace = int(ec["bos_grace_bars"])
                elif ec.get("bos_grace") is not None:
                    bos_grace = int(ec["bos_grace"])
                if ec.get("bos_buffer_pct") is not None:
                    bos_buf = float(ec["bos_buffer_pct"])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return float(atr_m), int(max_b), bool(use_bos), iv, bos_grace, bos_buf
    except Exception:
        return 2.0, 25, True, "1d", 3, 0.003


def rules_json_to_pine(
    *,
    pattern_id: int,
    name: str,
    description: str | None,
    timeframe: str | None,
    rules_json: str,
    kind: Literal["strategy", "indicator"] = "strategy",
    trading_insight_id: int | None = None,
    exit_config_json: str | None = None,
) -> tuple[str, list[str]]:
    """Return (full Pine v5 script, warnings).

    ``kind="strategy"`` emits ``strategy()`` with entries/exits for Strategy Tester.
    ``kind="indicator"`` emits ``indicator()`` with plotshape and alertcondition.
    """
    warnings: list[str] = []
    try:
        rules = json.loads(rules_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return (
            "// Invalid rules_json\n",
            ["Could not parse rules_json as JSON."],
        )

    conditions: list[dict] = rules.get("conditions") or []
    if not conditions:
        return (
            "// No conditions in rules_json\n",
            ["Pattern has no conditions."],
        )

    decl_lines: list[str] = []
    decl_vars: dict[str, str] = {}

    keys_needed: set[str] = set()
    for c in conditions:
        ik = c.get("indicator")
        if ik:
            keys_needed.add(ik)
        r = c.get("ref")
        if r:
            keys_needed.add(r)

    for k in sorted(keys_needed):
        nk = _normalize_indicator_key(k)
        if nk == "price":
            continue
        if nk == "resistance_retests":
            continue
        if not _ensure_decl(nk, decl_lines, decl_vars):
            warnings.append(
                f"Indicator key '{k}' has no Pine mapping — conditions using it "
                "will be false until you add a series."
            )

    emitted_rr: dict[tuple[int, float], str] = {}
    parts: list[str] = []
    for i, cond in enumerate(conditions):
        parts.append(
            _cond_to_pine(cond, decl_lines, decl_vars, warnings, i + 1, emitted_rr)
        )

    rr_helper = ""
    if emitted_rr:
        # Must match backtest_service._compute_series_for_conditions (resistance_retests):
        # window [i-lookback : i] inclusive = lookback+1 bars; res = max(high window);
        # count j in window where high[j] >= res - res*tolPct/100.
        rr_helper = """
// CHILI-aligned resistance retests (see backtest_service._compute_series_for_conditions)
f_chili_resistance_retests(simple int lookback, simple float tolPct) =>
    int windowBars = lookback + 1
    float res = ta.highest(high, windowBars)
    float thr = res * tolPct / 100.0
    float band = res - thr
    int cnt = 0
    for j = 0 to lookback
        if high[j] >= band
            cnt += 1
    cnt

"""

    # Pine v5: do not break longSignal across lines with leading `and` — TradingView reports
    # "Mismatched input 'and' expecting 'end of line without line continuation'".
    if len(parts) == 1:
        signal_expr = parts[0]
    else:
        signal_expr = " and ".join(parts)

    safe_title = (name or "CHILI Pattern").replace('"', "'")
    tf_note = (timeframe or "chart").strip()
    desc_txt = (description or "").replace("\n", " ").strip()
    if len(desc_txt) > 200:
        desc_txt = desc_txt[:197] + "..."

    tf_line = (
        "// CHILI timeframe hint: "
        f"{tf_note} (use the same interval on the chart for comparison)\n"
    )
    ins_line = ""
    if trading_insight_id is not None:
        ins_line = f"// TradingInsight id={int(trading_insight_id)} (evidence card / export request)\n"
    header = f"""// CHILI Home Copilot — Pine export (scan_pattern_id={pattern_id})
// pine_export_rev={PINE_EXPORT_REV} (if missing, server is running old code)
{ins_line}// Name: {name}
{tf_line}"""
    if desc_txt:
        header += f"// {desc_txt}\n"

    decl_block = "\n".join(decl_lines) if decl_lines else "// (no precomputed series — price-only or unmapped)"

    atr_m, max_b, use_bos_chili, bt_iv, bos_grace, bos_buf = _chili_exit_params_for_pine(
        conditions, timeframe, exit_config_json
    )
    bos_note = (
        f"BOS approx: pivotlow(10,10) + grace={bos_grace} buffer={bos_buf} "
        "(matches exit_config; CHILI may override grace/buffer via ATR/price when unset)."
        if use_bos_chili
        else "BOS disabled in CHILI exit_config for this pattern."
    )

    if kind == "strategy":
        script = f"""//@version=5
{rr_helper}strategy("CHILI: {safe_title}", overlay=true, initial_capital=100000, default_qty_type=strategy.percent_of_equity, default_qty_value=100, commission_type=strategy.commission.percent, commission_value=0.1, process_orders_on_close=true, pyramiding=1)

{header}
// Exits aligned with CHILI DynamicPatternStrategy (ATR trailing + max hold + optional BOS). Backtest interval key: {bt_iv}
// atr_mult={atr_m} max_bars={max_b} use_bos(CHILI)={str(use_bos_chili).lower()} bos_grace={bos_grace} bos_buffer_pct={bos_buf} — {bos_note}
// ATR: CHILI uses SMA(true range, 14) — same as ta.sma(ta.tr, 14), not Wilder ta.atr(14).
// BOS: CHILI _compute_swing_lows(lookback=10) ≈ ta.pivotlow(low, 10, 10) + carried last swing.
// Entry signals use the same rules_json AND chain as CHILI bar snapshots (see pattern_engine / backtest_service).

// --- Series (match CHILI flat keys where possible) ---
{decl_block}

// --- CHILI exit parameters (same defaults as run_pattern_backtest / exit_config overrides) ---
chili_atrMult = {atr_m}
chili_maxBars = {max_b}
chiliUseBos = {str(use_bos_chili).lower()}
chiliBosGrace = {bos_grace}
chiliBosBuf = {bos_buf}
lbBos = 10
// Match backtest_service._compute_atr_series: rolling mean of true range (period 14)
atrV = ta.sma(ta.tr, 14)
plBos = ta.pivotlow(low, lbBos, lbBos)
var float chiliLastSwing = na
if chiliUseBos and not na(plBos)
    chiliLastSwing := plBos

// --- Conditions (ANDed; mirrors CHILI _eval_condition_bt / indicator series) ---
longSignal = {signal_expr}
longEntry = longSignal and not longSignal[1]

var float chili_hi = na
var int chili_eb = na

inPos = strategy.position_size > 0

if longEntry
    strategy.entry("Chili", strategy.long, comment="CHILI")
    chili_hi := close
    chili_eb := bar_index
else if inPos
    chili_hi := math.max(chili_hi, close)
    barsHeld = bar_index - chili_eb
    trail = chili_hi - chili_atrMult * nz(atrV, 0.0)
    stopHit = close < trail
    timeHit = barsHeld >= chili_maxBars
    bosHit = chiliUseBos and barsHeld >= chiliBosGrace and not na(chiliLastSwing) and close < chiliLastSwing * (1.0 - chiliBosBuf)
    if stopHit or timeHit or bosHit
        exitReason = bosHit ? "bos" : (stopHit ? "atr trail" : "max bars")
        strategy.close("Chili", comment=exitReason)

if not inPos
    chili_hi := na
    chili_eb := na
"""
        warnings.append(
            "Pine strategy: entries match rules_json; exits use SMA(TR,14)*atr_mult + max_bars + optional BOS "
            "(grace/buffer from exit_config or CHILI defaults; CHILI may tune BOS from volatility when unset). "
            "Data feed/session still differ from CHILI — validate on the same symbol/interval."
        )
    else:
        script = f"""//@version=5
{rr_helper}indicator("CHILI: {safe_title}", overlay=true, max_labels_count=500)

{header}
// --- Series (match CHILI flat keys where possible) ---
{decl_block}

// --- Conditions (ANDed; mirrors CHILI rule JSON) ---
longSignal = {signal_expr}

plotshape(longSignal, title="CHILI match", style=shape.triangleup, location=location.belowbar, size=size.small, color=color.new(color.teal, 0), text="CHILI")
alertcondition(longSignal, title="CHILI pattern match", message="CHILI: {safe_title}")
"""
        warnings.append(
            "Pine indicator export: CHILI and TradingView differ on data feeds, "
            "session, and custom indicators. Validate manually."
        )
    return script, warnings


def scan_pattern_to_pine(
    pattern,
    kind: Literal["strategy", "indicator"] = "strategy",
    *,
    trading_insight_id: int | None = None,
) -> tuple[str, list[str]]:
    """``pattern`` is a SQLAlchemy ``ScanPattern`` instance."""
    return rules_json_to_pine(
        pattern_id=int(pattern.id),
        name=getattr(pattern, "name", "") or "Pattern",
        description=getattr(pattern, "description", None),
        timeframe=getattr(pattern, "timeframe", None),
        rules_json=getattr(pattern, "rules_json", None) or "{}",
        kind=kind,
        trading_insight_id=trading_insight_id,
        exit_config_json=getattr(pattern, "exit_config", None),
    )
