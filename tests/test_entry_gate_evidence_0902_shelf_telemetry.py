"""Shelf-registration damper: TELEMETRY ONLY (2026-09-02). Behaviour byte-identical.

The flat x0.75 / 365 d damper fired on 53/67 live fills (44 L / 9 W) — it does
not discriminate. A sizing regrade was studied and DROPPED: the > 60 d up-size
counterfactual is -$1,737 on the stale bucket (W$ +1,365 vs L$ -6,577; Aug/Sep
-7.7R) and the <= 7 d deepening is fitted to n=5 with ONE out-of-sample symbol
(VTAK) while D21's <= 10 d split points the other way (-1.72 with vs -1.61
without). What is missing is EVIDENCE: the damper left no trace when it did
not fire, none for candidates that never filled, and its cache is process-
local. So: record the state whenever it is present (age bucket,
days_since_newest, newest_form, count, fetched_at) on the fill AND once per
candidate session; decide the grade after four weeks.

Runnable: pytest tests/test_entry_gate_evidence_0902_shelf_telemetry.py -v
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import app.services.trading.momentum_neural.shelf_registration as sr
from app.services.trading.momentum_neural import live_runner as lr

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 11, 10, tzinfo=UTC)


def _d(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _state(days_ago, form="S-3", count=1, active=True):
    return {
        "symbol": "X", "cik": 1, "shelf_active": active, "shelf_filing_count": count,
        "newest_filing_date": _d(days_ago) if days_ago is not None else None,
        "newest_form": form, "fetched_at": NOW.isoformat(), "lookback_days": 365.0,
    }


# ── the v1 dataset cases: multiplier byte-identical ─────────────────────────

@pytest.mark.parametrize("sym,days,form,bucket", [
    ("CANF", 5, "S-3", "fresh"),
    ("JLHL", 40, "F-3", "active"),
    ("AUUD", 127, "424B5", "stale"),
    ("SDOT", 24, "S-1", "active"),
])
def test_active_states_keep_the_flat_075_and_gain_the_bucket(sym, days, form, bucket):
    st = _state(days, form=form)
    mult, dbg = sr.shelf_damper_multiplier(st, fraction=0.75)
    assert mult == 0.75 and dbg["shelf_filing_count"] == 1
    tel = sr.shelf_state_telemetry(st, fraction=0.75, now_utc=NOW)
    assert tel["mult"] == 0.75 and tel["shelf_active"] is True
    assert tel["days_since_newest"] == days and tel["age_bucket"] == bucket
    assert tel["newest_form"] == form and tel["fetched_at"] == NOW.isoformat()


def test_inactive_state_is_recorded_with_mult_1_and_bucket_none():
    st = _state(None, form=None, count=0, active=False)
    assert sr.shelf_damper_multiplier(st, fraction=0.75) == (1.0, None)  # unchanged contract
    tel = sr.shelf_state_telemetry(st, fraction=0.75, now_utc=NOW)
    assert tel is not None and tel["mult"] == 1.0 and tel["shelf_active"] is False
    assert tel["age_bucket"] == "none" and tel["days_since_newest"] is None


def test_none_state_records_nothing():
    assert sr.shelf_state_telemetry(None, fraction=0.75) is None
    assert sr.shelf_damper_multiplier(None, fraction=0.75) == (1.0, None)


@pytest.mark.parametrize("frac", [1.0, 0.0, float("nan"), "x"])
def test_invalid_fraction_records_mult_1_but_still_records(frac):
    st = _state(5)
    assert sr.shelf_damper_multiplier(st, fraction=frac)[0] == 1.0
    tel = sr.shelf_state_telemetry(st, fraction=frac, now_utc=NOW)
    assert tel["mult"] == 1.0 and tel["shelf_active"] is True and tel["age_bucket"] == "fresh"


@pytest.mark.parametrize("days,bucket", [
    (0, "fresh"), (7, "fresh"), (8, "active"), (60, "active"), (61, "stale"), (400, "stale"),
])
def test_bucket_boundaries_at_exactly_7_and_60_days(days, bucket):
    tel = sr.shelf_state_telemetry(_state(days), fraction=0.75, now_utc=NOW)
    assert (tel["days_since_newest"], tel["age_bucket"]) == (days, bucket)
    assert sr.shelf_age_bucket(days, has_filing=True) == bucket


def test_unparseable_newest_date_is_unknown():
    st = _state(5)
    st["newest_filing_date"] = "yesterday-ish"
    tel = sr.shelf_state_telemetry(st, fraction=0.75, now_utc=NOW)
    assert tel["days_since_newest"] is None and tel["age_bucket"] == "unknown"
    assert sr.shelf_age_bucket(None, has_filing=True) == "unknown"
    assert sr.shelf_age_bucket(None, has_filing=False) == "none"


def test_days_since_newest_uses_the_utc_calendar_date_of_the_clock():
    st = _state(5)
    assert sr.shelf_state_telemetry(st, fraction=0.75, now_utc=NOW)["days_since_newest"] == 5
    later = NOW + timedelta(days=3)
    assert sr.shelf_state_telemetry(st, fraction=0.75, now_utc=later)["days_since_newest"] == 8
    naive = later.replace(tzinfo=None)
    assert sr.shelf_state_telemetry(st, fraction=0.75, now_utc=naive)["days_since_newest"] == 8


# ── _fetch_state carries newest_form / fetched_at (mocked EDGAR) ─────────────

_TICKER_MAP = {"0": {"cik_str": 1234567, "ticker": "CANF", "title": "Can-Fite"}}


def _subs(forms_dates):
    return {"filings": {"recent": {
        "form": [f for f, _ in forms_dates], "filingDate": [d for _, d in forms_dates],
    }}}


def _prime(payload):
    """One mocked EDGAR round-trip through _fetch_state (prime_shelf_cache is
    hermetically skipped under CHILI_PYTEST=1 — the replay network fence)."""
    sr.reset_shelf_caches_for_tests()

    def fake_get(url):
        return _TICKER_MAP if "company_tickers" in url else payload

    with patch.object(sr, "_http_get_json", side_effect=fake_get):
        state = sr._fetch_state("CANF")
    sr.reset_shelf_caches_for_tests()
    return state


def _fresh(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_fetch_state_records_newest_form_and_fetched_at():
    state = _prime(_subs([("S-1", _fresh(30)), ("424B4", _fresh(5)), ("8-K", _fresh(2))]))
    assert state["shelf_active"] is True and state["shelf_filing_count"] == 2
    assert state["newest_form"] == "424B4"
    fetched = datetime.fromisoformat(state["fetched_at"])
    assert fetched.tzinfo is not None and (datetime.now(UTC) - fetched).total_seconds() < 60
    tel = sr.shelf_state_telemetry(state, fraction=0.75)
    assert tel["age_bucket"] == "fresh" and tel["days_since_newest"] == 5
    mult, dbg = sr.shelf_damper_multiplier(state, fraction=0.75)
    assert mult == 0.75 and dbg["newest_form"] == "424B4" and dbg["fetched_at"] == state["fetched_at"]


def test_fetch_state_without_shelf_forms_has_no_newest_form():
    state = _prime(_subs([("8-K", _fresh(2)), ("10-Q", _fresh(40))]))
    assert state["shelf_active"] is False and state["newest_form"] is None
    tel = sr.shelf_state_telemetry(state, fraction=0.75)
    assert tel["age_bucket"] == "none" and tel["mult"] == 1.0


# ── live_runner wiring: AST source pins ─────────────────────────────────────

def _tick_fn() -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(lr))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "tick_live_session":
            return node
    raise AssertionError("tick_live_session not found")


def _parents(fn):
    out = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            out[id(child)] = parent
    return out


def _ancestor_if_tests(node, parents):
    tests = []
    cur = node
    while id(cur) in parents:
        cur = parents[id(cur)]
        if isinstance(cur, ast.If):
            tests.append(ast.unparse(cur.test))
    return tests


def test_ledger_record_is_written_outside_the_mult_condition():
    fn = _tick_fn()
    parents = _parents(fn)
    writes = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Subscript)
        and ast.unparse(n.targets[0]) == "le['shelf_registration_damper']"
    ]
    assert writes, "no ledger write found"
    telemetry_writes = [w for w in writes if ast.unparse(w.value) == "_shelf_tel"]
    assert len(telemetry_writes) == 1
    tests = _ancestor_if_tests(telemetry_writes[0], parents)
    assert not any("_shelf_mult" in t for t in tests), tests
    assert any("_shelf_tel is not None" in t for t in tests), tests
    # the multiplier still applies ONLY inside 0 < mult < 1 (byte-identical sizing)
    src = ast.unparse(fn)
    assert "if 0.0 < float(_shelf_mult) < 1.0:" in src
    assert "_eff_max_loss = float(_eff_max_loss) * float(_shelf_mult)" in src


def test_candidate_state_is_emitted_exactly_once_and_never_networks():
    fn = _tick_fn()
    parents = _parents(fn)
    sites = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_emit"
        and len(n.args) >= 3 and isinstance(n.args[2], ast.Constant)
        and n.args[2].value == "shelf_registration_state"
    ]
    assert len(sites) == 1
    tests = _ancestor_if_tests(sites[0], parents)
    assert any("shelf_state_emitted" in t for t in tests), tests
    assert any(
        t.startswith("_score_ok and") and "not le.get('shelf_state_emitted')" in t for t in tests
    ), tests
    # cache-only: the candidate block imports cached_shelf_state, never the prime
    seg_start = ast.unparse(fn).index("shelf_registration_state")
    seg = ast.unparse(fn)[seg_start - 1500: seg_start]
    assert "cached_shelf_state" in seg and "prime_shelf_cache" not in seg


def test_no_fresh_or_stale_size_keys_shipped():
    from app.config import Settings

    s = Settings()
    assert s.chili_momentum_shelf_active_size_fraction == 0.75
    assert s.chili_momentum_shelf_lookback_days == 365.0
    for name in dir(s):
        assert "shelf_fresh" not in name and "shelf_stale" not in name, name
