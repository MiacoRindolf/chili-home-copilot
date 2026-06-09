"""Viability-staleness fix for the live momentum lane.

At the 06-09 open, **100% of boundary-risk blocks** were "Viability snapshot stale"
— a watched setup-in-progress went stale (its snapshot rotated out of the top-mover
refresh) and EVERY entry on it was rejected, and freshly-armed sessions were
terminally ERRORed on the same transient blip. Two pure helpers fix this:

  * ``_merge_equity_refresh_universe`` (2b) folds actively-watched equity symbols
    into the refresh scan so their snapshot stays fresh.
  * ``_only_transient_freshness_block`` (2a) lets the live runner RE-WATCH a
    freshly-armed session on a freshness-only block instead of hard-erroring it,
    while still hard-erroring genuine safety failures.

Both are pure — no DB, no market data.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import _only_transient_freshness_block
from app.services.trading_scheduler import _merge_equity_refresh_universe


# ── 2a: _only_transient_freshness_block ───────────────────────────────────────
def _chk(cid: str, ok: bool) -> dict:
    return {"id": cid, "ok": ok, "severity": "block" if not ok else "ok", "message": cid}


def test_freshness_only_block_is_transient():
    ev = {"checks": [_chk("viability_freshness", False)]}
    assert _only_transient_freshness_block(ev) is True


def test_freshness_with_a_passing_check_is_still_transient():
    # Only the freshness check FAILED; the others passed -> retry is safe.
    ev = {"checks": [_chk("viability_freshness", False), _chk("spread", True), _chk("concurrency", True)]}
    assert _only_transient_freshness_block(ev) is True


def test_freshness_plus_safety_failure_is_NOT_transient():
    # Kill-switch ALSO failed -> must hard-error, never re-watch.
    ev = {"checks": [_chk("viability_freshness", False), _chk("governance_kill_switch", False)]}
    assert _only_transient_freshness_block(ev) is False


def test_non_freshness_failure_is_NOT_transient():
    ev = {"checks": [_chk("concurrency_cap", False)]}
    assert _only_transient_freshness_block(ev) is False


def test_no_failed_checks_is_not_transient():
    ev = {"checks": [_chk("viability_freshness", True), _chk("spread", True)]}
    assert _only_transient_freshness_block(ev) is False


def test_empty_or_malformed_fails_safe():
    assert _only_transient_freshness_block({}) is False
    assert _only_transient_freshness_block({"checks": []}) is False
    assert _only_transient_freshness_block({"checks": "not-a-list"}) is False
    assert _only_transient_freshness_block({"checks": [None, 7]}) is False  # no failed dicts -> False


# ── 2b: _merge_equity_refresh_universe ────────────────────────────────────────
def test_merges_screened_and_watched_symbols():
    assert _merge_equity_refresh_universe(["NPT", "FCEL"], {"ADUR", "CCTG"}) == [
        "ADUR", "CCTG", "FCEL", "NPT",
    ]


def test_excludes_crypto_usd_pairs():
    # Watched crypto pairs have their own venue feed — never equity-scan them.
    assert _merge_equity_refresh_universe(["NPT"], {"BTC-USD", "ADUR"}) == ["ADUR", "NPT"]


def test_dedups_and_uppercases():
    assert _merge_equity_refresh_universe(["npt", "ADUR"], {"adur", "FCEL"}) == [
        "ADUR", "FCEL", "NPT",
    ]


def test_handles_empties_and_none():
    assert _merge_equity_refresh_universe(["NPT"], set()) == ["NPT"]
    assert _merge_equity_refresh_universe([], {"ADUR"}) == ["ADUR"]
    assert _merge_equity_refresh_universe([], set()) == []
    assert _merge_equity_refresh_universe(None, None) == []


def test_watched_symbol_already_screened_is_not_duplicated():
    # A watched name that is ALSO a top mover today appears once.
    assert _merge_equity_refresh_universe(["ADUR", "NPT"], {"ADUR"}) == ["ADUR", "NPT"]
