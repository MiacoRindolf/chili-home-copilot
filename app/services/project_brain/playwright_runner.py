"""Playwright browser automation for the QA Engineer agent.

Provides sandboxed browser test execution, screenshot capture,
vision-based UI analysis, and accessibility auditing.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCREENSHOTS_DIR = os.path.join("data", "qa_screenshots")
_DEFAULT_TIMEOUT = 15_000
_BASE_URL = os.getenv("QA_BASE_URL", "http://127.0.0.1:8000")


def _ensure_screenshot_dir() -> str:
    os.makedirs(_SCREENSHOTS_DIR, exist_ok=True)
    return _SCREENSHOTS_DIR


def _get_browser():
    """Lazy-import playwright and launch a chromium browser."""
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        return pw, browser
    except Exception as e:
        logger.warning("[playwright] Browser launch failed: %s", e)
        raise


def run_smoke_tests(base_url: str | None = None) -> List[Dict[str, Any]]:
    """Run basic smoke tests against the running app."""
    url = base_url or _BASE_URL
    results: List[Dict[str, Any]] = []

    try:
        pw, browser = _get_browser()
    except Exception as e:
        return [{"name": "browser_launch", "passed": False, "errors": [str(e)]}]

    try:
        context = browser.new_context()
        page = context.new_page()

        smoke_routes = ["/", "/brain", "/trading"]
        for route in smoke_routes:
            start = time.time()
            test_name = f"smoke_{route.strip('/') or 'home'}"
            try:
                resp = page.goto(f"{url}{route}", wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT)
                passed = resp is not None and resp.status < 400
                errors = [] if passed else [f"HTTP {resp.status if resp else 'no response'}"]
                ss_path = _take_screenshot(page, test_name)
                results.append({
                    "name": test_name,
                    "passed": passed,
                    "errors": errors,
                    "duration_ms": int((time.time() - start) * 1000),
                    "screenshot_path": ss_path,
                })
            except Exception as e:
                results.append({
                    "name": test_name,
                    "passed": False,
                    "errors": [str(e)],
                    "duration_ms": int((time.time() - start) * 1000),
                })
    finally:
        browser.close()
        pw.stop()

    return results


def screenshot_pages(base_url: str | None = None, routes: List[str] | None = None) -> List[Dict[str, Any]]:
    """Take screenshots of application pages for visual analysis."""
    url = base_url or _BASE_URL
    routes = routes or ["/", "/brain", "/trading"]
    screenshots: List[Dict[str, Any]] = []

    try:
        pw, browser = _get_browser()
    except Exception:
        return []

    try:
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        for route in routes:
            try:
                page.goto(f"{url}{route}", wait_until="networkidle", timeout=_DEFAULT_TIMEOUT)
                ss_name = f"page_{route.strip('/') or 'home'}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
                path = _take_screenshot(page, ss_name)
                if path:
                    screenshots.append({"url": f"{url}{route}", "path": path, "route": route})
            except Exception as e:
                logger.debug("[playwright] Screenshot failed for %s: %s", route, e)
    finally:
        browser.close()
        pw.stop()

    return screenshots


def analyze_screenshot(screenshot_path: str) -> Optional[Dict[str, Any]]:
    """Use vision LLM to analyze a screenshot for UI bugs.

    Returns None if analysis is unavailable; otherwise
    {\"bugs\": [{\"title\": ..., \"description\": ..., \"severity\": ...}]}
    """
    if not screenshot_path or not os.path.isfile(screenshot_path):
        return None

    try:
        from ..llm_caller import call_llm
        import base64
        with open(screenshot_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        prompt = (
            "Analyze this screenshot for UI/UX bugs: layout issues, overlapping elements, "
            "broken alignment, missing text, truncated content, poor contrast, invisible buttons.\n"
            "Return ONLY valid JSON: {\"bugs\": [{\"title\": \"...\", \"description\": \"...\", "
            "\"severity\": \"info|warn|critical\"}]}\n"
            "If no bugs are found, return {\"bugs\": []}."
        )

        reply = call_llm(
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ]},
            ],
            max_tokens=500,
            trace_id="qa-vision",
        )
        if not reply:
            return None
        import json
        start = reply.find("{")
        end = reply.rfind("}")
        if start == -1:
            return None
        return json.loads(reply[start:end + 1])
    except Exception as e:
        logger.info("[playwright] Vision analysis unavailable: %s", e)
        return None


def run_accessibility_check(base_url: str | None = None, routes: List[str] | None = None) -> List[Dict[str, Any]]:
    """Run axe-core accessibility checks via Playwright."""
    url = base_url or _BASE_URL
    routes = routes or ["/", "/brain"]
    results: List[Dict[str, Any]] = []

    try:
        pw, browser = _get_browser()
    except Exception:
        return []

    try:
        context = browser.new_context()
        page = context.new_page()
        for route in routes:
            try:
                page.goto(f"{url}{route}", wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT)
                page.add_script_tag(url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js")
                page.wait_for_function("typeof axe !== 'undefined'", timeout=5000)
                axe_results = page.evaluate("axe.run()")
                violations = axe_results.get("violations", [])
                results.append({
                    "route": route,
                    "violations": [
                        {"id": v.get("id"), "description": v.get("description"),
                         "impact": v.get("impact"), "nodes": len(v.get("nodes", []))}
                        for v in violations[:10]
                    ],
                })
            except Exception as e:
                logger.debug("[playwright] Accessibility check failed for %s: %s", route, e)
                results.append({"route": route, "violations": [], "error": str(e)})
    finally:
        browser.close()
        pw.stop()

    return results


def _take_screenshot(page, name: str) -> Optional[str]:
    """Take and save a screenshot, return path."""
    try:
        d = _ensure_screenshot_dir()
        path = os.path.join(d, f"{name}.png")
        page.screenshot(path=path, full_page=True)
        return path
    except Exception as e:
        logger.debug("[playwright] Screenshot save failed: %s", e)
        return None
