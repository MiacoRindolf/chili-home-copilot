"""
Reduced-motion verification screenshot. Captures the diagnostics drawer
open under prefers-reduced-motion: reduce — should snap to translateX(0)
with no in-flight slide.
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
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1440, "height": 900},
            reduced_motion="reduce",
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#bx-runtime-header", timeout=10000)
        page.wait_for_timeout(2000)

        # Open the diagnostics drawer. Under reduced-motion the transition is
        # neutralized — the drawer should be fully on-screen immediately, not
        # mid-slide. We still wait a short tick for the JS open handler to run.
        opened = page.evaluate(
            "(() => { if (typeof window.openBxDiagnosticsDrawer === 'function') { window.openBxDiagnosticsDrawer('patterns'); return true; } "
            "const btn = document.getElementById('bx-open-diagnostics-btn'); if (btn) { btn.click(); return true; } return false; })()"
        )
        if not opened:
            print("WARN: could not invoke drawer open helper or click button", file=sys.stderr)
        page.wait_for_timeout(200)

        page.screenshot(
            path=str(OUT / "05_drawer_reduced_motion.png"),
            full_page=False,
            clip={"x": 0, "y": 0, "width": 1440, "height": 900},
        )

        browser.close()
        print("OK 05_drawer_reduced_motion.png")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
