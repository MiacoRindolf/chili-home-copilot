"""Ignition payload -> Ross admission signal (the admission-latency seam).

MEASURED PROBLEM. ``ross_event_admission.admit_ross_event`` is the only code
path that can create a viability row for a symbol the universe poll has never
seen, and it produced **0 ``ross_event_admitted`` events across 3,379 live
sessions in 14 days**. The ignition NOTIFY carried the symbol's own last print
price, 60-second move and 60-second turnover; the loop passed ``signal=None`` and
threw all three away, so the universe proof fell back entirely on the Massive
snapshot row and failed closed on exactly the newly-igniting names the snapshot
has not caught up with yet.

These tests pin the FAIL-CLOSED properties as hard as the fix itself: a symbol
that cannot prove price AND dollar volume is still refused, a rejected day-change
basis still yields no day change, and a 60-second turnover number is never
allowed to stand in for day-scale tradability.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect, text

import app.services.trading.momentum_neural.live_runner_loop as loop_mod
import app.services.trading.momentum_neural.ross_event_admission as admission_mod
from app.migrations import (
    MIGRATIONS,
    _migration_376_momentum_ignition_nominations,
)
from app.services.trading.momentum_neural.ross_event_admission import (
    _independent_smallcap_a_plus_source,
    _explicit_ross_source,
    ignition_signal_from_payload,
)
from app.services.trading.momentum_neural.tick_scalp import (
    independent_smallcap_a_plus_evidence_ok,
    ross_tick_scalp_evidence_ok,
)
from app.services.trading.momentum_neural.universe import (
    ross_smallcap_profile_evidence,
)

_NOMINATION_TABLE = "momentum_ignition_nominations"


def _payload(**overrides) -> dict:
    """A validated ``chili.iqfeed-ignition-nominate.v1`` payload.

    ``pct_change_60s`` is a FRACTION exactly as the host detector reports it
    (``IgnitionConfig.pct_base = 0.05`` means +5%).
    """
    payload = {
        "symbol": "VTIX",
        "source": "ignition_tick",
        "schema": "chili.iqfeed-ignition-nominate.v1",
        "fired_at": "2026-07-27T13:17:15+00:00",
        "last_price": 4.98,
        "pct_change_60s": 0.084,
        "dollar_vol_60s": 516_361.0,
        "prints_10s": 103,
    }
    payload.update(overrides)
    return payload


def _snapshot(**overrides) -> dict:
    snap = {
        "ticker": "VTIX",
        "lastTrade": {"p": 4.98},
        "day": {"o": 4.10, "c": 4.98, "h": 5.02, "l": 4.05, "v": 900_000},
        "min": {"c": 4.98, "av": 900_000},
        "prevDay": {"c": 4.00, "h": 4.20, "l": 3.90, "v": 400_000},
        "todaysChangePerc": 24.5,
    }
    snap.update(overrides)
    return snap


# ── A. the pure signal builder ───────────────────────────────────────────────


def test_tape_dollar_volume_proves_the_universe_without_a_snapshot_row():
    """THE FIX. Payload price + tape $-volume, no snapshot row at all."""
    signal = ignition_signal_from_payload(
        _payload(),
        tape_dollar_volume=3_400_000.0,
        snapshot_row=None,
    )

    assert signal is not None
    assert signal["price"] == pytest.approx(4.98)
    assert signal["dollar_volume"] == pytest.approx(3_400_000.0)
    assert signal["ignition_dollar_volume_leg"] == "tape_since_midnight"
    # Today's share volume is reported from a TODAY-scale leg only.
    assert signal["day_volume"] == pytest.approx(3_400_000.0 / 4.98)
    # Price and dollar volume are both proven; the day change is unavailable
    # without a snapshot, so the honest short-horizon axis carries the "in play"
    # proof instead of a fabricated day change.
    ok, reason, debug = ross_smallcap_profile_evidence("VTIX", signal=signal)
    assert ok is True, (reason, debug)
    assert reason == "ross_universe_profile_ok"
    assert debug["change_leg"] == "velocity"
    assert debug["dollar_volume_source"] == "signal"


def test_dollar_volume_is_the_monotonic_max_of_the_same_two_legs():
    """EXACTLY the max(...) build_equity_universe computes at universe.py:1023.

    That line is ``max(price * vol, _iqfeed_dvols.get(ticker, 0.0))`` with
    ``vol = max(day.v, min.av)`` — TWO legs, both today-scale. This test
    evaluates the reference rule rather than asserting agreement with it.
    """
    # The tape leg wins: 100k of tape vs the snapshot's 10k shares x 4.98.
    snap = _snapshot(
        day={"o": 4.10, "c": 4.98, "h": 5.02, "l": 4.05, "v": 10_000},
        min={"c": 4.98, "av": 10_000},
    )
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=100_000.0, snapshot_row=snap
    )
    assert signal is not None
    assert signal["ignition_dollar_volume_leg"] == "tape_since_midnight"

    def _universe_rule(row, price, tape_dv):
        """The literal universe.py:1023 expression, re-evaluated here."""
        vol = max(
            float((row.get("day") or {}).get("v") or 0.0),
            float((row.get("min") or {}).get("av") or 0.0),
        )
        return max(price * vol, tape_dv or 0.0)

    assert signal["dollar_volume"] == pytest.approx(
        _universe_rule(snap, 4.98, 100_000.0)
    )
    assert signal["day_volume"] == pytest.approx(100_000.0 / 4.98)

    # The snapshot's today-volume leg wins when it is the largest.
    snap2 = _snapshot()
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=1_000.0, snapshot_row=snap2
    )
    assert signal is not None
    assert signal["ignition_dollar_volume_leg"] == "snapshot_today_volume"
    assert signal["dollar_volume"] == pytest.approx(900_000 * 4.98)
    assert signal["dollar_volume"] == pytest.approx(
        _universe_rule(snap2, 4.98, 1_000.0)
    )


def test_yesterdays_share_volume_is_never_a_tradability_proof():
    """REGRESSION: prevDay.v x today's price must NOT clear the $1M floor.

    A snapshot row that has a price but zero TODAY aggregates (the premarket /
    stale-aggregate case this whole change targets) has $0 of proven turnover.
    ``build_equity_universe`` computes max(4.20*0, 0.0) = $0 for it and drops
    it. An earlier revision of the builder added a third ``price * prevDay.v``
    leg, which manufactured $2.1M and ADMITTED the name — a fabricated
    tradability proof on the fail-closed admission path.
    """
    snap = _snapshot(
        day={"o": 0, "c": 0, "h": 0, "l": 0, "v": 0},
        min={"c": 4.20, "av": 0},
        prevDay={"c": 3.90, "v": 500_000},
        todaysChangePerc=None,
    )
    payload = _payload(last_price=4.20)
    signal = ignition_signal_from_payload(
        payload, tape_dollar_volume=None, snapshot_row=snap
    )
    assert signal is not None
    # No today-scale leg exists, so the signal carries NO dollar-volume claim.
    assert "dollar_volume" not in signal
    assert signal.get("ignition_dollar_volume_leg") is None

    # ...and the real gate still refuses it, exactly as it does on main.
    ok, reason, debug = ross_smallcap_profile_evidence(
        "TSTX", signal=signal, snapshot_row=snap
    )
    assert ok is False
    assert reason == "ross_universe_missing_dollar_volume"

    # ...which is what build_equity_universe computes for the identical row.
    assert max(4.20 * max(0.0, 0.0), 0.0) < 1_000_000.0


def test_all_dollar_volume_sources_absent_still_fails_closed():
    """The $1M floor is the tradability bar and must never be faked.

    ``dollar_vol_60s`` is present in the payload and is deliberately NOT a leg:
    it is a SIXTY-SECOND number, and the profile floor asks whether a full
    position can be exited TODAY.
    """
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=None, snapshot_row=None
    )

    assert signal is not None
    assert "dollar_volume" not in signal
    assert "day_volume" not in signal
    assert signal["ignition_dollar_volume_leg"] is None
    # The 60s turnover is carried for the record only, under its own key.
    assert signal["ignition_dollar_vol_60s"] == pytest.approx(516_361.0)

    ok, reason, _debug = ross_smallcap_profile_evidence("VTIX", signal=signal)
    assert ok is False
    assert reason == "ross_universe_missing_dollar_volume"


@pytest.mark.parametrize("tape_dv", [0.0, -1.0, float("nan"), None, "n/a"])
def test_degenerate_tape_dollar_volume_is_not_a_leg(tape_dv):
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=tape_dv, snapshot_row=None
    )
    assert signal is not None
    assert "dollar_volume" not in signal


def test_basis_rejected_snapshot_yields_change_pct_none():
    """A rejected day basis withholds the number; it never substitutes one.

    HOS 2026-09-02 shape: a $0.30 "prev close" against a $10.33 tape (34x) is
    order-of-magnitude fiction, caught by ``_snapshot_basis_rejected``.
    """
    poisoned = _snapshot(
        lastTrade={"p": 10.33},
        day={"o": 10.10, "c": 10.33, "h": 10.61, "l": 10.02, "v": 900_000},
        min={"c": 10.33, "av": 900_000},
        prevDay={"c": 0.30, "h": 0.31, "l": 0.29, "v": 400_000},
        todaysChangePerc=3343.3,
    )
    signal = ignition_signal_from_payload(
        _payload(last_price=10.33, pct_change_60s=0.01),
        tape_dollar_volume=None,
        snapshot_row=poisoned,
    )

    assert signal is not None
    assert signal["ignition_snapshot_basis_rejected"] == "implausible_vs_session"
    assert signal["ignition_change_source"] is None
    for key in ("daily_change_pct", "todays_change_perc", "change_pct"):
        assert key not in signal
    # Velocity is +1%, under the intake floor, so nothing rescues the change leg.
    ok, reason, _debug = ross_smallcap_profile_evidence("VTIX", signal=signal)
    assert ok is False
    assert reason == "ross_universe_missing_change_pct"


def test_clean_snapshot_basis_supplies_the_day_change():
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=None, snapshot_row=_snapshot()
    )
    assert signal is not None
    assert signal["ignition_change_source"] == "snapshot_todays_change_perc"
    assert signal["daily_change_pct"] == pytest.approx(24.5)
    assert signal["change_pct"] == pytest.approx(24.5)


def test_premarket_change_fallback_is_used_when_the_vendor_field_is_null():
    """The 09:17 ET case: no vendor change yet, but a clean prior close."""
    snap = _snapshot(
        todaysChangePerc=None,
        day={"o": 0.0, "c": 0.0, "h": 0.0, "l": 0.0, "v": 0},
        min={"c": 4.98, "av": 900_000},
    )
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=None, snapshot_row=snap
    )
    assert signal is not None
    assert signal["ignition_change_source"] == "snapshot_premarket_basis"
    # (4.98 - 4.00) / 4.00 * 100
    assert signal["daily_change_pct"] == pytest.approx(24.5)


def test_velocity_pct_is_converted_from_the_producer_fraction():
    """The producer reports a FRACTION; the velocity leg reads PERCENT."""
    signal = ignition_signal_from_payload(
        _payload(pct_change_60s=0.084), tape_dollar_volume=2_000_000.0, snapshot_row=None
    )
    assert signal is not None
    assert signal["velocity_pct"] == pytest.approx(8.4)

    # Below the intake floor (7.0) the velocity leg must NOT open.
    weak = ignition_signal_from_payload(
        _payload(pct_change_60s=0.02), tape_dollar_volume=2_000_000.0, snapshot_row=None
    )
    assert weak is not None
    assert weak["velocity_pct"] == pytest.approx(2.0)
    ok, reason, _debug = ross_smallcap_profile_evidence("VTIX", signal=weak)
    assert ok is False
    assert reason == "ross_universe_missing_change_pct"


def test_missing_velocity_leaves_the_key_absent():
    signal = ignition_signal_from_payload(
        _payload(pct_change_60s=None), tape_dollar_volume=2_000_000.0, snapshot_row=None
    )
    assert signal is not None
    assert "velocity_pct" not in signal


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        _payload(last_price=None),
        _payload(last_price=0),
        _payload(last_price=-3.0),
        _payload(symbol=""),
    ],
)
def test_no_usable_last_price_yields_no_signal(payload):
    assert (
        ignition_signal_from_payload(
            payload, tape_dollar_volume=5_000_000.0, snapshot_row=_snapshot()
        )
        is None
    )


def test_source_tokens_are_ones_the_existing_recognisers_accept():
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=6_000_000.0, snapshot_row=_snapshot()
    )
    assert signal is not None

    # The tape-source recogniser on the admission path accepts it...
    assert _independent_smallcap_a_plus_source("ignition_tick", signal) is True
    # ...and it is NOT mistaken for an explicit Ross/Warrior scanner payload,
    # which would close the independent small-cap A+ proof to a tape nomination.
    assert _explicit_ross_source(signal) is False

    # Both evidence gates recognise the source tokens.
    _ok, _reason, scalp_debug = ross_tick_scalp_evidence_ok(signal)
    assert scalp_debug["source_support"] is True
    _ok, _reason, indep_debug = independent_smallcap_a_plus_evidence_ok(signal)
    assert indep_debug["source_support"] is True


def test_the_built_signal_survives_the_full_arm_evidence_gate():
    """End to end on the seam: a real igniter reaches WATCH, not a refusal."""
    signal = ignition_signal_from_payload(
        _payload(), tape_dollar_volume=6_000_000.0, snapshot_row=_snapshot()
    )
    assert signal is not None
    universe_ok, universe_reason, _debug = ross_smallcap_profile_evidence(
        "VTIX", signal=signal
    )
    assert universe_ok is True, universe_reason
    evidence_ok, evidence_reason, evidence_debug = ross_tick_scalp_evidence_ok(signal)
    assert evidence_ok is True, (evidence_reason, evidence_debug)


def test_builder_is_pure_no_io_no_clock_no_settings():
    """The builder takes every fact as an argument — nothing is fetched.

    Checked on the AST (names actually referenced), not on the source text, so
    the prose in the docstring cannot pass or fail the test.
    """
    tree = ast.parse(inspect.getsource(ignition_signal_from_payload))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in (
        "SessionLocal",
        "settings",
        "now",
        "utcnow",
        "get_full_market_snapshot",
        "_iqfeed_dollar_volumes",
        "_fetch_snapshot_row",
    ):
        assert forbidden not in referenced, forbidden
    assert not [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


# ── D. governors are settings, defaulting to today's constants ───────────────


def test_governor_defaults_equal_the_previous_hard_coded_constants():
    from app.config import settings as app_settings

    assert app_settings.chili_momentum_ignition_dedup_ttl_seconds == 300.0
    assert app_settings.chili_momentum_ignition_admits_per_minute == 6
    assert loop_mod._ignition_dedup_ttl_seconds() == 300.0
    assert loop_mod._ignition_admits_per_minute() == 6
    # The module-level fallbacks are the same day-1 values, so an unreadable
    # setting cannot silently change behaviour.
    assert loop_mod._IGNITION_DEDUP_TTL_FALLBACK_S == 300.0
    assert loop_mod._IGNITION_ADMITS_PER_MINUTE_FALLBACK == 6


def test_governors_read_from_settings(monkeypatch):
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_ignition_dedup_ttl_seconds",
        42.5,
        raising=False,
    )
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_ignition_admits_per_minute",
        11,
        raising=False,
    )
    assert loop_mod._ignition_dedup_ttl_seconds() == 42.5
    assert loop_mod._ignition_admits_per_minute() == 11

    # A garbage value falls back to the documented day-1 default rather than
    # disabling the governor.
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_ignition_admits_per_minute",
        "not-a-number",
        raising=False,
    )
    assert loop_mod._ignition_admits_per_minute() == 6


def test_governor_verdicts_name_which_governor_suppressed_the_nomination(monkeypatch):
    monkeypatch.setattr(
        loop_mod.settings,
        "chili_momentum_ignition_admits_per_minute",
        2,
        raising=False,
    )
    loop = loop_mod.LiveRunnerLoop()

    assert loop._ignition_admission_verdict("AAAA") is None
    # Same symbol inside the TTL -> the dedup governor.
    assert loop._ignition_admission_verdict("AAAA") == "governor_dedup_ttl"
    assert loop._ignition_admission_verdict("BBBB") is None
    # Third distinct symbol inside the minute -> the rate governor.
    assert loop._ignition_admission_verdict("CCCC") == "governor_admits_per_minute"
    assert loop._ignition_admission_verdict("DDDD") == "governor_admits_per_minute"


# ── C. the migration ─────────────────────────────────────────────────────────


def test_migration_376_is_registered_once_with_a_free_id():
    ids = [vid for vid, _fn in MIGRATIONS]
    assert "376_momentum_ignition_nominations" in ids
    numbers = [vid.split("_", 1)[0] for vid in ids]
    assert numbers.count("376") == 1
    # The durable contract is that NO id is ever reused — assert that directly.
    # This line previously read `assert "375" not in numbers`, which was true only
    # while #1305 (migration 375) was unmerged. Both landed on 2026-09-04, 375 and
    # 376 now coexist by design, and the branch-time guard would have forced the
    # merge to either renumber a shipped migration or delete the check. Asserting
    # global uniqueness is strictly stronger and cannot expire.
    assert len(numbers) == len(set(numbers)), "duplicate migration id"
    assert "375_append_heavy_autovacuum_scale_factor" in ids


def test_migration_376_creates_the_table_and_is_idempotent(db):
    from app.db import engine

    with engine.begin() as conn:
        _migration_376_momentum_ignition_nominations(conn)
        # Re-running must be a no-op, not an error.
        _migration_376_momentum_ignition_nominations(conn)

    with engine.connect() as conn:
        inspector = sa_inspect(conn)
        assert _NOMINATION_TABLE in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns(_NOMINATION_TABLE)}
        assert {
            "symbol",
            "fired_at",
            "received_at",
            "last_price",
            "pct_change_60s",
            "dollar_vol_60s",
            "prints_10s",
            "outcome",
            "skipped",
            "ross_universe_reason",
        } <= columns
        indexes = {ix["name"] for ix in inspector.get_indexes(_NOMINATION_TABLE)}
        assert {"ix_min_fired_at", "ix_min_symbol_fired_at"} <= indexes


def test_recorded_at_stamps_the_decision_not_the_transaction_start(db):
    """REGRESSION: ``recorded_at`` must advance INSIDE a long transaction.

    The nomination INSERT deliberately rides the SAME transaction as the
    admission attempt, and Postgres ``now()`` is ``transaction_timestamp()`` —
    frozen at transaction START, before the universe proof, the tape query and
    the snapshot fetch. ``derive_ignition_governors.py`` reads
    ``recorded_at - fired_at`` as fire->admission-decision latency and sizes the
    dedup TTL from its p90, so a ``DEFAULT now()`` would understate that
    latency by exactly the work being measured (24x in a 3s admission).
    """
    db.execute(text("SELECT pg_sleep(1)"))
    params = loop_mod.LiveRunnerLoop._ignition_nomination_params(
        _payload(symbol="CLKT"),
        received_at=loop_mod._utcnow(),
        outcome="ross_universe_rejected",
        result={"skipped": "ross_universe_rejected"},
    )
    assert loop_mod.LiveRunnerLoop._write_ignition_nomination(db, params) is True
    db.flush()
    advanced = db.execute(
        text(
            f"SELECT recorded_at > now() FROM {_NOMINATION_TABLE} "
            "WHERE symbol = :s"
        ),
        {"s": "CLKT"},
    ).scalar()
    # With DEFAULT now() this is exactly equal (False); clock_timestamp advances.
    assert advanced is True


def test_nomination_row_round_trips(db):
    """The bound parameters actually INSERT against the real column types."""
    params = loop_mod.LiveRunnerLoop._ignition_nomination_params(
        _payload(),
        received_at=loop_mod._utcnow(),
        outcome="ross_universe_rejected",
        result={
            "skipped": "ross_universe_rejected",
            "ross_universe_reason": "ross_universe_missing_dollar_volume",
        },
    )
    assert params["symbol"] == "VTIX"
    # The fraction is stored exactly as the producer reports it.
    assert params["pct_change_60s"] == pytest.approx(0.084)
    assert params["prints_10s"] == 103
    assert params["ross_universe_reason"] == "ross_universe_missing_dollar_volume"

    assert loop_mod.LiveRunnerLoop._write_ignition_nomination(db, params) is True
    db.flush()
    row = db.execute(
        text(
            "SELECT symbol, outcome, skipped, ross_universe_reason, pct_change_60s "
            f"FROM {_NOMINATION_TABLE} WHERE symbol = :s"
        ),
        {"s": "VTIX"},
    ).fetchone()
    assert row is not None
    assert row[0] == "VTIX"
    assert row[1] == "ross_universe_rejected"
    assert row[3] == "ross_universe_missing_dollar_volume"


def test_admission_writes_one_nomination_row_in_its_own_transaction(db, monkeypatch):
    """C: the record and the decision commit together, or not at all."""
    seen: dict = {}

    def _fake_admit(session, **kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "admitted": False,
            "skipped": "ross_universe_rejected",
            "ross_universe_reason": "ross_universe_missing_dollar_volume",
        }

    monkeypatch.setattr(admission_mod, "admit_ross_event", _fake_admit)
    monkeypatch.setattr(admission_mod, "_fetch_snapshot_row", lambda _sym: _snapshot())
    monkeypatch.setattr(
        admission_mod,
        "_iqfeed_dollar_volumes",
        lambda syms: {str(s).upper(): 3_400_000.0 for s in syms},
    )

    loop = loop_mod.LiveRunnerLoop()
    loop._running = True
    loop._generation = 1
    received_at = loop_mod._utcnow()
    try:
        result = loop._admit_iqfeed_symbol(
            "VTIX",
            _payload(),
            expected_generation=1,
            nomination_received_at=received_at,
        )
        assert result is not None
        # The signal actually reached admit_ross_event — not None.
        assert seen["signal"] is not None
        # The monotonic max ran across both providers: the snapshot's today
        # volume (900k x 4.98) beats the tape's 3.4M here.
        assert seen["signal"]["dollar_volume"] == pytest.approx(900_000 * 4.98)
        assert seen["signal"]["ignition_dollar_volume_leg"] == "snapshot_today_volume"
        assert seen["signal"]["velocity_pct"] == pytest.approx(8.4)
        # And the snapshot row is NOT forwarded: snapshot precedence inside
        # ross_smallcap_profile_evidence would override the max with the
        # snapshot's own (possibly smaller) turnover — the premarket JEM bug.
        assert seen.get("snapshot_row") is None

        row = db.execute(
            text(
                "SELECT outcome, skipped, ross_universe_reason, last_price "
                f"FROM {_NOMINATION_TABLE} WHERE symbol = 'VTIX'"
            )
        ).fetchall()
        assert len(row) == 1
        assert row[0][0] == "ross_universe_rejected"
        assert row[0][1] == "ross_universe_rejected"
        assert row[0][2] == "ross_universe_missing_dollar_volume"
        assert row[0][3] == pytest.approx(4.98)
    finally:
        # The admission path COMMITS, so clean up outside this test's fixture tx.
        from app.db import engine

        with engine.begin() as conn:
            conn.execute(
                text(f"DELETE FROM {_NOMINATION_TABLE} WHERE symbol = 'VTIX'")
            )


# ── B/A. AST guard on the ignition handler's call order ──────────────────────


def _loop_ast() -> ast.ClassDef:
    source = Path(loop_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LiveRunnerLoop":
            return node
    raise AssertionError("LiveRunnerLoop class not found")


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found on LiveRunnerLoop")


def _called_attr_lines(func: ast.AST, attr: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
    ]


def test_ignition_handler_subscribes_before_it_admits():
    """AST, not a mock: the bridge hint must precede the admission attempt.

    The host bridge only tapes symbols it is subscribed to, and it discovers
    them by polling armed sessions — so the hint has to land BEFORE the
    admission decision, or a refused nomination leaves the symbol untaped for
    the next one.
    """
    handler = _method(_loop_ast(), "_handle_iqfeed_ignition_payload")
    subscribe_lines = _called_attr_lines(handler, "_request_ignition_bridge_subscription")
    admit_lines = _called_attr_lines(handler, "_admit_iqfeed_symbol")

    assert subscribe_lines, "handler never requests a bridge subscription"
    assert admit_lines, "handler never attempts admission"
    assert min(subscribe_lines) < min(admit_lines)


def test_bridge_subscription_helper_calls_the_real_hint_writer():
    helper = _method(_loop_ast(), "_request_ignition_bridge_subscription")
    called = {
        node.func.id
        for node in ast.walk(helper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    imported = {
        alias.name
        for node in ast.walk(helper)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "request_bridge_subscription" in called
    assert "request_bridge_subscription" in imported


def test_admission_no_longer_passes_signal_none():
    """The whole defect in one assertion."""
    admit = _method(_loop_ast(), "_admit_iqfeed_symbol")
    calls = [
        node
        for node in ast.walk(admit)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "admit_ross_event"
    ]
    assert len(calls) == 1
    signal_kwargs = [kw for kw in calls[0].keywords if kw.arg == "signal"]
    assert len(signal_kwargs) == 1
    value = signal_kwargs[0].value
    assert not (
        isinstance(value, ast.Constant) and value.value is None
    ), "admit_ross_event is still being handed signal=None"

    # And the value it IS handed is produced by the pure builder's I/O shell,
    # bound earlier in the same method.
    assert isinstance(value, ast.Name), ast.dump(value)
    builder_calls = [
        node
        for node in ast.walk(admit)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ignition_admission_signal"
    ]
    assert len(builder_calls) == 1
    bindings = [
        node
        for node in ast.walk(admit)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == value.id
            for target in node.targets
        )
    ]
    assert bindings, f"{value.id} is never assigned in _admit_iqfeed_symbol"
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ignition_admission_signal"
        for binding in bindings
        for node in ast.walk(binding.value)
    )


def test_admission_signal_is_only_built_for_ignition_payloads():
    """The v3 authority path carries no last price and must stay untouched."""
    signal = admission_mod.ignition_admission_signal(
        {"symbol": "VTIX", "source": "iqfeed_l1", "bid": 4.9, "ask": 5.0},
        snapshot_provider=lambda _sym: None,
        tape_dollar_volume_provider=lambda _syms: {},
    )
    assert signal is None
