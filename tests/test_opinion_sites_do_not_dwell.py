"""[10] NCRA handoff tail -- the fast-bail DWELL-CONFIRM is retired (2026-09-11).

ANG PINAGMULAN (session 1586b298, 09-09): sa NCRA 07-29 ang doctrine arm ay -$133.20 laban sa
base. Ang structure floor ay +$22.20 (3 sentimong MAS MABUTI). Ang BUONG pinsala ay nasa buntot
pagkatapos ng floor: -$59.20 sa 5.296 s na CONFIRM DWELL (`bailout_breach_pending_confirm`
12:25:14.404 bid 2.56, entry 2.60; lumabas lang nang tumawid ang bid sa 2% hard backstop 2.548,
bid 2.48) + -$96.20 sa 6.17 s na bailout-to-fill ([9]).

ANG PREMISE NA NAWALA. Ang dwell (2026-08-27, #1207) ay nagbabantay ng isang fast-bail EXIT: 60 s
na tuloy-tuloy na dwell sa ilalim ng entry + lalim >= 1%, may 2% backstop. Mula 2026-09-10 [21]
ang dalawang site na binabalot nito (breakout fast-bail, lost-VWAP) ay ARM na lang ng resibo
(`live_opinion_exit_armed`), at mula #1385 ang tape verdict ay tumatakbo NAUNA sa kanila. Kaya ang
dwell ay nagbabantay ng RESIBO -- at ang early return nitong `bailout_dwell_pending` ay nilalaktawan
ang BUONG natitirang tick (max-hold, lost-VWAP, trail ratchet, adds) habang buhay ang stamp.

SUKAT (live, read-only, 2026-09-11): 23 stamp / 18 session (08-27 22:38Z .. 09-10 17:46Z), 0 sa
09-11; stamp -> unang desisyon o fill sa parehong session n=22: p50 67.80 s, min 3.06 s, max
1,116.45 s (scratchpad t10b_dwell_span.sql).

Runnable: pytest tests/test_opinion_sites_do_not_dwell.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import NormalizedTicker

from tests.test_held_tick_bbo_iqfeed_l1_first import _wired  # noqa: F401  (fixture)

_SRC = pathlib.Path(lr.__file__)

_RETIRED_FIELDS = (
    "chili_momentum_bailout_dwell_confirm_enabled",
    "chili_momentum_bailout_dwell_confirm_seconds",
    "chili_momentum_bailout_min_depth_pct",
    "chili_momentum_bailout_hold_max_depth_pct",
)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def _emitted_event_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_emit" and len(node.args) >= 3
                and isinstance(node.args[2], ast.Constant)):
            names.add(str(node.args[2].value))
    return names


# ── 1. the retirement, pinned on the source ────────────────────────────────────────


def test_the_dwell_function_and_every_call_to_it_are_gone(tree):
    defs = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_bailout_dwell_confirm_holds"]
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_bailout_dwell_confirm_holds"]
    assert defs == [] and calls == []


def test_no_tick_returns_bailout_dwell_pending_any_more(tree):
    """The early return that muted the rest of the held tick (p50 67.80 s, n=22 live)."""
    keys = [k.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)]
    assert "bailout_dwell_pending" not in keys


def test_the_dwell_receipts_are_no_longer_emitted(tree):
    names = _emitted_event_names(tree)
    assert "bailout_breach_pending_confirm" not in names
    assert "bailout_breach_flicker_dodged" not in names


def test_the_four_config_fields_are_removed_and_stale_env_keys_are_ignored():
    fields = type(settings).model_fields
    for name in _RETIRED_FIELDS:
        assert name not in fields, name
        assert not hasattr(settings, name), name
    # the precedent (config.py, the retired BOS site): a stale env key is dropped, not an error
    assert type(settings).model_config.get("extra") == "ignore"


def test_the_legacy_dwell_stamp_still_clears_on_recycle():
    """A lane that ran the old code can leave a stamp in `le`; only the recycle reset removes
    it, so the two keys stay in `_RECYCLE_ENTRY_STATE_KEYS` although nothing writes them."""
    le = {
        "bailout_breach_pending_utc": "2026-09-10T17:46:00",
        "bailout_breach_trigger": "breakout_failed_to_hold",
        "trade_cycles": 3,
    }
    cleared = lr._reset_entry_state_on_recycle(le)
    assert {"bailout_breach_pending_utc", "bailout_breach_trigger"} <= set(cleared)
    assert le == {"trade_cycles": 3}


# ── 2. the runner: the breakout fast-bail arms on the SAME tick and the tick goes on ──


@pytest.fixture
def _no_external_market_or_broker_http(monkeypatch):
    import requests
    from curl_cffi import requests as curl_requests

    def unavailable(*args, **kwargs):
        raise RuntimeError("external HTTP unavailable in the dwell-retirement regression")

    monkeypatch.setattr(requests.sessions.Session, "request", unavailable)
    monkeypatch.setattr(curl_requests.Session, "request", unavailable)
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *a, **k: None)


def test_breakout_fast_bail_arms_on_the_same_tick_and_the_rest_of_the_tick_runs(
    db, monkeypatch, _wired, _no_external_market_or_broker_http,
):
    """bid 9.85 = 1.5% under the 10.00 entry: INSIDE the retired dwell's band (below entry,
    above the 2% backstop 9.80), exactly where it used to stamp `bailout_breach_pending_confirm`
    and return `bailout_dwell_pending` on every tick for 60 s."""
    from tests.test_exit_verdict_held_priority import _held_tick
    from tests.test_momentum_emergency_exit_recovery import _fresh

    sess, _le, adapter, _tape, submits = _held_tick(db, monkeypatch, rollover=False)
    bid = 9.85
    meta = _fresh()
    ticker = NormalizedTicker(product_id="EVPRIOR", bid=bid, ask=bid + .02,
                              mid=bid + .01, freshness=meta)
    monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (ticker, meta, {"reason": "test_fresh_bbo"}))
    adapter.execution_bbo = (ticker, meta)
    # the predicate itself is not under test (entry_gates.breakout_failed_to_hold has its own
    # suite); only what the site does once it is True
    monkeypatch.setattr(lr, "breakout_failed_to_hold", lambda **_k: True)
    asked: list[str] = []

    def _suppressed(_db, _sess, _le, *, trigger, **_k):
        asked.append(str(trigger))
        return False

    monkeypatch.setattr(lr, "_opinion_exit_suppressed", _suppressed)

    out1 = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    assert out1.get("opinion_exit_armed") == "breakout_failed_fast_bail", out1
    assert not out1.get("bailout_dwell_pending")
    names = [e for e, _ in _wired]
    assert "bailout_breach_pending_confirm" not in names
    armed = [p for e, p in _wired if e == "live_opinion_exit_armed"]
    assert len(armed) == 1 and armed[0]["reason"] == "breakout_failed_fast_bail"
    assert armed[0]["bid"] == bid
    db.refresh(sess)
    saved = sess.risk_snapshot_json["momentum_live_execution"]
    assert "bailout_breach_pending_utc" not in saved
    assert sess.state == lr.STATE_LIVE_ENTERED      # a receipt, not a bailout

    # tick 2: already armed -> the site falls through and the REST of the tick runs (the
    # old dwell returned `bailout_dwell_pending` here too: 0 s into its 60-s clock)
    asked.clear()
    out2 = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    assert not out2.get("bailout_dwell_pending"), out2
    assert "lost_vwap_flatten" in asked, (asked, out2)   # the downstream site was reached
    assert len([e for e, _ in _wired if e == "live_opinion_exit_armed"]) == 1
    assert not submits and adapter.market_calls == [] and adapter.limit_calls == []


# ── 3. the runner: the lost-VWAP site arms on the SAME tick on the shipped defaults ──


def test_lost_vwap_arms_on_the_same_tick_on_the_shipped_defaults(
    db, monkeypatch, stable_non_alpaca_account_identity,
):
    """No dwell setting to switch off any more: the confirmed loss (bid 9.96, entry 10.00 --
    below entry, where the dwell used to stamp and return) arms on the first tick."""
    from tests.test_momentum_lost_vwap_flatten import (
        _LOSS_CLOSES, _PROD, _drive_tick, _events, _seed_held_session,
    )

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)

    sess = _seed_held_session(db, symbol=_PROD)
    out, _ad = _drive_tick(db, sess, bid=9.96, ask=9.97, provider_closes=_LOSS_CLOSES)

    assert out.get("opinion_exit_armed") == "lost_vwap_confirmed", out
    assert not out.get("bailout_dwell_pending")
    assert _events(db, sess, "bailout_breach_pending_confirm") == []
    assert len(_events(db, sess, "live_opinion_exit_armed")) == 1
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert "bailout_breach_pending_utc" not in le
    assert sess.state == lr.STATE_LIVE_ENTERED


# ── 4. the sibling quote-clock bailouts stay dark (kept from the retired suite) ───────


_DARK_BAILOUT_SIBLINGS = [
    "chili_momentum_bail_on_no_confirmation_enabled",
    "chili_momentum_instant_bid_below_fill_cut_enabled",
    "chili_momentum_sub5min_scalp_bailout_enabled",
    "chili_momentum_instant_bid_above_fill_confirm_enabled",
]


@pytest.mark.parametrize("flag", _DARK_BAILOUT_SIBLINGS)
def test_the_quote_clock_bailout_siblings_stay_off(flag):
    """Kept from the retired tests/test_bailout_dwell_confirm.py with a NEW reason: these four
    still transition to BAILOUT on a bid/wall-clock reading (a quote opinion that exits), while
    the held leg has been judged by the tape since [21]/#1385. The old reason -- an interaction
    with the dwell machine -- is gone with the dwell. Turning one on is a separate, measured PR."""
    assert getattr(settings, flag) is False, flag
