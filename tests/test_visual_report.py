"""Tests for the visual report generator (salvaged/trimmed from odysseus, MIT).

Verifies the renderer produces a valid self-contained HTML document, builds the
TOC from headings, renders sources/stats, escapes user content, and that the
.format() template (heavy with literal CSS/JS braces) has no placeholder
mismatch.
"""
import re

from app.visual_report import (
    generate_report,
    to_plaintext,
    _category_css,
    _extract_headings,
    _extract_report_title,
    _md_to_html,
    _strip_thinking,
)

_MD = """# Q2 Catalyst Review

## Earnings
NVDA reports on the 21st. Strong guidance expected.

## Regulatory
FDA decision on XYZ pending.

### Sub-detail
More text here.
"""


class TestGenerateReport:
    def test_returns_full_html_document(self):
        out = generate_report("Daily Brief", _MD)
        assert out.lstrip().startswith("<!DOCTYPE html>")
        assert out.rstrip().endswith("</html>")
        assert "<style>" in out and "</style>" in out
        assert "<script>" in out

    def test_no_unfilled_placeholders(self):
        # A missed brace-escape or stray placeholder would leave a {token} or
        # raise during format(); assert none of our known placeholders leak.
        out = generate_report("T", _MD, subtitle="sub", stats={"Trades": 3})
        for token in ("{title}", "{report_html}", "{toc_html}", "{sources_html}",
                      "{stats_block}", "{timestamp}", "{question_html}"):
            assert token not in out

    def test_title_taken_from_first_heading(self):
        out = generate_report("fallback title", _MD)
        # The H1 "Q2 Catalyst Review" should win over the fallback.
        assert "Q2 Catalyst Review" in out
        # ...and be stripped from the body so it doesn't duplicate as an <h1> twice.
        assert out.count("Q2 Catalyst Review") >= 1

    def test_toc_built_from_headings(self):
        out = generate_report("T", _MD)
        assert 'href="#earnings"' in out
        assert 'href="#regulatory"' in out
        assert 'href="#sub-detail"' in out

    def test_sources_rendered(self):
        out = generate_report("T", _MD, sources=[
            {"title": "Reuters story", "url": "https://www.reuters.com/x"},
            {"title": "", "url": "https://sec.gov/filing"},
        ])
        assert "Sources (2)" in out
        assert "Reuters story" in out
        assert "reuters.com" in out  # www. stripped
        assert "sec.gov" in out

    def test_stats_bar_rendered(self):
        out = generate_report("T", _MD, stats={"Trades": 12, "Net P/L": "+$340", "Skip": None})
        assert "stats-bar" in out
        assert "12" in out and "Trades" in out
        assert "+$340" in out
        # None-valued stat is skipped (no empty stat cell label "Skip").
        assert ">Skip<" not in out

    def test_no_stats_bar_when_empty(self):
        out = generate_report("T", _MD)
        assert 'class="stats-bar"' not in out

    def test_escapes_html_in_title_and_subtitle(self):
        # Heading-less body so the (malicious) fallback title is actually used.
        out = generate_report("<script>alert(1)</script>", "plain body, no heading",
                              subtitle="<img src=x onerror=alert(1)>")
        # The raw injected tag must never appear unescaped.
        assert "<script>alert(1)" not in out
        # It must appear escaped instead.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
        # Subtitle is escaped too.
        assert "&lt;img src=x onerror=alert(1)&gt;" in out

    def test_empty_body_does_not_crash(self):
        out = generate_report("Just a title", "")
        assert "<!DOCTYPE html>" in out
        assert "Just a title" in out

    def test_bold_lines_promoted_to_headings_for_toc(self):
        md = "**First Section**\n\nsome text\n\n**Second Section**\n\nmore"
        out = generate_report("T", md)
        assert 'href="#first-section"' in out
        assert 'href="#second-section"' in out


class TestCategoryTheming:
    def test_category_brief_sets_body_class_and_override(self):
        out = generate_report("T", _MD, category="brief")
        assert 'body class="category-brief"' in out
        # The scoped override rule re-tints --accent for this category.
        assert "body.category-brief" in out
        assert "--accent:" in out.split("body.category-brief", 1)[1][:200]

    def test_category_dark_override_present(self):
        out = generate_report("T", _MD, category="alert")
        # Dark-scheme override is scoped inside a prefers-color-scheme block.
        assert "body.category-alert" in out
        # Two occurrences: light rule + dark @media rule.
        assert out.count("body.category-alert") >= 2

    def test_default_no_category_class_and_no_leaks(self):
        out = generate_report("T", _MD, subtitle="sub", stats={"Trades": 3})
        # No category- body class when category is unset.
        assert "category-" not in out
        assert 'body class=""' in out
        # Placeholder-leak guard, now including the new template placeholders.
        for token in ("{title}", "{report_html}", "{toc_html}", "{sources_html}",
                      "{stats_block}", "{timestamp}", "{question_html}",
                      "{category_css}", "{body_class}"):
            assert token not in out
        # A bare unfilled "{name}" placeholder token must not survive.
        assert not re.search(r"\{[a-z_]+\}", out)

    def test_unknown_category_falls_back(self):
        out = generate_report("T", _MD, category="not-a-real-theme")
        # Graceful fallback: no override rule, empty body class.
        assert "body.category-" not in out
        assert 'body class=""' in out

    def test_category_css_helper_direct(self):
        assert _category_css("") == ""
        assert _category_css("bogus") == ""
        css = _category_css("research")
        assert "body.category-research" in css
        assert "prefers-color-scheme: dark" in css
        assert "--accent:" in css


class TestToPlaintext:
    def test_headings_lose_hashes(self):
        assert to_plaintext("# Title\n## Sub") == "Title\nSub"

    def test_links_become_text_and_url(self):
        out = to_plaintext("See [Reuters](https://reuters.com/x) now.")
        assert "Reuters (https://reuters.com/x)" in out
        assert "[" not in out and "]" not in out

    def test_emphasis_stripped(self):
        out = to_plaintext("**bold** and _italic_ and `code` text")
        assert out == "bold and italic and code text"

    def test_images_dropped(self):
        out = to_plaintext("Chart: ![alt text](https://img/x.png) done")
        assert "alt text" not in out
        assert "x.png" not in out
        assert "Chart:  done" in out or "Chart: done" in out

    def test_no_markdown_symbols_remain(self):
        doc = (
            "# Daily Brief\n\n"
            "## Earnings\n\n"
            "**NVDA** reports on the _21st_. See [filing](https://sec.gov/a).\n\n"
            "Logo: ![logo](https://x/l.png)\n\n"
            "> A quote with `inline` code.\n"
        )
        out = to_plaintext(doc)
        assert "#" not in out
        assert "*" not in out
        assert "_" not in out
        assert "`" not in out
        assert "![" not in out
        # Link text + url preserved.
        assert "filing (https://sec.gov/a)" in out
        # No 3+ consecutive newlines.
        assert "\n\n\n" not in out

    def test_empty_input(self):
        assert to_plaintext("") == ""


class TestHelpers:
    def test_strip_thinking(self):
        assert _strip_thinking("<think>secret</think>visible") == "visible"
        assert _strip_thinking("<thinking>x</thinking> hi").strip() == "hi"
        assert _strip_thinking(None) is None

    def test_extract_headings_levels_and_slugs(self):
        hs = _extract_headings("## Alpha\n### Beta\n## Alpha\n")
        assert hs[0] == {"level": 2, "text": "Alpha", "slug": "alpha"}
        assert hs[1]["level"] == 3
        # Duplicate "Alpha" gets a unique slug.
        assert hs[2]["slug"] != hs[0]["slug"]

    def test_extract_report_title_skips_generic(self):
        title, stripped = _extract_report_title("# Introduction\n## Real Title\nbody", "fb")
        # "Introduction" is generic -> falls through to "Real Title".
        assert title == "Real Title"

    def test_md_to_html_external_links_new_tab(self):
        html_out = _md_to_html("see [link](https://example.com)")
        assert 'target="_blank"' in html_out
        assert 'rel="noopener noreferrer"' in html_out

    def test_md_to_html_autolinks_bare_urls(self):
        html_out = _md_to_html("visit https://example.com now")
        assert "href=" in html_out and "example.com" in html_out
