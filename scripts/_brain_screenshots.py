"""
Headless screenshot script for the runtime tab redesign verification.
Captures 4 PNGs into docs/STRATEGY/CC_REPORTS/2026-05-23_runtime-tab-redesign-screens/.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("docs/STRATEGY/CC_REPORTS/2026-05-23_runtime-tab-redesign-screens")
OUT.mkdir(parents=True, exist_ok=True)

URL = "https://localhost:8000/brain?domain=trading"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#bx-runtime-header", timeout=10000)
        # Let above-fold loaders settle
        page.wait_for_timeout(2000)

        # 01_header_and_thesis.png — sticky header + thesis (clip just the runtime header band)
        page.evaluate("document.documentElement.scrollTop = 0")
        header_box = page.evaluate("(() => { const h = document.getElementById('bx-runtime-header'); const t = document.getElementById('bx-thesis-line'); if (!h || !t) return null; const hr = h.getBoundingClientRect(); const tr = t.getBoundingClientRect(); return {x: Math.max(0, hr.left - 8), y: Math.max(0, hr.top - 8), width: Math.min(1440, hr.right + 8), height: Math.min(900, tr.bottom + 16) - Math.max(0, hr.top - 8)}; })()")
        if header_box:
            page.screenshot(path=str(OUT / "01_header_and_thesis.png"), full_page=False, clip=header_box)
        else:
            page.screenshot(path=str(OUT / "01_header_and_thesis.png"), full_page=False, clip={"x": 0, "y": 0, "width": 1440, "height": 320})

        # 02_above_fold.png — full above-the-fold (header + thesis + edge card + activity card)
        page.screenshot(path=str(OUT / "02_above_fold.png"), full_page=False, clip={"x": 0, "y": 0, "width": 1440, "height": 900})

        # 03_diagnostics_drawer.png — drawer open over the above-fold
        page.evaluate("window.openBxDiagnosticsDrawer && window.openBxDiagnosticsDrawer('patterns')")
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "03_diagnostics_drawer.png"), full_page=False, clip={"x": 0, "y": 0, "width": 1440, "height": 900})
        page.evaluate("window.closeBxDiagnosticsDrawer && window.closeBxDiagnosticsDrawer()")
        page.wait_for_timeout(400)

        # 04_drilldown_research.png — Research tab active, scrolled so the tabstrip
        # AND the relocated research-extras are both visible.
        page.evaluate("typeof switchDeepDiveTab === 'function' && switchDeepDiveTab('research')")
        page.wait_for_timeout(1500)
        page.evaluate("var el = document.getElementById('bx-drilldown-tabs'); if (el) { var y = el.getBoundingClientRect().top + window.scrollY - 20; window.scrollTo({top: y, behavior: 'instant'}); }")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "04_drilldown_research.png"), full_page=False, clip={"x": 0, "y": 0, "width": 1440, "height": 900})

        browser.close()
        print("OK 4 screenshots")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
