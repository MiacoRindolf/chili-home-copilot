"""[61] SNAPSHOT ONSET — ang UNANG spike, at ang resibong nagpapatunay na nakita natin ito.

REKLAMO NG OPERATOR (2026-09-10 23:20Z): "ang pasok lang ni CHILI ay sa pangalawang
spike, samantalang ang pinakamagandang entry (malaking R) ay ang unang spike."

SINUKAT (read-only, 37 symbol-day / 84 entry ng 14 araw; IQFeed lookup :9100 +
chili DB):
  * ang agwat mula sa UNANG ignition fire hanggang sa unang IQFeed tick natin ay
    p50 1.9 min / p75 3.7 min (max 8.4, AHMA) — ang HUGIS ng 300-s snapshot cache
    ng screen, hindi ng tape;
  * 19/36 symbol-day ang pumasok sa isang MAS HULING fire (16/36 sa una);
  * `momentum_ignition_nominations` = 0 hilera (23:45Z 09-10): ang tanging
    prodyuser nito ay ang bridge NOTIFY, na tumatakbo lamang sa naka-watch nang
    pangalan, kaya ang unang spike ng bagong pangalan ay hindi kailanman
    maitatala.

ANG SINUSUBOK DITO (slice 1):
  (a) ang ignition loop ay humihila na sa PROVIDER FLOOR (60 s), hindi sa profile
      TTL (300 s);
  (b) ang admission ay CROSS-SECTIONAL na ngayon — ang hangganan ay ang ika-K na
      order statistic ng MISMONG pull (K = profile.max_universe), at ang 7.0%
      na literal ay NAMED fallback lamang na ipinapangalan sa resibo;
  (c) bawat onset ay nag-iiwan ng hilera (source='snapshot_onset', cycle_index,
      JSONB receipt) + subscribe hint sa parehong transaksyon.

Runnable:
  set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_rossbench17_test
  pytest tests/test_ignition_snapshot_onset.py -v
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

import app.services.massive_client as mc
import app.services.trading.momentum_neural.ignition_loop as il
from app.services.trading.momentum_neural.ignition_loop import _UniverseTracker
from app.services.trading.momentum_neural.ignition_receipts import (
    SOURCE_SNAPSHOT_ONSET,
    ignition_nomination_params,
    write_ignition_nomination,
)

_NOMINATIONS = "momentum_ignition_nominations"
_HINTS = "momentum_bridge_subscribe_requests"


# ── fixtures ────────────────────────────────────────────────────────────────


def _row(sym, price, *, min_v=100_000.0, min_vw=None, day_v=2_000_000.0):
    """Isang snapshot row na may TAPAT na minute bar (`min.v` x `min.vw` = 60-s $)."""
    return {
        "ticker": sym,
        "day": {"o": 5.0, "v": day_v, "c": price},
        "min": {"c": price, "av": day_v, "v": min_v, "vw": min_vw or price},
        "prevDay": {"c": 5.0, "v": 5_000_000},
        "lastTrade": {"p": price},
    }


def _band(n, rise_by_i, dvol_by_i, *, base=5.0):
    """n na pangalan sa band; ang rise at 60-s $vol ay function ng rank index."""
    out = []
    for i in range(n):
        px = base * (1.0 + rise_by_i(i))
        dvol = dvol_by_i(i)
        # min.v x min.vw == dvol, sa presyong px
        out.append(_row(f"T{i:03d}", px, min_v=(dvol / px if px else 0.0), min_vw=px))
    return out


def _refresh(tracker, snapshot, *, universe=()):
    """Diretsong i-inject ang snapshot (walang network), gaya ng test_velocity_intake."""
    orig_build = il.build_equity_universe
    orig_snap = mc.get_full_market_snapshot
    try:
        il.build_equity_universe = lambda profile, snapshot=None: list(universe)
        mc.get_full_market_snapshot = lambda **kw: snapshot
        return tracker.refresh()
    finally:
        il.build_equity_universe = orig_build
        mc.get_full_market_snapshot = orig_snap


def _receipts(tracker):
    return {o["symbol"]: o for o in tracker.drain_onset_receipts()}


# ── (a) ang cadence ─────────────────────────────────────────────────────────


def test_ignition_loop_pulls_snapshot_at_the_provider_floor():
    """DERIVASYON: detection lag p50 1.9 / p75 3.7 min = hugis ng 300-s cache.

    Ang ignition loop ay ang feeder na ang TANGING tungkulin ay makita ang
    nagsisimula pa lamang, kaya hindi na ito humihila sa cadence ng screen.
    """
    seen: dict = {}
    orig_build = il.build_equity_universe
    orig_snap = mc.get_full_market_snapshot
    try:
        il.build_equity_universe = lambda profile, snapshot=None: []

        def _snap(**kw):
            seen.update(kw)
            return [_row("AAAA", 5.0)]

        mc.get_full_market_snapshot = _snap
        _UniverseTracker().refresh()
    finally:
        il.build_equity_universe = orig_build
        mc.get_full_market_snapshot = orig_snap

    assert seen["max_age_seconds"] == mc.MASSIVE_FULL_SNAPSHOT_TTL_FLOOR_S
    assert mc.MASSIVE_FULL_SNAPSHOT_TTL_FLOOR_S == 60.0
    # ...at ito ay MAS SARIWA kaysa sa dating profile TTL na ginagamit dito.
    from app.services.trading.momentum_neural.universe import EQUITY_ROSS_SMALLCAP

    assert EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds == 300.0
    assert mc.MASSIVE_FULL_SNAPSHOT_TTL_FLOOR_S < 300.0


def test_provider_floor_is_the_clamp_the_client_actually_applies():
    """Ang pangalan ay HINDI dekorasyon: ito ang literal na dating nakatago sa clamp."""
    assert mc._full_snapshot_effective_ttl(1.0) == mc.MASSIVE_FULL_SNAPSHOT_TTL_FLOOR_S
    assert mc._full_snapshot_effective_ttl(600.0) == 600.0


# ── (b) ang cut ─────────────────────────────────────────────────────────────


def test_cross_section_cut_binds_and_is_reported():
    """300 na pangalan: ang hangganan ay ang ika-50 (K = profile.max_universe)."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)))
    _refresh(tr, _band(300, lambda i: i * 0.0005, lambda i: 20_000.0 * (i + 1)))
    rec = _receipts(tr)
    assert rec, "walang onset receipt"
    any_receipt = next(iter(rec.values()))["receipt"]
    assert any_receipt["binding"] == "cross_section_rank"
    assert any_receipt["rank_k"] == 50
    n = any_receipt["n_cross_section"]
    # ang T000 ay may rise=0 (hindi umaakyat) kaya wala sa cross-section
    assert n == 299
    assert any_receipt["quantile_effective"] == pytest.approx(1.0 - 50.0 / 299.0)
    # ang ika-50 na pinakamataas na rise: i=250 -> 12.5%
    assert any_receipt["rise_cut_pct"] == pytest.approx(12.5, abs=1e-6)
    assert any_receipt["dvol_cut_usd"] == pytest.approx(20_000.0 * 251, rel=1e-6)
    # BUNTOT NGA: ang cut ay lampas sa median ng sarili nitong cross-section
    assert any_receipt["rise_cut_pct"] > any_receipt["rise_median_pct"]
    assert any_receipt["dvol_cut_usd"] > any_receipt["dvol_median_usd"]
    assert any_receipt["fallback_reason"] is None
    assert any_receipt["window_seconds"] == 180.0
    assert any_receipt["snapshot_ttl_seconds"] == 60.0
    # ANG HALAGANG NAGPASYA ay isa lamang at hindi ang literal na 7.0
    assert any_receipt["binding_rise_pct"] == pytest.approx(12.5, abs=1e-6)
    assert any_receipt["binding_dvol_usd"] == pytest.approx(20_000.0 * 251, rel=1e-6)
    assert any_receipt["fallback_floor_pct"] == 7.0


def test_the_tail_name_is_nominated_and_the_midpack_name_is_not():
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)))
    got = _refresh(tr, _band(300, lambda i: i * 0.0005, lambda i: 20_000.0 * (i + 1)))
    rec = _receipts(tr)
    assert "T299" in rec and "T299" in got          # p99-class: nominated + admitted
    assert "T150" not in rec and "T150" not in got  # p50-class: walang resibo
    assert rec["T299"]["outcome"] == "snapshot_onset_admitted"
    assert rec["T299"]["cycle_index"] == 0
    # eksaktong K ang nakalusot sa AND ng dalawang axis
    assert len(rec) == 50


def test_small_cross_section_uses_the_named_fallback_floor():
    """n <= K: ang 'top K' ay LAHAT, kaya walang sinasabi ang cross-section."""
    tr = _UniverseTracker()
    _refresh(tr, _band(10, lambda i: 0.0, lambda i: 500_000.0))
    got = _refresh(tr, _band(10, lambda i: 0.01 * i, lambda i: 500_000.0))
    rec = _receipts(tr)
    assert rec
    r = next(iter(rec.values()))["receipt"]
    assert r["binding"] == "fallback_floor"
    assert r["fallback_reason"] == "cross_section_smaller_than_pool"
    assert r["n_cross_section"] == 9
    # ang NAMED na floor ang iniuulat bilang halagang nagpasya, at ang $-leg ay
    # PINANGANGALANANG wala (hindi 0.0, na parang lahat pumasa)
    assert r["binding_rise_pct"] == 7.0
    assert r["binding_dvol_usd"] is None
    # ang NAMED na floor (7.0%) ang nagpasya: i=7 -> +7%, i=6 -> +6%
    assert "T007" in got and "T009" in got
    assert "T006" not in got and "T005" not in got


def test_a_cut_that_is_not_a_tail_falls_back_to_the_named_floor():
    """n=60, K=50: ang 'top 50' ay ang top 83% — hindi buntot, kaya hindi hangganan."""
    tr = _UniverseTracker()
    _refresh(tr, _band(60, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)))
    _refresh(tr, _band(60, lambda i: 0.002 * i, lambda i: 20_000.0 * (i + 1)))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["binding"] == "fallback_floor"
    assert r["fallback_reason"] == "cut_not_a_tail"
    assert r["rise_cut_pct"] <= r["rise_median_pct"] or (
        r["dvol_cut_usd"] <= r["dvol_median_usd"]
    )


def test_a_dead_minute_bar_is_not_a_threshold():
    """Walang naka-print sa huling minuto sa buong band: p_K ng $vol = 0.

    Ang 0 >= 0 ay totoo para sa LAHAT, kaya ang zero-heavy na cross-section ay
    hindi hangganan kundi katahimikan — pangalanan ito, huwag ipagpalagay.
    """
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, lambda i: 0.0))
    _refresh(tr, _band(300, lambda i: i * 0.0005, lambda i: 0.0))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["binding"] == "fallback_floor"
    assert r["fallback_reason"] == "cut_not_positive"


def test_falling_names_are_never_in_the_cross_section():
    """May TANDA ang onset: ang bumabagsak ay hindi denominador at hindi admission."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.30, lambda i: 20_000.0 * (i + 1)))
    got = _refresh(tr, _band(300, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)))
    assert got == set()
    assert _receipts(tr) == {}


# ── (c) ang resibo: rising edge + cycle index ───────────────────────────────


def test_receipt_is_written_on_the_rising_edge_only():
    """Ang tumatakbo nang 20 minuto ay isang onset, hindi 60 hilera."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)))
    _refresh(tr, _band(300, lambda i: i * 0.0005, lambda i: 20_000.0 * (i + 1)))
    assert "T299" in _receipts(tr)
    # parehong pull muli: patuloy na lumalampas, pero WALA nang bagong edge
    _refresh(tr, _band(300, lambda i: i * 0.0005, lambda i: 20_000.0 * (i + 1)))
    assert "T299" not in _receipts(tr)


def test_cycle_index_counts_the_nth_spike_of_the_day():
    """ANG INSTRUMENTO NG [61]: cycle 0 = unang spike, 1 = ang pinapasok natin."""
    tr = _UniverseTracker()
    dvol = lambda i: 20_000.0 * (i + 1)  # noqa: E731
    _refresh(tr, _band(300, lambda i: 0.0, dvol))
    # spike #1 ng T299
    _refresh(tr, _band(300, lambda i: i * 0.0005, dvol))
    assert _receipts(tr)["T299"]["cycle_index"] == 0
    # ang iba ay lumundag nang mas malayo; ang T299 ay nanatili -> nawala sa cut
    quiet = _band(300, lambda i: 0.20 + i * 0.0005, dvol)
    quiet[299] = _row("T299", 5.0 * 1.1495, min_v=(dvol(299) / (5.0 * 1.1495)),
                      min_vw=5.0 * 1.1495)
    _refresh(tr, quiet)
    assert "T299" not in _receipts(tr)
    # spike #2 ng T299
    hot = list(quiet)
    hot[299] = _row("T299", 15.0, min_v=(dvol(299) / 15.0), min_vw=15.0)
    _refresh(tr, hot)
    assert _receipts(tr)["T299"]["cycle_index"] == 1


def test_cycle_counters_reset_on_the_et_date_rollover():
    tr = _UniverseTracker()
    tr._basis_session_date = "1999-01-01"
    tr._onset_cycle = {"OLDY": 7}
    tr._onset_active = {"OLDY"}
    tr._roll_basis_session()
    assert tr._onset_cycle == {}
    assert tr._onset_active == set()


def test_a_name_already_in_the_universe_still_gets_a_receipt():
    """Ang PANGALAWANG spike ng pangalang nasa screen na ay ang mismong reklamo —
    kaya ito ay naitatala kahit walang bagong admission."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)),
             universe=["T299"])
    _refresh(tr, _band(300, lambda i: i * 0.0005, lambda i: 20_000.0 * (i + 1)),
             universe=["T299"])
    rec = _receipts(tr)
    assert rec["T299"]["outcome"] == "snapshot_onset_already_watched"
    assert rec["T299"]["receipt"]["already_in_universe"] is True


# ── kill switch: walang bagong dark flag, ang luma ay kumikilos pa rin ───────


def test_intake_flag_off_is_still_byte_identical():
    from app.config import settings

    tr = _UniverseTracker()
    old = getattr(settings, "chili_momentum_velocity_intake_enabled", True)
    try:
        settings.chili_momentum_velocity_intake_enabled = False
        _refresh(tr, _band(300, lambda i: 0.0, lambda i: 20_000.0 * (i + 1)))
        got = _refresh(tr, _band(300, lambda i: i * 0.0005,
                                lambda i: 20_000.0 * (i + 1)))
    finally:
        settings.chili_momentum_velocity_intake_enabled = old
    assert got == set()
    assert tr.drain_onset_receipts() == []


# ── ang hilera: DB round trip ───────────────────────────────────────────────


def test_migration_377_is_registered_once_with_a_free_id():
    from app.migrations import MIGRATIONS

    ids = [mid for mid, _fn in MIGRATIONS]
    assert ids.count("377_ignition_nomination_onset_receipt") == 1
    assert len(ids) == len(set(ids))


def test_onset_row_round_trips_with_source_cycle_and_receipt(db):
    """Ang bound na parameter ay dumadaan sa TUNAY na column type (JSONB kasama)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    params = ignition_nomination_params(
        {
            "symbol": "TNON",
            "fired_at": now,
            "last_price": 4.21,
            "pct_change_60s": None,
            "dollar_vol_60s": 812_345.0,
        },
        received_at=now,
        outcome="snapshot_onset_admitted",
        source=SOURCE_SNAPSHOT_ONSET,
        cycle_index=3,
        receipt={"binding": "cross_section_rank", "rise_cut_pct": 12.5,
                 "dvol_cut_usd": 507_660.0, "n_cross_section": 4153},
    )
    assert params["source"] == "snapshot_onset"
    assert params["cycle_index"] == 3
    # ang %rise ay sinusukat sa WINDOW, kaya ang 60-s na column ay iniiwang NULL
    assert params["pct_change_60s"] is None
    assert write_ignition_nomination(db, params) is True
    db.flush()
    row = db.execute(
        text(
            "SELECT source, cycle_index, receipt->>'binding', "
            "(receipt->>'n_cross_section')::int, dollar_vol_60s, pct_change_60s "
            f"FROM {_NOMINATIONS} WHERE symbol = 'TNON'"
        )
    ).fetchone()
    assert row is not None
    assert row[0] == "snapshot_onset"
    assert row[1] == 3
    assert row[2] == "cross_section_rank"
    assert row[3] == 4153
    assert row[4] == pytest.approx(812_345.0)
    assert row[5] is None


def test_existing_iqfeed_rows_keep_their_meaning_by_default(db):
    """Ang column default ay `iqfeed_ignition`, kaya walang dating hilera na
    nagiging malabo pagkatapos ng mig 377."""
    db.execute(
        text(
            f"INSERT INTO {_NOMINATIONS} "
            "(symbol, fired_at, received_at, outcome) VALUES "
            "('LEGC', now(), now(), 'already_tracked')"
        )
    )
    db.flush()
    src = db.execute(
        text(f"SELECT source FROM {_NOMINATIONS} WHERE symbol='LEGC'")
    ).scalar()
    assert src == "iqfeed_ignition"


def test_publish_writes_the_row_and_the_subscribe_hint_together(db):
    """Ang ebidensya at ang TAPE ay iisang sandali — magkasama sila sa transaksyon."""
    from datetime import datetime, timezone

    from app.db import engine

    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    tr = _UniverseTracker()
    tr._pending_onsets = [{
        "symbol": "FTFT",
        "fired_at": datetime.now(timezone.utc),
        "last_price": 3.30,
        "dollar_vol_60s": 640_000.0,
        "pct_change_60s": None,
        "cycle_index": 0,
        "outcome": "snapshot_onset_admitted",
        "receipt": {"binding": "cross_section_rank", "rise_cut_pct": 12.5},
    }]
    loop._tracker = tr
    try:
        assert loop._publish_onset_receipts() == 1
        got = db.execute(
            text(
                "SELECT source, cycle_index, outcome "
                f"FROM {_NOMINATIONS} WHERE symbol='FTFT'"
            )
        ).fetchall()
        assert len(got) == 1
        assert got[0][0] == "snapshot_onset"
        assert got[0][1] == 0
        assert got[0][2] == "snapshot_onset_admitted"
        hint = db.execute(
            text(f"SELECT reason FROM {_HINTS} WHERE symbol='FTFT'")
        ).fetchall()
        assert [r[0] for r in hint] == ["snapshot_onset"]
        # ang drain ay isang beses lamang: ang pangalawang publish ay 0
        assert loop._publish_onset_receipts() == 0
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {_NOMINATIONS} WHERE symbol='FTFT'"))
            conn.execute(text(f"DELETE FROM {_HINTS} WHERE symbol='FTFT'"))


# ── ang derive script ay hindi dapat malason ng bagong prodyuser ────────────


def test_governor_derivation_is_scoped_to_the_iqfeed_source():
    """Ang dalawang governor ay katangian ng NOTIFY consumer lamang; ang bagong
    onset rows ay may ibang cadence at tahimik na igagalaw ang dalawa."""
    from pathlib import Path

    path = Path(il.__file__).resolve().parents[4] / "scripts" / "derive_ignition_governors.py"
    src = path.read_text(encoding="utf-8")
    for name in (
        "NOMINATION_MINUTES_SQL",
        "ADMISSION_LATENCY_SQL",
        "OUTCOME_CENSUS_SQL",
        "UNIVERSE_REASON_CENSUS_SQL",
    ):
        block = src.split(f"{name} = \"\"\"", 1)[1].split('"""', 1)[0]
        assert "source = 'iqfeed_ignition'" in block, name
