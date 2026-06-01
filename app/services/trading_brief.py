"""Build a daily trading brief from a pre-fetched summary dict.

A *pure* report builder: it takes an already-fetched ``summary`` dict (no DB,
no network) and produces a markdown document plus metadata shaped for
``app.visual_report.generate_report(...)``.

The renderer's public API is::

    generate_report(title, body_markdown, *, subtitle="", label="CHILI Report",
                    sources=None, stats=None) -> str

so this module returns a dict whose keys line up with those kwargs
(``title``, ``markdown`` -> ``body_markdown``, ``subtitle``, ``label``,
``stats``, ``sources``). Callers typically splat it::

    brief = build_brief(summary)
    html = generate_report(
        brief["title"], brief["markdown"],
        subtitle=brief["subtitle"], label=brief["label"],
        stats=brief["stats"], sources=brief["sources"],
    )

Every key in ``summary`` is optional; missing or empty values degrade
gracefully (the corresponding section / stat is omitted) and never raise.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TITLE = "Daily Trading Brief"
LABEL = "CHILI — Daily Trading Brief"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to float, returning ``None`` if it can't be parsed."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_money(value: Any) -> Optional[str]:
    """Render a number as signed currency, e.g. 340.12 -> ``+$340.12``.

    Negatives render as ``-$33.00``; thousands get a separator. Returns
    ``None`` when the value isn't numeric so callers can omit the field.
    """
    amount = _to_float(value)
    if amount is None:
        return None
    sign = "-" if amount < 0 else "+"
    return f"{sign}${abs(amount):,.2f}"


def _format_pct(value: Any) -> Optional[str]:
    """Render a 0..1 fraction as a whole-number percent, e.g. 0.35 -> ``35%``."""
    fraction = _to_float(value)
    if fraction is None:
        return None
    return f"{round(fraction * 100)}%"


def _format_payoff(value: Any) -> Optional[str]:
    """Render a payoff ratio as ``4.97:1`` (two decimals)."""
    ratio = _to_float(value)
    if ratio is None:
        return None
    return f"{ratio:.2f}:1"


def _cell(value: Any) -> str:
    """Stringify a table cell, escaping pipes so the column doesn't break."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").strip()


def _money_cell(value: Any) -> str:
    """Money for a table cell; falls back to the raw string if non-numeric."""
    formatted = _format_money(value)
    if formatted is not None:
        return formatted
    return _cell(value)


def _table(headers: List[str], rows: List[List[str]]) -> str:
    """Build a GitHub-flavored markdown table (header + separator + rows)."""
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section builders — each returns a markdown block or None (omit the section)
# ---------------------------------------------------------------------------

def _performance_section(summary: Dict[str, Any]) -> Optional[str]:
    bits: List[str] = []
    net = _format_money(summary.get("net_pnl"))
    if net is not None:
        bits.append(f"- **Net P/L:** {net}")
    realized = _format_money(summary.get("realized_pnl"))
    if realized is not None:
        bits.append(f"- **Realized P/L:** {realized}")
    win_rate = _format_pct(summary.get("win_rate"))
    if win_rate is not None:
        bits.append(f"- **Win rate:** {win_rate}")
    payoff = _format_payoff(summary.get("payoff_ratio"))
    if payoff is not None:
        bits.append(f"- **Payoff ratio:** {payoff}")
    if not bits:
        return None
    return "## Performance\n\n" + "\n".join(bits)


def _closes_section(summary: Dict[str, Any]) -> Optional[str]:
    closes = summary.get("closes") or []
    if not closes:
        return None
    rows = []
    for close in closes:
        if not isinstance(close, dict):
            continue
        rows.append([
            _cell(close.get("ticker")),
            _money_cell(close.get("pnl")),
            _cell(close.get("pattern")),
            _cell(close.get("reason")),
        ])
    if not rows:
        return None
    table = _table(["Ticker", "P/L", "Pattern", "Reason"], rows)
    return "## Closes\n\n" + table


def _open_positions_section(summary: Dict[str, Any]) -> Optional[str]:
    positions = summary.get("open_positions") or []
    if not positions:
        return None
    rows = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        rows.append([
            _cell(pos.get("ticker")),
            _cell(pos.get("side")),
            _money_cell(pos.get("unrealized")),
        ])
    if not rows:
        return None
    table = _table(["Ticker", "Side", "Unrealized"], rows)
    return "## Open Positions\n\n" + table


def _top_patterns_section(summary: Dict[str, Any]) -> Optional[str]:
    patterns = summary.get("top_patterns") or []
    if not patterns:
        return None
    rows = []
    for pat in patterns:
        if not isinstance(pat, dict):
            continue
        payoff = _format_payoff(pat.get("payoff"))
        rows.append([
            _cell(pat.get("id")),
            _money_cell(pat.get("pnl")),
            _cell(pat.get("trades")),
            payoff if payoff is not None else "",
        ])
    if not rows:
        return None
    table = _table(["Pattern", "P/L", "Trades", "Payoff"], rows)
    return "## Top Patterns\n\n" + table


def _notes_section(summary: Dict[str, Any]) -> Optional[str]:
    notes = summary.get("notes") or []
    items = [str(n).strip() for n in notes if str(n).strip()]
    if not items:
        return None
    return "## Notes\n\n" + "\n".join(f"- {item}" for item in items)


# ---------------------------------------------------------------------------
# Stats / subtitle
# ---------------------------------------------------------------------------

def _build_stats(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Stats bar entries — only include those whose source value is present."""
    stats: Dict[str, Any] = {}

    closes = summary.get("closes")
    if closes:
        stats["Closes"] = len(closes)

    net = _format_money(summary.get("net_pnl"))
    if net is not None:
        stats["Net P/L"] = net

    win_rate = _format_pct(summary.get("win_rate"))
    if win_rate is not None:
        stats["Win Rate"] = win_rate

    payoff = _format_payoff(summary.get("payoff_ratio"))
    if payoff is not None:
        stats["Payoff"] = payoff

    return stats


def _build_subtitle(summary: Dict[str, Any]) -> str:
    """One-line subtitle: ``<date> · net P/L <formatted>`` (parts optional)."""
    parts: List[str] = []
    date = summary.get("date")
    if date:
        parts.append(str(date).strip())
    net = _format_money(summary.get("net_pnl"))
    if net is not None:
        parts.append(f"net P/L {net}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_brief(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a pre-fetched ``summary`` dict into a markdown brief + metadata.

    Args:
        summary: a plain dict with optional keys (``date``, ``net_pnl``,
            ``realized_pnl``, ``closes``, ``open_positions``, ``win_rate``,
            ``payoff_ratio``, ``top_patterns``, ``notes``, ``sources``). Every
            key is optional; missing/empty values omit their section/stat.

    Returns:
        A dict with ``title``, ``subtitle``, ``label``, ``markdown``, ``stats``
        and ``sources`` — keys aligned with ``generate_report`` kwargs.
    """
    if not isinstance(summary, dict):
        logger.warning("[trading_brief] summary is not a dict (%r); treating as empty", type(summary))
        summary = {}

    sections = [
        _performance_section(summary),
        _closes_section(summary),
        _open_positions_section(summary),
        _top_patterns_section(summary),
        _notes_section(summary),
    ]
    body_parts = [s for s in sections if s]

    if summary.get("closes") is not None and not summary.get("closes"):
        # Explicit, empty closes — say so rather than silently dropping it.
        if not _closes_section(summary):
            body_parts.append("## Closes\n\n_No closes today._")

    # Lead with the title as an H1 so visual_report.generate_report uses it as
    # the hero title (it prefers an h1/h2 heading over the `title` kwarg and
    # strips it from the body). Without this, generate_report would steal the
    # first "## Performance" heading as the title and drop that section.
    if not body_parts:
        markdown = f"# {TITLE}\n\n_No trading activity to report._"
    else:
        markdown = f"# {TITLE}\n\n" + "\n\n".join(body_parts)

    return {
        "title": TITLE,
        "subtitle": _build_subtitle(summary),
        "label": LABEL,
        "markdown": markdown,
        "stats": _build_stats(summary),
        "sources": summary.get("sources", []) or [],
    }
