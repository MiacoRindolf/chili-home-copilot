"""Generate a self-contained, styled HTML report from markdown.

Turns a markdown report (research summary, daily trading brief, audit writeup)
into an editorial-quality, single-file HTML document — no external assets, no
backend calls — that the operator can open or share offline:

- System/local typography (no remote font provider), dark/light via
  prefers-color-scheme, subtle animated aurora background
- Auto-generated table-of-contents sidebar from h2/h3 headings
- Collapsible sources list, optional stats bar
- Print / "Download HTML" toolbar (pure client-side, no server)

Salvaged and trimmed (MIT) from the `odysseus` project
(https://github.com/pewdiepie-archdaemon/odysseus), `src/visual_report.py`. The
odysseus-specific machinery (OG-image reroll/hide wired to /api/research
endpoints, chat-spinoff CTA, session-id plumbing, per-category palettes) was
dropped; what remains is the self-contained markdown→HTML renderer + CSS.

Public API:
    generate_report(title, body_markdown, *, subtitle="", sources=None,
                    stats=None, category="") -> str   # complete HTML document
    to_plaintext(markdown_text) -> str   # strip markdown to readable plain text
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# `markdown` is a small pure-Python dep (declared in requirements). Guard the
# import so a stripped environment degrades to a minimal regex renderer rather
# than crashing.
try:
    import markdown as _markdown  # type: ignore
    _HAS_MARKDOWN = True
except Exception:  # pragma: no cover - defensive
    _markdown = None  # type: ignore
    _HAS_MARKDOWN = False

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:  # pragma: no cover - defensive
    BeautifulSoup = None  # type: ignore
    _HAS_BS4 = False

_GENERIC_HEADINGS = {
    "executive summary", "introduction", "overview", "summary", "conclusion",
    "background", "abstract", "contents", "table of contents",
}


# ---------------------------------------------------------------------------
# Markdown / text helpers
# ---------------------------------------------------------------------------

def _strip_thinking(text: Optional[str]) -> Optional[str]:
    """Remove <think>/<thinking> reasoning blocks an LLM may have emitted."""
    if text is None:
        return None
    return re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text).strip()


def _autolink_urls(md_text: str) -> str:
    """Convert bare URLs to markdown links (skip ones already in link syntax)."""
    return re.sub(r"(?<!\]\()(?<!\()(https?://[^\s\)<>]+)", r"[\1](\1)", md_text)


def _basic_md_to_html(md_text: str) -> str:
    """Very small markdown fallback when the `markdown` lib is unavailable."""
    out_lines = []
    for line in md_text.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            lvl = len(h.group(1))
            out_lines.append(f"<h{lvl}>{html.escape(h.group(2).strip())}</h{lvl}>")
            continue
        if line.strip():
            esc = html.escape(line)
            esc = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", esc)
            esc = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                         r'<a target="_blank" rel="noopener noreferrer" href="\2">\1</a>', esc)
            out_lines.append(f"<p>{esc}</p>")
    return "\n".join(out_lines)


def _md_to_html(md_text: str) -> str:
    """Convert markdown to HTML, opening external links in a new tab."""
    md_text = _autolink_urls(md_text)
    if _HAS_MARKDOWN:
        result = _markdown.markdown(
            md_text,
            extensions=["extra", "sane_lists", "tables"],
        )
    else:
        result = _basic_md_to_html(md_text)
    return re.sub(
        r'<a href="(https?://)',
        r'<a target="_blank" rel="noopener noreferrer" href="\1',
        result,
    )


def _extract_headings(md_text: str) -> List[Dict[str, str]]:
    """Pull h2/h3 headings from markdown for the table of contents."""
    headings: List[Dict[str, str]] = []
    seen_slugs: Dict[str, int] = {}

    def _plain(text: str) -> str:
        text = text.strip().rstrip("#").strip()
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"[`*_~]+", "", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    def _slug(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}-{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 0
        return slug

    for m in re.finditer(r"^(#{2,3})\s+(.+)$", md_text, re.MULTILINE):
        text = _plain(m.group(2))
        if text:
            headings.append({"level": len(m.group(1)), "text": text, "slug": _slug(text)})
    if not headings:
        for m in re.finditer(r"^\*\*([^*]+)\*\*\s*$", md_text, re.MULTILINE):
            text = _plain(m.group(1)).rstrip(":")
            if 3 < len(text) < 80:
                headings.append({"level": 2, "text": text, "slug": _slug(text)})
    return headings


def _apply_heading_ids(report_html: str, headings: List[Dict[str, str]]) -> str:
    """Force rendered h2/h3 ids to match the sidebar anchor slugs."""
    if not headings or not _HAS_BS4:
        return report_html
    try:
        soup = BeautifulSoup(report_html, "html.parser")
        for element, heading in zip(soup.find_all(["h2", "h3"]), headings):
            element["id"] = heading["slug"]
        return str(soup)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[visual_report] heading id apply failed: %s", e)
        return report_html


def _extract_report_title(md_text: str, fallback: str):
    """Use the report's first non-generic heading as the title; strip it from
    the body so it doesn't duplicate the hero. Returns (title, stripped_md)."""
    if not md_text:
        return fallback, md_text
    candidates = []
    for level, pattern in ((1, r"^# +(.+?)\s*$"), (2, r"^## +(.+?)\s*$")):
        for m in re.finditer(pattern, md_text, re.MULTILINE):
            cand = m.group(1).strip().rstrip("#").strip()
            if cand and cand.lower() not in _GENERIC_HEADINGS:
                candidates.append((level, m, cand))
    candidates.sort(key=lambda t: (t[0], t[1].start()))
    if candidates:
        _level, match, title = candidates[0]
        stripped = md_text[:match.start()] + md_text[match.end():]
        return title, stripped.lstrip()
    return fallback, md_text


# ---------------------------------------------------------------------------
# Category theming (re-tint the accent palette only)
# ---------------------------------------------------------------------------

# Per-category accent palettes. Each entry overrides only the color variables
# (palette, not structure) for light + dark. Keys mirror trading-relevant report
# kinds. Unknown/empty categories fall through to the default :root palette.
#   light: (--accent, --accent-light, --accent-bg, --aurora-a/b/c)
#   dark:  (--accent, --accent-light, --accent-bg, --aurora-a/b/c)
_CATEGORY_PALETTES: Dict[str, Dict[str, Dict[str, str]]] = {
    # Daily trading brief — calm slate blue.
    "brief": {
        "light": {
            "accent": "#3d6a99", "accent-light": "#5e8bbd",
            "accent-bg": "rgba(61,106,153,0.07)",
            "aurora-a": "rgba(61,106,153,0.10)", "aurora-b": "rgba(94,139,189,0.08)",
            "aurora-c": "rgba(64,98,128,0.07)",
        },
        "dark": {
            "accent": "#73a8e8", "accent-light": "#95c0f4",
            "accent-bg": "rgba(115,168,232,0.10)",
            "aurora-a": "rgba(115,168,232,0.13)", "aurora-b": "rgba(149,192,244,0.09)",
            "aurora-c": "rgba(125,180,224,0.10)",
        },
    },
    # Research / deep-dive — teal-green.
    "research": {
        "light": {
            "accent": "#2f8f7f", "accent-light": "#4fb3a1",
            "accent-bg": "rgba(47,143,127,0.07)",
            "aurora-a": "rgba(47,143,127,0.10)", "aurora-b": "rgba(79,179,161,0.08)",
            "aurora-c": "rgba(64,128,112,0.07)",
        },
        "dark": {
            "accent": "#5fd4bf", "accent-light": "#88e3d2",
            "accent-bg": "rgba(95,212,191,0.10)",
            "aurora-a": "rgba(95,212,191,0.13)", "aurora-b": "rgba(136,227,210,0.09)",
            "aurora-c": "rgba(112,200,180,0.10)",
        },
    },
    # Audit / reconciliation — muted gold.
    "audit": {
        "light": {
            "accent": "#a8842c", "accent-light": "#c9a64f",
            "accent-bg": "rgba(168,132,44,0.07)",
            "aurora-a": "rgba(168,132,44,0.10)", "aurora-b": "rgba(201,166,79,0.08)",
            "aurora-c": "rgba(140,110,40,0.07)",
        },
        "dark": {
            "accent": "#e0c05a", "accent-light": "#ecd283",
            "accent-bg": "rgba(224,192,90,0.10)",
            "aurora-a": "rgba(224,192,90,0.13)", "aurora-b": "rgba(236,210,131,0.09)",
            "aurora-c": "rgba(200,170,90,0.10)",
        },
    },
    # Alert / risk — assertive red.
    "alert": {
        "light": {
            "accent": "#c23b2e", "accent-light": "#dd6052",
            "accent-bg": "rgba(194,59,46,0.07)",
            "aurora-a": "rgba(194,59,46,0.10)", "aurora-b": "rgba(221,96,82,0.08)",
            "aurora-c": "rgba(160,50,40,0.07)",
        },
        "dark": {
            "accent": "#f07568", "accent-light": "#f6968b",
            "accent-bg": "rgba(240,117,104,0.10)",
            "aurora-a": "rgba(240,117,104,0.13)", "aurora-b": "rgba(246,150,139,0.09)",
            "aurora-c": "rgba(220,100,90,0.10)",
        },
    },
}


def _palette_block(vars_: Dict[str, str]) -> str:
    """Render the palette CSS variable declarations for one scheme."""
    return " ".join(f"--{name}: {value};" for name, value in vars_.items())


def _category_css(category: str) -> str:
    """Return a small CSS string re-tinting the accent palette for `category`.

    Overrides only the color variables (--accent, --accent-light, --accent-bg,
    --aurora-a/b/c), scoped to `body.category-<name>` so it applies only when
    the matching category is set, for both light and dark schemes. Unknown or
    empty categories return an empty string (default palette unchanged).
    """
    palette = _CATEGORY_PALETTES.get((category or "").strip().lower())
    if not palette:
        return ""
    name = category.strip().lower()
    light = _palette_block(palette["light"])
    dark = _palette_block(palette["dark"])
    return (
        f"body.category-{name} {{ {light} }}\n"
        f"@media (prefers-color-scheme: dark) {{ "
        f"body.category-{name} {{ {dark} }} }}"
    )


# ---------------------------------------------------------------------------
# HTML template (trimmed from odysseus — self-contained, no backend calls)
# ---------------------------------------------------------------------------

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<meta name="theme-color" content="#b8543a" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#131214" media="(prefers-color-scheme: dark)">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='75' font-size='75'>&#127798;</text></svg>">
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --font-display: 'Charter', 'Iowan Old Style', Georgia, serif;
  --font-body: system-ui, -apple-system, 'Segoe UI', sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --bg: #fbf9f4; --bg-surface: #ffffff; --bg-surface-alt: #f1ede4;
  --border: rgba(0,0,0,0.08); --border-strong: rgba(0,0,0,0.16);
  --text: #1a1817; --text-dim: #5a5651; --text-muted: #8a8580;
  --accent: #b8543a; --accent-light: #d97a5e; --accent-bg: rgba(184,84,58,0.06);
  --gold: #c9952e; --gold-bg: rgba(201,149,46,0.09);
  --aurora-a: rgba(184,84,58,0.10); --aurora-b: rgba(201,149,46,0.08); --aurora-c: rgba(64,98,128,0.07);
  --radius: 12px; --shadow-sm: 0 1px 3px rgba(0,0,0,0.05); --shadow-md: 0 4px 24px rgba(0,0,0,0.07);
  --max-w: 760px;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #131214; --bg-surface: #1c1a1e; --bg-surface-alt: #25232a;
    --border: rgba(255,255,255,0.07); --border-strong: rgba(255,255,255,0.16);
    --text: #ece8e2; --text-dim: #a8a39c; --text-muted: #6f6b66;
    --accent: #e88f73; --accent-light: #f4ad95; --accent-bg: rgba(232,143,115,0.09);
    --gold: #e8c05a; --gold-bg: rgba(232,192,90,0.09);
    --aurora-a: rgba(232,143,115,0.13); --aurora-b: rgba(232,192,90,0.09); --aurora-c: rgba(125,180,224,0.10);
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.4); --shadow-md: 0 4px 28px rgba(0,0,0,0.55);
  }}
}}
html {{ scroll-behavior: smooth; scroll-padding-top: 4rem; }}
body {{
  font-family: var(--font-body); background: var(--bg); color: var(--text);
  line-height: 1.75; font-size: 17px; -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility; position: relative; min-height: 100vh;
}}
body::before {{
  content: ''; position: fixed; inset: -20vh -20vw; z-index: -2;
  background:
    radial-gradient(40vw 50vh at 18% 22%, var(--aurora-a) 0%, transparent 60%),
    radial-gradient(45vw 55vh at 82% 12%, var(--aurora-b) 0%, transparent 65%),
    radial-gradient(55vw 60vh at 50% 88%, var(--aurora-c) 0%, transparent 70%);
  filter: blur(20px); animation: aurora-drift 28s ease-in-out infinite alternate; pointer-events: none;
}}
@keyframes aurora-drift {{
  0% {{ transform: translate3d(0,0,0) scale(1); }}
  50% {{ transform: translate3d(2vw,-1vh,0) scale(1.04); }}
  100% {{ transform: translate3d(-1vw,1.5vh,0) scale(1.02); }}
}}
@media (prefers-reduced-motion: reduce) {{ body::before {{ animation: none; }} }}
.toolbar {{ position: fixed; top: 1rem; right: 1rem; z-index: 100; display: flex; gap: 0.4rem; opacity: 0.7; transition: opacity 0.2s; }}
.toolbar:hover {{ opacity: 1; }}
.toolbar button {{
  display: inline-flex; align-items: center; gap: 5px; padding: 6px 14px;
  border: 1px solid var(--border-strong); border-radius: 8px; background: var(--bg-surface);
  color: var(--text); font-family: inherit; font-size: 0.78rem; font-weight: 500;
  cursor: pointer; box-shadow: var(--shadow-sm); transition: background 0.15s; position: relative;
}}
.toolbar button:hover {{ background: var(--bg-surface-alt); }}
.toolbar button svg {{ width: 14px; height: 14px; flex-shrink: 0; }}
.dropdown {{ position: relative; }}
.dropdown-menu {{
  display: none; position: absolute; top: calc(100% + 4px); right: 0; background: var(--bg-surface);
  border: 1px solid var(--border-strong); border-radius: 8px; box-shadow: var(--shadow-md);
  overflow: hidden; min-width: 140px;
}}
.dropdown-menu.open {{ display: block; }}
.dropdown-menu button {{
  display: block; width: 100%; padding: 8px 14px; border: none; background: none;
  color: var(--text); font-family: inherit; font-size: 0.8rem; text-align: left; cursor: pointer;
}}
.dropdown-menu button:hover {{ background: var(--bg-surface-alt); }}
.hero {{ position: relative; padding: 5.5rem 2rem 2.5rem; text-align: center; overflow: hidden; }}
.hero::after {{
  content: ''; position: absolute; left: 50%; bottom: 0; width: min(60%, 320px); height: 1px;
  transform: translateX(-50%); background: linear-gradient(90deg, transparent, var(--border-strong), transparent);
}}
.hero-label {{
  text-transform: uppercase; letter-spacing: 0.28em; font-size: 0.68rem; font-weight: 600;
  color: var(--accent); opacity: 0.85; margin-bottom: 1.4rem;
}}
.hero h1 {{
  font-family: var(--font-display); font-size: clamp(2rem, 4.5vw, 3rem); font-weight: 600;
  line-height: 1.15; max-width: 720px; margin: 0 auto; letter-spacing: -0.02em; color: var(--text);
}}
.hero .subtitle {{ margin: 0.9rem auto 0; max-width: 620px; color: var(--text-dim); font-size: 1rem; }}
.stats-bar {{
  display: flex; justify-content: center; gap: 1.5rem; flex-wrap: wrap; padding: 0.9rem 2rem;
  background: var(--bg-surface); border-bottom: 1px solid var(--border); font-size: 0.82rem; color: var(--text-dim);
}}
.stat {{ display: flex; align-items: center; gap: 0.35rem; }}
.stat-value {{ font-weight: 600; color: var(--text); }}
.layout {{ display: grid; grid-template-columns: 200px 1fr; max-width: calc(var(--max-w) + 260px); margin: 0 auto; }}
@media (max-width: 900px) {{ .layout {{ grid-template-columns: 1fr; }} .toc-sidebar {{ display: none; }} }}
.toc-sidebar {{ position: sticky; top: 0; height: 100vh; overflow-y: auto; padding: 3.2rem 0.8rem 2rem 1.4rem; border-right: 1px solid var(--border); font-size: 0.78rem; }}
.toc-sidebar nav a {{
  position: relative; display: block; color: var(--text-dim); text-decoration: none;
  padding: 0.42rem 0.7rem 0.42rem 0.85rem; margin: 1px 0; border-radius: 6px; line-height: 1.4;
  transition: color 0.18s, background 0.18s, padding-left 0.18s;
}}
.toc-sidebar nav a:hover {{ color: var(--text); background: var(--accent-bg); padding-left: 1rem; }}
.toc-sidebar nav a.active {{ color: var(--accent); font-weight: 600; background: var(--accent-bg); }}
.toc-sidebar nav a.depth-3 {{ padding-left: 1.3rem; font-size: 0.72rem; color: var(--text-muted); }}
.content {{ max-width: var(--max-w); padding: 3rem 2.5rem 4rem; }}
.content h2 {{
  font-family: var(--font-display); font-size: clamp(1.55rem, 2.4vw, 1.85rem); font-weight: 600;
  margin: 3rem 0 1rem; padding-bottom: 0.55rem; border-bottom: 1px solid transparent;
  border-image: linear-gradient(90deg, var(--accent) 0%, transparent 65%) 1; letter-spacing: -0.022em; line-height: 1.2;
}}
.content h2:first-child {{ margin-top: 0; }}
.content h3 {{ font-family: var(--font-display); font-size: 1.22rem; font-weight: 600; margin: 2.2rem 0 0.6rem; letter-spacing: -0.015em; }}
.content h4 {{ font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.12em; color: var(--text-dim); margin: 1.6rem 0 0.5rem; }}
.content p {{ margin-bottom: 1.1rem; }}
.content a {{ color: var(--accent); text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--accent) 35%, transparent); text-underline-offset: 3px; }}
.content a:hover {{ text-decoration-color: var(--accent); color: var(--accent-light); }}
.content ul, .content ol {{ margin: 0 0 1.1rem 1.6rem; }}
.content li {{ margin-bottom: 0.4rem; }}
.content li::marker {{ color: var(--accent); }}
.content blockquote {{
  border-left: 3px solid var(--gold); background: var(--gold-bg); padding: 1.1rem 1.4rem; margin: 1.5rem 0;
  border-radius: 0 var(--radius) var(--radius) 0; font-family: var(--font-display); font-style: italic; font-size: 1.05rem;
}}
.content hr {{ border: none; height: 1px; background: linear-gradient(90deg, transparent, var(--border-strong), transparent); margin: 2rem 0; }}
.content code {{ font-family: var(--font-mono); font-size: 0.86em; background: var(--bg-surface-alt); padding: 0.15em 0.4em; border-radius: 4px; }}
.content pre {{ background: var(--bg-surface-alt); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.25rem 1.5rem; overflow-x: auto; margin: 1.25rem 0; font-size: 0.86rem; line-height: 1.6; }}
.content pre code {{ background: none; padding: 0; }}
.content table {{ width: 100%; border-collapse: collapse; margin: 1.25rem 0; font-size: 0.9rem; border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow-sm); }}
.content th {{ text-align: left; padding: 0.7rem 1rem; background: var(--accent-bg); font-weight: 600; border-bottom: 2px solid var(--border-strong); }}
.content td {{ padding: 0.6rem 1rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
.content tr:last-child td {{ border-bottom: none; }}
.sources-panel {{ margin-top: 3rem; border-top: 2px solid var(--border); padding-top: 1.5rem; }}
.sources-panel summary {{ display: flex; align-items: center; gap: 0.5rem; cursor: pointer; font-size: 1rem; font-weight: 600; color: var(--text); padding: 0.5rem 0; list-style: none; }}
.sources-panel summary::-webkit-details-marker {{ display: none; }}
.sources-panel summary::before {{ content: '\\25B6'; font-size: 0.65em; color: var(--text-muted); transition: transform 0.2s; }}
.sources-panel details[open] summary::before {{ transform: rotate(90deg); }}
.sources-list a {{ display: flex; align-items: baseline; gap: 0.5rem; padding: 0.35rem 0; font-size: 0.85rem; color: var(--text); text-decoration: none; }}
.sources-list a:hover {{ color: var(--accent); }}
.sources-list .snum {{ color: var(--text-muted); font-size: 0.75rem; min-width: 1.5rem; text-align: right; flex-shrink: 0; }}
.sources-list .sdomain {{ color: var(--text-muted); font-size: 0.75rem; margin-left: auto; flex-shrink: 0; }}
.report-footer {{ text-align: center; padding: 2rem; font-size: 0.75rem; color: var(--text-muted); border-top: 1px solid var(--border); margin-top: 2rem; }}
@media print {{ .toc-sidebar, .toolbar {{ display: none !important; }} .layout {{ grid-template-columns: 1fr; }} }}
{category_css}
</style>
</head>
<body class="{body_class}">
<div class="toolbar">
  <div class="dropdown">
    <button id="btn-export" title="Export">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      Export &#9662;
    </button>
    <div class="dropdown-menu" id="export-menu">
      <button id="btn-pdf">Save as PDF</button>
      <button id="btn-html">Download HTML</button>
    </div>
  </div>
</div>
<div class="hero">
  <div class="hero-label">{label}</div>
  <h1>{question_html}</h1>
  {subtitle_html}
</div>
{stats_block}
<div class="layout">
  <aside class="toc-sidebar"><nav>{toc_html}</nav></aside>
  <main class="content">
    {report_html}
    {sources_html}
  </main>
</div>
<div class="report-footer">Generated by CHILI &middot; {timestamp}</div>
<script>
(function() {{
  var exportBtn = document.getElementById('btn-export');
  var exportMenu = document.getElementById('export-menu');
  exportBtn.addEventListener('click', function(e) {{ e.stopPropagation(); exportMenu.classList.toggle('open'); }});
  document.addEventListener('click', function() {{ exportMenu.classList.remove('open'); }});
  document.getElementById('btn-pdf').addEventListener('click', function() {{ exportMenu.classList.remove('open'); window.print(); }});
  document.getElementById('btn-html').addEventListener('click', function() {{
    exportMenu.classList.remove('open');
    var blob = new Blob([document.documentElement.outerHTML], {{ type: 'text/html' }});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = document.title.replace(/[^a-z0-9]+/gi, '-').substring(0, 60) + '.html';
    a.click();
  }});
  var tocLinks = document.querySelectorAll('.toc-sidebar nav a[href^="#"]');
  tocLinks.forEach(function(link) {{
    link.addEventListener('click', function(e) {{
      var id = link.getAttribute('href').slice(1);
      var target = document.getElementById(id);
      if (!target) return;
      e.preventDefault();
      target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      history.replaceState(null, '', '#' + id);
    }});
  }});
  var tocMap = {{}};
  tocLinks.forEach(function(link) {{ tocMap[link.getAttribute('href').slice(1)] = link; }});
  var activeId = null;
  function setActive(id) {{
    if (id === activeId) return;
    if (activeId && tocMap[activeId]) tocMap[activeId].classList.remove('active');
    if (id && tocMap[id]) tocMap[id].classList.add('active');
    activeId = id;
  }}
  var headings = document.querySelectorAll('.content h2[id], .content h3[id]');
  if (headings.length && 'IntersectionObserver' in window) {{
    var visible = new Set();
    var io = new IntersectionObserver(function(entries) {{
      entries.forEach(function(en) {{ if (en.isIntersecting) visible.add(en.target.id); else visible.delete(en.target.id); }});
      var current = null;
      for (var i = 0; i < headings.length; i++) {{ if (visible.has(headings[i].id)) {{ current = headings[i].id; break; }} }}
      if (current) setActive(current);
    }}, {{ rootMargin: '-10% 0px -75% 0px', threshold: 0 }});
    headings.forEach(function(h) {{ io.observe(h); }});
  }}
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(
    title: str,
    body_markdown: str,
    *,
    subtitle: str = "",
    label: str = "CHILI Report",
    sources: Optional[List[Dict]] = None,
    stats: Optional[Dict] = None,
    category: str = "",
) -> str:
    """Render a complete, self-contained HTML report from markdown.

    Args:
        title: report title; if the markdown's first heading is more specific
            it is used instead (and stripped from the body to avoid duplication).
        body_markdown: the report content in markdown.
        subtitle: optional one-line subtitle under the title.
        label: small uppercase kicker above the title (e.g. "Daily Trading Brief").
        sources: optional [{"title", "url"}] rendered as a collapsible list.
        stats: optional {label: value} rendered as a stats bar (e.g.
            {"Trades": 12, "Net P/L": "+$340"}).
        category: optional theme key ("brief", "research", "audit", "alert")
            that re-tints the accent palette. Unknown/empty -> default palette.

    Returns:
        A full HTML document string (no external assets, no backend calls).
    """
    sources = sources or []
    stats = stats or {}

    body_markdown = _strip_thinking(body_markdown) or ""
    synthesized, body_markdown = _extract_report_title(body_markdown, title)
    title_text = (synthesized or title or "Report")[:120]

    # Promote bold-only lines to ## headings if there are no real headings, so
    # the TOC isn't empty for snippet-style reports.
    if not re.search(r"^#{2,3}\s+", body_markdown, re.MULTILINE):
        body_markdown = re.sub(
            r"^\*\*([^*]+)\*\*\s*$",
            lambda m: f"## {m.group(1).strip()}",
            body_markdown,
            flags=re.MULTILINE,
        )

    report_html = _md_to_html(body_markdown)
    headings = _extract_headings(body_markdown)
    report_html = _apply_heading_ids(report_html, headings)

    toc_html = "\n      ".join(
        f'<a href="#{h["slug"]}" class="depth-{h["level"]}">{html.escape(h["text"])}</a>'
        for h in headings
    )

    stat_items = []
    for key, val in stats.items():
        if val is None:
            continue
        stat_items.append(
            f'<div class="stat"><span class="stat-value">{html.escape(str(val))}</span> {html.escape(str(key))}</div>'
        )
    stats_block = (
        f'<div class="stats-bar">\n  ' + "\n  ".join(stat_items) + "\n</div>"
        if stat_items else ""
    )

    sources_html = ""
    if sources:
        items = []
        for i, s in enumerate(sources, 1):
            url = s.get("url", "")
            stitle = html.escape(s.get("title", "") or url)
            try:
                domain = urlparse(url).hostname or ""
                domain = domain[4:] if domain.startswith("www.") else domain
            except Exception:
                domain = url
            items.append(
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">'
                f'<span class="snum">{i}.</span><span>{stitle}</span>'
                f'<span class="sdomain">{html.escape(domain)}</span></a>'
            )
        sources_html = (
            '<div class="sources-panel"><details><summary>'
            f'Sources ({len(sources)})</summary><div class="sources-list">'
            + "\n".join(items) + "</div></details></div>"
        )

    desc_text = re.sub(r"[#*_\[\]()]", "", body_markdown)[:160].strip()
    subtitle_html = f'<p class="subtitle">{html.escape(subtitle)}</p>' if subtitle else ""

    category_css = _category_css(category)
    # Only emit a category- class when the theme actually resolved, so default
    # reports keep an empty body class (and produce no override CSS).
    body_class = f"category-{category.strip().lower()}" if category_css else ""

    return _TEMPLATE.format(
        title=html.escape(title_text),
        description=html.escape(desc_text),
        label=html.escape(label),
        question_html=html.escape(synthesized or title_text),
        subtitle_html=subtitle_html,
        stats_block=stats_block,
        toc_html=toc_html,
        report_html=report_html,
        sources_html=sources_html,
        timestamp=datetime.now().strftime("%B %d, %Y at %H:%M"),
        category_css=category_css,
        body_class=body_class,
    )


# ---------------------------------------------------------------------------
# Plaintext export
# ---------------------------------------------------------------------------

def to_plaintext(markdown_text: str) -> str:
    """Strip markdown to readable plain text.

    Removes heading hashes (keeps the text), strips emphasis markers
    (``*``/``_``/`` ` ``), converts ``[text](url)`` links to ``text (url)``,
    drops image syntax, and collapses runs of blank lines. Pure regex, no deps.
    """
    if not markdown_text:
        return ""

    text = markdown_text
    # Drop images entirely: ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Links: [text](url) -> text (url); empty-text link -> just the url.
    text = re.sub(
        r"\[([^\]]*)\]\(([^)]*)\)",
        lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1).strip() else m.group(2),
        text,
    )

    out_lines: List[str] = []
    for line in text.splitlines():
        # Heading hashes: leading #'s (and any trailing #'s) -> keep text only.
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"\s+#+\s*$", "", line)
        # Blockquote markers and list bullets -> plain.
        line = re.sub(r"^\s*>\s?", "", line)
        out_lines.append(line)
    text = "\n".join(out_lines)

    # Strip emphasis/inline-code markers, keeping the wrapped content.
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)          # bold
    text = re.sub(r"(\*|_)(.+?)\1", r"\2", text)             # italic
    text = re.sub(r"`+([^`]*)`+", r"\1", text)               # inline code
    # Remove any stray leftover emphasis/code symbols.
    text = re.sub(r"[*_`]", "", text)

    # Collapse 3+ newlines into a single blank line; trim trailing whitespace.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
