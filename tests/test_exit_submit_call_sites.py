"""Bawat tawag sa _submit_live_market_exit ay nagpapasa ng LAHAT ng required keyword (#1283).

ANG BUG. `_submit_live_market_exit_impl` ay may keyword-only na parameter na
WALANG default: client_order_id, bid, ask, mid. Ang dalawang exit na
naipadala nitong linggo — burst-window (#1275/#1277, ON) at failed-pop
momentum break (#1261) — ay tumawag nang wala ang apat na iyon:

    return _submit_live_market_exit(
        db, sess, adapter, le=le, reason="burst_window_exit",
        product_id=product_id, quantity=float(pos.get("quantity") or 0.0),
    )

TypeError sa sandaling pumutok ang burst; walang try sa paligid kundi ang
tick_live_session mismo, kaya namamatay ang pulse sa tawag. Ang 13 unit test
ng burst helper ay berde habang ang seam na nagsusumite ay hindi kailanman
gumana. Nahuli ng partial-wiring map 2026-09-02 ~06:30Z — bago ang unang RTH.

ANG GUARD. Hinahango ang required set MULA SA SIGNATURE (hindi hardcoded),
saka sinusuri ng AST ang bawat call site: kung may magdagdag ng bagong
required keyword sa impl, mabibigo ang guard sa bawat call site na hindi
na-update — hindi sa unang fill.

Runnable: pytest tests/test_exit_submit_call_sites.py -v
"""
from __future__ import annotations

import ast
import inspect

from app.services.trading.momentum_neural import live_runner as lr


def _required_keywords_of_impl() -> set[str]:
    sig = inspect.signature(lr._submit_live_market_exit_impl)
    return {
        name for name, p in sig.parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }


def _call_sites() -> list[tuple[int, set[str], bool]]:
    """(lineno, keyword names, has **kwargs) para sa bawat tawag sa wrapper."""
    tree = ast.parse(inspect.getsource(lr))
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_submit_live_market_exit"
        ):
            names = {kw.arg for kw in node.keywords if kw.arg is not None}
            splat = any(kw.arg is None for kw in node.keywords)
            out.append((node.lineno, names, splat))
    return out


def test_the_impl_still_requires_the_four_keywords():
    """Positibong panig ng guard: ang required set ay hindi tahimik na nawala."""
    req = _required_keywords_of_impl()
    assert {"client_order_id", "bid", "ask", "mid", "le", "product_id", "quantity", "reason"} <= req


def test_every_call_site_passes_every_required_keyword():
    req = _required_keywords_of_impl()
    sites = _call_sites()
    assert len(sites) >= 5, f"inaasahan ang maraming call site, nakita {len(sites)}"
    missing = [
        (lineno, sorted(req - names))
        for lineno, names, splat in sites
        if not splat and not req <= names
    ]
    assert not missing, f"call site na kulang ng required keyword: {missing}"


def test_the_burst_and_break_sites_are_among_them():
    """Ang dalawang site na sumabog ay dapat naroon at kumpleto na."""
    src = inspect.getsource(lr)
    assert 'reason="burst_window_exit"' in src
    assert 'reason="momentum_break_stop"' in src
    sites = {lineno: names for lineno, names, _ in _call_sites()}
    lines = src.splitlines()
    for token in ('reason="burst_window_exit"', 'reason="momentum_break_stop"'):
        hit = next(i + 1 for i, l in enumerate(lines) if token in l)
        near = [n for ln, n in sites.items() if abs(ln - hit) <= 8]
        assert near, f"walang call site malapit sa {token}"
        assert {"client_order_id", "bid", "ask", "mid"} <= near[0], token
