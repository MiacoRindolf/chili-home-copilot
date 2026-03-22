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


def _cond_to_pine(
    cond: dict[str, Any],
    decl_lines: list[str],
    decl_vars: dict[str, str],
    warnings: list[str],
    idx: int,
) -> str:
    """One boolean Pine sub-expression or ``false`` with comment if unmapped."""
    ind = cond.get("indicator") or ""
    op = (cond.get("op") or "").strip()
    value = cond.get("value")
    ref = cond.get("ref")

    params = cond.get("params")
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


def rules_json_to_pine(
    *,
    pattern_id: int,
    name: str,
    description: str | None,
    timeframe: str | None,
    rules_json: str,
    kind: Literal["strategy", "indicator"] = "strategy",
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
        if k == "price":
            continue
        if not _ensure_decl(k, decl_lines, decl_vars):
            warnings.append(
                f"Indicator key '{k}' has no Pine mapping — conditions using it "
                "will be false until you add a series."
            )

    parts: list[str] = []
    for i, cond in enumerate(conditions):
        parts.append(_cond_to_pine(cond, decl_lines, decl_vars, warnings, i + 1))

    if len(parts) == 1:
        signal_expr = parts[0]
    else:
        signal_expr = "\n    and ".join(parts)

    safe_title = (name or "CHILI Pattern").replace('"', "'")
    tf_note = (timeframe or "chart").strip()
    desc_txt = (description or "").replace("\n", " ").strip()
    if len(desc_txt) > 200:
        desc_txt = desc_txt[:197] + "..."

    tf_line = (
        "// CHILI timeframe hint: "
        f"{tf_note} (use the same interval on the chart for comparison)\n"
    )
    header = f"""// CHILI Home Copilot — Pine export (pattern id={pattern_id})
// Name: {name}
{tf_line}"""
    if desc_txt:
        header += f"// {desc_txt}\n"

    decl_block = "\n".join(decl_lines) if decl_lines else "// (no precomputed series — price-only or unmapped)"

    if kind == "strategy":
        script = f"""//@version=5
strategy("CHILI: {safe_title}", overlay=true, initial_capital=100000, default_qty_type=strategy.percent_of_equity, default_qty_value=100, commission_type=strategy.commission.percent, commission_value=0.1, process_orders_on_close=true, pyramiding=1)

{header}
// Strategy Tester export — long on rising edge of ANDed conditions; flat when signal turns off.
// CHILI backtests may use ATR / max-hold exits; this script does not — P&L will not match CHILI.

// --- Series (match CHILI flat keys where possible) ---
{decl_block}

// --- Conditions (ANDed; mirrors CHILI rule JSON) ---
longSignal = {signal_expr}
longEntry = longSignal and not longSignal[1]
longExit = longSignal[1] and not longSignal

if longEntry
    strategy.entry("Chili", strategy.long)

if longExit
    strategy.close("Chili", comment="signal off")
"""
        warnings.append(
            "Pine strategy export for TradingView Strategy Tester: entries/exits follow "
            "signal on/off only; CHILI may use different exits and sizing. "
            "Feeds, session, and unmapped indicators differ — validate manually."
        )
    else:
        script = f"""//@version=5
indicator("CHILI: {safe_title}", overlay=true, max_labels_count=500)

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
    pattern, kind: Literal["strategy", "indicator"] = "strategy"
) -> tuple[str, list[str]]:
    """``pattern`` is a SQLAlchemy ``ScanPattern`` instance."""
    return rules_json_to_pine(
        pattern_id=int(pattern.id),
        name=getattr(pattern, "name", "") or "Pattern",
        description=getattr(pattern, "description", None),
        timeframe=getattr(pattern, "timeframe", None),
        rules_json=getattr(pattern, "rules_json", None) or "{}",
        kind=kind,
    )
