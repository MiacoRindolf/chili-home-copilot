"""Pure unit tests for app.services.trading_brief.build_brief.

No DB, no app boot beyond importing the module under test. The builder takes
already-fetched data, so these tests construct plain dicts inline.
"""
from __future__ import annotations

from app.services.trading_brief import build_brief


FULL_SUMMARY = {
    "date": "2026-06-01",
    "net_pnl": 340.12,
    "realized_pnl": 320.0,
    "closes": [
        {"ticker": "GRT-USD", "pnl": 22.1, "pattern": "585", "reason": "target"},
        {"ticker": "EKSO", "pnl": -8.5, "pattern": "412", "reason": "stop"},
    ],
    "open_positions": [
        {"ticker": "EKSO", "side": "long", "unrealized": -5.4},
        {"ticker": "GRT-USD", "side": "long", "unrealized": 12.0},
    ],
    "win_rate": 0.35,
    "payoff_ratio": 4.97,
    "top_patterns": [
        {"id": "585", "pnl": 554.0, "trades": 86},
        {"id": "412", "pnl": 120.0, "trades": 30},
    ],
    "notes": ["entry slippage averaged +102 bps", "no halts"],
    "sources": [{"title": "Audit log", "url": "https://example.com/audit"}],
}


# ---------------------------------------------------------------------------
# Output shape / renderer contract
# ---------------------------------------------------------------------------

EXPECTED_KEYS = {"title", "subtitle", "label", "markdown", "stats", "sources"}


def test_output_keys_match_renderer_kwargs():
    brief = build_brief(FULL_SUMMARY)
    # generate_report(title, body_markdown, *, subtitle, label, sources, stats)
    # -> title, markdown (body), subtitle, label, stats, sources
    assert EXPECTED_KEYS.issubset(brief.keys())
    assert brief["title"] == "Daily Trading Brief"
    assert brief["label"] == "CHILI — Daily Trading Brief"


def test_brief_keys_align_with_generate_report_signature():
    """The brief's keys must cover every kwarg generate_report consumes.

    We inspect the renderer's signature rather than invoking it, so this stays
    a pure unit test (no HTML render, no coupling to renderer internals).
    """
    import inspect

    from app import visual_report

    sig = inspect.signature(visual_report.generate_report)
    params = sig.parameters
    # Positional: title -> brief["title"], body_markdown -> brief["markdown"].
    assert "title" in params
    assert "body_markdown" in params
    # Keyword-only metadata the brief supplies.
    for kw in ("subtitle", "label", "stats", "sources"):
        assert kw in params

    brief = build_brief(FULL_SUMMARY)
    assert set(brief.keys()) >= {"title", "markdown", "subtitle", "label", "stats", "sources"}


# ---------------------------------------------------------------------------
# Full summary -> sections, tables, stats
# ---------------------------------------------------------------------------

def test_full_summary_section_headers():
    md = build_brief(FULL_SUMMARY)["markdown"]
    for header in (
        "## Performance",
        "## Closes",
        "## Open Positions",
        "## Top Patterns",
        "## Notes",
    ):
        assert header in md


def test_full_summary_contains_tickers_and_notes():
    md = build_brief(FULL_SUMMARY)["markdown"]
    assert "GRT-USD" in md
    assert "EKSO" in md
    assert "entry slippage averaged +102 bps" in md


def test_full_summary_has_valid_table_separator():
    md = build_brief(FULL_SUMMARY)["markdown"]
    # A GFM table separator row of dashes must be present.
    assert "| --- |" in md or "---" in md
    # Specifically the closes table header + separator.
    assert "| Ticker | P/L | Pattern | Reason |" in md
    assert "| --- | --- | --- | --- |" in md


def test_full_summary_stats_formatting():
    stats = build_brief(FULL_SUMMARY)["stats"]
    assert stats["Closes"] == 2
    assert stats["Net P/L"] == "+$340.12"
    assert stats["Win Rate"] == "35%"
    assert stats["Payoff"] == "4.97:1"


def test_subtitle_includes_date_and_net():
    brief = build_brief(FULL_SUMMARY)
    assert "2026-06-01" in brief["subtitle"]
    assert "+$340.12" in brief["subtitle"]


def test_payoff_ratio_rendered_in_markdown():
    md = build_brief(FULL_SUMMARY)["markdown"]
    assert "4.97:1" in md


# ---------------------------------------------------------------------------
# Empty / partial summaries
# ---------------------------------------------------------------------------

def test_empty_summary_does_not_crash():
    brief = build_brief({})
    assert EXPECTED_KEYS.issubset(brief.keys())
    assert isinstance(brief["markdown"], str)
    assert brief["markdown"]  # non-empty placeholder
    assert brief["stats"] == {}
    assert brief["sources"] == []
    assert brief["subtitle"] == ""


def test_partial_summary_only_net_pnl():
    brief = build_brief({"net_pnl": 100.0})
    assert brief["stats"] == {"Net P/L": "+$100.00"}
    # No closes / positions / patterns sections.
    md = brief["markdown"]
    assert "## Open Positions" not in md
    assert "## Top Patterns" not in md
    assert "## Closes" not in md
    # Performance section still present (net_pnl drives it).
    assert "## Performance" in md
    assert "+$100.00" in md


def test_omits_empty_sections():
    brief = build_brief({
        "net_pnl": 5.0,
        "closes": [],
        "open_positions": [],
        "top_patterns": [],
        "notes": [],
    })
    md = brief["markdown"]
    assert "## Open Positions" not in md
    assert "## Top Patterns" not in md
    assert "## Notes" not in md
    # Empty closes are reported explicitly rather than dropped silently.
    assert "## Closes" in md
    assert "No closes" in md


def test_stats_only_include_present_values():
    brief = build_brief({"win_rate": 0.5})
    assert brief["stats"] == {"Win Rate": "50%"}


# ---------------------------------------------------------------------------
# Money / negatives / zero closes
# ---------------------------------------------------------------------------

def test_negative_net_pnl_formats_with_minus():
    brief = build_brief({"net_pnl": -33.0})
    assert brief["stats"]["Net P/L"] == "-$33.00"
    assert "-$33.00" in brief["markdown"]


def test_thousands_separator_in_money():
    brief = build_brief({"net_pnl": 12345.6})
    assert brief["stats"]["Net P/L"] == "+$12,345.60"


def test_zero_closes_no_table():
    brief = build_brief({"closes": []})
    md = brief["markdown"]
    # No table header for closes, but an explicit no-closes line.
    assert "| Ticker | P/L | Pattern | Reason |" not in md
    assert "No closes" in md
    assert "Closes" not in brief["stats"]


def test_missing_closes_key_no_closes_section():
    brief = build_brief({"net_pnl": 1.0})
    assert "## Closes" not in brief["markdown"]


# ---------------------------------------------------------------------------
# Sources passthrough
# ---------------------------------------------------------------------------

def test_sources_passthrough():
    sources = [{"title": "X", "url": "https://x.test"}]
    brief = build_brief({"sources": sources})
    assert brief["sources"] == sources


def test_sources_default_empty_list():
    brief = build_brief({})
    assert brief["sources"] == []


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_non_dict_summary_degrades():
    brief = build_brief(None)  # type: ignore[arg-type]
    assert EXPECTED_KEYS.issubset(brief.keys())
    assert brief["stats"] == {}


def test_pipe_in_cell_is_escaped():
    brief = build_brief({"closes": [{"ticker": "A|B", "pnl": 1.0}]})
    assert "A\\|B" in brief["markdown"]


def test_markdown_leads_with_h1_title():
    # The brief must lead with an H1 title so visual_report.generate_report uses
    # it as the hero (it prefers a heading over the title kwarg and strips it),
    # rather than stealing the first "## Performance" heading.
    md = build_brief({"net_pnl": 10.0})["markdown"]
    assert md.lstrip().startswith("# Daily Trading Brief")


def test_brief_composes_with_visual_report():
    # End-to-end: the rendered HTML keeps the brief title as the hero AND keeps
    # the section headings (they aren't stolen as the title).
    from app import visual_report
    brief = build_brief({
        "date": "2026-06-01",
        "net_pnl": 340.12,
        "closes": [{"ticker": "GRT-USD", "pnl": 22.1, "pattern": "585", "reason": "target"}],
        "win_rate": 0.35,
    })
    html_out = visual_report.generate_report(
        brief["title"], brief["markdown"],
        subtitle=brief["subtitle"], label=brief["label"],
        stats=brief["stats"], sources=brief["sources"],
    )
    assert "Daily Trading Brief" in html_out      # hero title preserved
    assert "Performance" in html_out              # section NOT stolen as title
    assert "Closes" in html_out
    assert "GRT-USD" in html_out
    assert "+$340.12" in html_out
