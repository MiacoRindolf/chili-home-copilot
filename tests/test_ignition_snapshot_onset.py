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
  (b) ang admission ay CROSS-SECTIONAL na ngayon — ang mata ay ``min`` ng ika-K
      na order statistic ng MISMONG pull at ng NAMED na floor (7.0%), at ang
      kapasidad (K = profile.max_universe) ang nagpuputol ng bilang;
  (c) bawat onset ay nag-iiwan ng hilera (source='snapshot_onset', cycle_index,
      JSONB receipt) + subscribe hint sa IISANG savepoint.

ANG REVIEW (2026-09-11) AT KUNG ANO ANG PINAPAKO NG BAWAT TEST DITO:
  * ``cycle_index`` ay dating binibilang ang paggalaw ng IBANG pangalan (rank
    churn) at ang outage ng provider. Ngayon ang paglabas ay PER-SYMBOL at
    galing sa tape ng pangalang iyon, at ang HINDI MASUKAT ay hindi "tapos na".
  * ang AND ng dalawang independiyenteng top-K ay maaaring mag-admit ng ZERO
    (magkahiwalay na buntot). Ngayon isang mata (%rise) + capacity rank.
  * ang fallback na sanga ay WALANG hangganan (sinukat: 400 hilera/pull).
    Ngayon ay pinuputol sa K sa PAREHONG sanga.
  * ang cut ay PUMAPALIT sa 7.0 na floor, kaya sa mainit na araw ay NAGSASARA
    ang mata nang walang nag-uulat. Ngayon ``min(cut, floor)`` + ``floor_bound``.
  * ang IGNITE axis ay nasa 7.0 pa rin habang ang admission ay bumaba —
    dekorasyon ang admission. Ngayon IISANG mata ang dalawa.
  * ang hilera at ang hint ay magkahiwalay na savepoint (tape na walang
    ebidensya). Ngayon iisa.
  * ``recorded=True`` kahit bumagsak ang commit. Ngayon post-commit ang bandila.
  * ang ``cycle_index`` ay in-process at 0 pagkatapos ng restart. Ngayon galing
    sa LIBRO (bilang ng naunang hilera ng araw).
  * ``fired_at`` ay wall clock sa isang column na para sa paghahambing ng
    latency. Ngayon ang oras ng PRINT ng snapshot row.

Runnable:
  set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_rossbench17_test
  pytest tests/test_ignition_snapshot_onset.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

import app.services.massive_client as mc
import app.services.trading.momentum_neural.ignition_loop as il
import app.services.trading.momentum_neural.ignition_receipts as ir
from app.services.trading.momentum_neural.ignition_loop import _UniverseTracker
from app.services.trading.momentum_neural.ignition_receipts import (
    SOURCE_SNAPSHOT_ONSET,
    ignition_nomination_params,
    write_ignition_nomination,
)

_NOMINATIONS = "momentum_ignition_nominations"
_HINTS = "momentum_bridge_subscribe_requests"


# ── fixtures ────────────────────────────────────────────────────────────────


def _row(sym, price, *, min_v=100_000.0, min_vw=None, day_v=2_000_000.0, at_ns=None):
    """Isang snapshot row na may TAPAT na minute bar (`min.v` x `min.vw` = 60-s $)."""
    row = {
        "ticker": sym,
        "day": {"o": 5.0, "v": day_v, "c": price},
        "min": {"c": price, "av": day_v, "v": min_v, "vw": min_vw or price},
        "prevDay": {"c": 5.0, "v": 5_000_000},
        "lastTrade": {"p": price},
    }
    if at_ns is not None:
        row["updated"] = int(at_ns)
        row["lastTrade"]["t"] = int(at_ns)
    return row


def _band(n, rise_by_i, dvol_by_i, *, base=5.0, at_ns=None):
    """n na pangalan sa band; ang rise at 60-s $vol ay function ng rank index."""
    out = []
    for i in range(n):
        px = base * (1.0 + rise_by_i(i))
        dvol = dvol_by_i(i)
        # min.v x min.vw == dvol, sa presyong px
        out.append(_row(
            f"T{i:03d}", px, min_v=(dvol / px if px else 0.0), min_vw=px, at_ns=at_ns
        ))
    return out


def _refresh(tracker, snapshot, *, universe=(), build_raises=False):
    """Diretsong i-inject ang snapshot (walang network), gaya ng test_velocity_intake."""
    orig_build = il.build_equity_universe
    orig_snap = mc.get_full_market_snapshot

    def _build(profile, snapshot=None):
        if build_raises:
            raise RuntimeError("universe build failed (simulated)")
        return list(universe)

    try:
        il.build_equity_universe = _build
        mc.get_full_market_snapshot = lambda **kw: snapshot
        return tracker.refresh()
    finally:
        il.build_equity_universe = orig_build
        mc.get_full_market_snapshot = orig_snap


def _receipts(tracker):
    return {o["symbol"]: o for o in tracker.drain_onset_receipts()}


def _age_history(tracker, seconds):
    """Itulak pabalik ang price history (gayahin ang isang tunay na outage)."""
    tracker._price_history = [
        (ts - float(seconds), px) for ts, px in tracker._price_history
    ]


_DVOL = lambda i: 20_000.0 * (i + 1)  # noqa: E731 — rank-correlated turnover


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
    """300 na pangalan, tahimik na banda: ang mata ay ang ika-50 (K = max_universe)."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0001, _DVOL))
    rec = _receipts(tr)
    assert rec, "walang onset receipt"
    any_receipt = next(iter(rec.values()))["receipt"]
    assert any_receipt["binding"] == "cross_section_rank"
    assert any_receipt["rank_k"] == 50
    n = any_receipt["n_cross_section"]
    # ang T000 ay may rise=0 (hindi umaakyat) kaya wala sa cross-section
    assert n == 299
    assert any_receipt["quantile_effective"] == pytest.approx(1.0 - 50.0 / 299.0)
    # ang ika-50 na pinakamataas na rise: i=250 -> 2.50%
    assert any_receipt["rise_cut_pct"] == pytest.approx(2.5, abs=1e-6)
    assert any_receipt["dvol_cut_usd"] == pytest.approx(20_000.0 * 251, rel=1e-6)
    # BUNTOT NGA: ang cut ay lampas sa median ng sarili nitong cross-section
    assert any_receipt["rise_cut_pct"] > any_receipt["rise_median_pct"]
    assert any_receipt["fallback_reason"] is None
    assert any_receipt["window_seconds"] == 180.0
    assert any_receipt["snapshot_ttl_seconds"] == 60.0
    # ANG HALAGANG NAGPASYA ay isa lamang at hindi ang literal na 7.0
    assert any_receipt["binding_rise_pct"] == pytest.approx(2.5, abs=1e-6)
    assert any_receipt["fallback_floor_pct"] == 7.0
    # ...at ang $-volume ay HINDI na beto, kaya wala nang `binding_dvol_usd`
    assert "binding_dvol_usd" not in any_receipt


def test_the_receipt_carries_the_realized_counts_and_a_pull_id():
    """BUKAS NA TANONG 1 ng PR: 're-derivable ang K mula sa tunay na trapiko'.

    Hindi ito totoo kung ang nasusulat lamang ay ang mga rising EDGE — hindi
    mababawi ang bilang kada pull sa pamamagitan ng pagbilang ng hilera, at
    walang pull id na pagkakagrupuhan. Kaya iniuulat na ngayon ang REALIZED.
    """
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0001, _DVOL))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["n_qualified"] == 50
    assert r["n_cleared"] == 50
    assert r["n_admitted"] == 50
    assert r["capacity_capped"] is False
    assert r["admit_cut_rise_pct"] == pytest.approx(2.5, abs=1e-6)
    assert r["admit_cut_dvol_usd"] is not None
    # isang pull, IISANG id sa lahat ng hilera nito
    assert len({v["receipt"]["pull_id"] for v in rec.values()}) == 1
    assert len(next(iter(rec.values()))["receipt"]["pull_id"]) == 32


def test_the_cross_section_can_only_widen_the_eye_never_close_it():
    """ANG DIREKSYON NA HINDI KAILANMAN NASUKAT: sa MAINIT na cross-section ang
    ika-K na rise ay LAMPAS sa 7.0, kaya ang paglilipat ng mata doon ay
    PAGSASARA — sa mismong mga araw na binubuhay ni Ross. Ang floor ay
    nananatili, at ang cut ay iniuulat pa rin."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["rise_cut_pct"] == pytest.approx(12.5, abs=1e-6)   # ang cut ay MAINIT
    assert r["rise_cut_pct"] > r["fallback_floor_pct"]
    assert r["binding"] == "floor_bound"                        # ...pero hindi ito mata
    assert r["binding_rise_pct"] == 7.0                         # ang floor pa rin
    assert r["fallback_reason"] is None                         # nasukat ang cut
    # ang REALIZED na hangganan ay iniuulat, kaya nababasa sa hilera kung ang
    # mekanismong ito ay floor o ceiling sa araw na iyon
    assert r["n_qualified"] == 160                              # lahat ng >= 7.0%
    assert r["n_cleared"] == 50
    assert r["capacity_capped"] is True
    assert r["admit_cut_rise_pct"] == pytest.approx(12.5, abs=1e-6)


def test_the_tail_name_is_nominated_and_the_midpack_name_is_not():
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    got = _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    rec = _receipts(tr)
    assert "T299" in rec and "T299" in got          # p99-class: nominated + admitted
    assert "T150" not in rec and "T150" not in got  # p50-class: walang resibo
    assert rec["T299"]["outcome"] == "snapshot_onset_admitted"
    assert rec["T299"]["cycle_index"] == 0
    # eksaktong K ang nakalusot — ang kapasidad ang nagputol
    assert len(rec) == 50


def test_disjoint_marginal_tails_never_admit_zero():
    """ANG DALAWANG BUNTOT AY MAAARING MAGKAHIWALAY.

    Sinukat sa tunay na tracker: 300 na pangalan, ang 50 PINAKAMABILIS ay ang 50
    PINAKAMANIPIS sa huling-minutong turnover — ang literal na hugis ng unang
    spike (hindi pa nakakaabot ang volume). Ang AND ng dalawang independiyenteng
    top-K ay nag-admit ng ZERO: walang resibo, walang hint, walang bakas sa
    anumang log level, sa EKSAKTONG cohort na dahilan ng pag-iral ng [61].

    Ngayon: ISANG mata (%rise), at ang $-volume ay tie-break ng capacity rank
    lamang — kaya ang MABILIS AT MANIPIS ang pumapasok, hindi ang mabagal at
    mataba.
    """
    def _rise(i):
        return 0.20 + (i - 250) * 0.002 if i >= 250 else 0.08 + i * 0.00001

    def _dvol(i):
        return 300_000.0 + (i - 250) * 6_000.0 if i >= 250 else 1_800_000.0 + i

    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _dvol))
    got = _refresh(tr, _band(300, _rise, _dvol))
    rec = _receipts(tr)
    assert len(rec) == 50, "ang AND ng dalawang marginal ay nag-a-admit ng zero"
    assert {"T250", "T275", "T299"} <= set(rec)      # mabilis + manipis: PUMASOK
    assert "T000" not in rec and "T000" not in got   # mabagal + mataba: hindi
    r = rec["T299"]["receipt"]
    assert r["dvol_cut_usd"] > r["admit_cut_dvol_usd"], (
        "ang $-cut ay dapat REPORTED lamang; kung beto ito, zero ang admission"
    )


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
    # ang NAMED na floor ang iniuulat bilang halagang nagpasya
    assert r["binding_rise_pct"] == 7.0
    # ang NAMED na floor (7.0%) ang nagpasya: i=7 -> +7%, i=6 -> +6%
    assert "T007" in got and "T009" in got
    assert "T006" not in got and "T005" not in got


def test_a_cut_that_is_not_a_tail_falls_back_to_the_named_floor():
    """n=60, K=50: ang 'top 50' ay ang top 83% — hindi buntot, kaya hindi hangganan."""
    tr = _UniverseTracker()
    _refresh(tr, _band(60, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(60, lambda i: 0.002 * i, _DVOL))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["binding"] == "fallback_floor"
    assert r["fallback_reason"] == "cut_not_a_tail"
    assert r["rise_cut_pct"] <= r["rise_median_pct"]


def test_the_fallback_branch_is_capacity_bounded_too():
    """ANG PINAKAMAPANGANIB NA PAG-AANGKIN NG UNANG BERSYON.

    'Self-bounding: at most K names per pull' — hindi totoo sa fallback na sanga:
    doon ay wala nang $-leg at ang admission ay `rise >= 7.0%` sa BUONG banda.
    Sinukat sa tunay na tracker: 400 na miyembro -> 400 na hilera, 400 na hint,
    400 na magkakasunod na DB session sa refresh thread, sa IISANG pull.

    Ngayon ang K ay pinuputol sa PAREHONG sanga at ang pagkakaputol ay iniuulat.
    """
    tr = _UniverseTracker()
    # n=400 > K, pero ang cut ay HINDI buntot (lahat EKSAKTONG magkapantay, kaya
    # ang ika-K ay ang median mismo) -> ang NAMED na floor ang nagpasya
    _refresh(tr, _band(400, lambda i: 0.0, lambda i: 500_000.0))
    got = _refresh(tr, _band(400, lambda i: 0.30, lambda i: 500_000.0))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["binding"] == "fallback_floor"
    assert r["fallback_reason"] == "cut_not_a_tail"
    assert r["n_qualified"] == 400          # lahat ay lampas sa 7.0% na floor
    assert r["n_cleared"] == 50             # ...pero ang kapasidad ay 50
    assert r["capacity_capped"] is True
    assert len(rec) == 50
    assert len(got) == 50


def test_a_dead_minute_bar_is_not_a_veto():
    """Walang naka-print sa huling minuto sa buong band: ang ika-K na $vol = 0.

    Dati ito ay ipinapangalang `cut_not_positive` at ibinabalik ang BUONG
    admission sa 7.0 na floor — dahil ang $-leg ay beto noon. Ngayon ang
    $-volume ay ebidensya lamang, kaya ang patay na minute bar ay hindi na
    nagpapalit ng mata; ang liquidity hygiene ay nasa band membership pa rin
    (ang $1M na floor ng screen mismo, `_onset_band_member`).
    """
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, lambda i: 0.0))
    got = _refresh(tr, _band(300, lambda i: i * 0.0001, lambda i: 0.0))
    rec = _receipts(tr)
    r = next(iter(rec.values()))["receipt"]
    assert r["binding"] == "cross_section_rank"
    assert r["dvol_cut_usd"] == 0.0          # iniuulat, hindi ipinagpapalagay
    assert r["fallback_reason"] is None
    assert len(rec) == 50 and len(got) == 50


def test_falling_names_are_never_in_the_cross_section():
    """May TANDA ang onset: ang bumabagsak ay hindi denominador at hindi admission."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.30, _DVOL))
    got = _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    assert got == set()
    assert _receipts(tr) == {}


# ── (c) ang resibo: rising edge + cycle index ───────────────────────────────


def test_receipt_is_written_on_the_rising_edge_only():
    """Ang tumatakbo nang 20 minuto ay isang onset, hindi 60 hilera."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    assert "T299" in _receipts(tr)
    # parehong pull muli: patuloy na lumalampas, pero WALA nang bagong edge
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    assert "T299" not in _receipts(tr)


def test_cycle_index_is_invariant_to_what_the_other_symbols_do():
    """ANG PINAKAMALAKING BUTAS NG UNANG BERSYON.

    `_onset_active = _cleared` ay ginagawang MEMBERSHIP SA RANK ang pagiging
    aktibo, at ang rank ay gumagalaw kapag gumalaw ang IBA. Sinukat sa tunay na
    tracker: ang T299 ay hindi tumitinag ng kahit isang tick habang 200 iba pang
    pangalan ang lumundag at bumalik — at nagsulat ito ng PANGALAWANG nomination
    row, pangalawang hint, at `cycle_index=1`. Ang instrumentong dapat sumagot ng
    'pang-ilang spike' ay pinapatakbo ng gawi ng IBA.

    Ngayon ang paglabas ay per-symbol at galing sa tape ng pangalan mismo.
    """
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    assert _receipts(tr)["T299"]["cycle_index"] == 0
    frozen_px = 5.0 * (1.0 + 299 * 0.0005)

    # ang 200 ibang pangalan ay lumundag sa +50%; ang T299 ay NAKA-FREEZE
    hot = _band(300, lambda i: 0.50 if i < 200 else i * 0.0005, _DVOL)
    hot[299] = _row("T299", frozen_px, min_v=_DVOL(299) / frozen_px, min_vw=frozen_px)
    _refresh(tr, hot)
    assert "T299" not in _receipts(tr)

    # ...at bumalik sila; ang T299 ay HINDI PA RIN gumagalaw
    cool = _band(300, lambda i: i * 0.0005, _DVOL)
    cool[299] = _row("T299", frozen_px, min_v=_DVOL(299) / frozen_px, min_vw=frozen_px)
    _refresh(tr, cool)
    assert "T299" not in _receipts(tr), "rank churn ang nagsulat ng pekeng spike"
    assert tr._onset_cycle["T299"] == 1, "isang spike lamang ang nangyari"


def test_cycle_index_counts_the_symbols_own_second_spike():
    """ANG INSTRUMENTO NG [61]: cycle 0 = unang spike, 1 = ang pinapasok natin.

    Ang pangalawang spike ay tinutukoy ng TAPE ng pangalan mismo: bumalik ito sa
    pinanggalingan (wala nang positibong rise sa sarili nitong window), at
    tumakbo ULI.
    """
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    assert _receipts(tr)["T299"]["cycle_index"] == 0

    # ibinalik ng T299 ang BUONG leg — nasukat na hindi na ito umaakyat
    back = _band(300, lambda i: i * 0.0005, _DVOL)
    back[299] = _row("T299", 5.0, min_v=_DVOL(299) / 5.0, min_vw=5.0)
    _refresh(tr, back)
    assert "T299" not in _receipts(tr)
    assert "T299" not in tr._onset_active

    # spike #2
    again = _band(300, lambda i: i * 0.0005, _DVOL)
    again[299] = _row("T299", 15.0, min_v=_DVOL(299) / 15.0, min_vw=15.0)
    _refresh(tr, again)
    assert _receipts(tr)["T299"]["cycle_index"] == 1


def test_a_snapshot_outage_does_not_manufacture_a_second_spike():
    """ANG NORMAL NA FAILURE MODE, HINDI ANG SULOK.

    Ang 5-minutong outage ng snapshot ay NAITALANG pangyayari (08-18/19/20/22 —
    kaya nga umiiral ang `_UNIVERSE_RETAIN_MAX_S`). Dati: sa unang pull ng
    pagbawi ay wala nang history sa loob ng window, kaya walang `velocity`,
    kaya `_onset_active = set()` — at ang BAWAT pangalang hindi tumigil sa
    pagtakbo ay nakalista bilang bagong UNANG spike.

    Ngayon: ang HINDI MASUKAT ay hindi "tapos na". Ang paglabas ay kailangan ng
    SUKAT na nagsasabing hindi na umaakyat.
    """
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    assert _receipts(tr)["T299"]["cycle_index"] == 0
    assert len(tr._onset_active) == 50

    # ang outage: ang provider ay walang ibinalik at ang history ay lumampas na
    # sa velocity window
    _age_history(tr, 1_000.0)
    _refresh(tr, [])
    assert len(tr._onset_active) == 50, "hindi masukat != tapos na"

    # unang pull ng pagbawi: walang maihahambing pa rin, kaya walang edge
    _refresh(tr, _band(300, lambda i: i * 0.0006, _DVOL))
    assert _receipts(tr) == {}

    # ...at ang pangalang hindi tumigil sa pagtakbo ay HINDI bagong unang spike
    _refresh(tr, _band(300, lambda i: i * 0.0007, _DVOL))
    assert _receipts(tr) == {}
    assert tr._onset_cycle["T299"] == 1


def test_receipt_backlog_overflow_retains_newest_and_reports_drop(caplog):
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    tr._pending_onsets = [{"symbol": "OLD"}] * il._ONSET_RECEIPT_BUFFER_CAP
    _refresh(tr, _band(300, lambda i: i * 0.0001, _DVOL))
    pending = tr.drain_onset_receipts()
    assert len(pending) == il._ONSET_RECEIPT_BUFFER_CAP
    assert pending[-1]["symbol"] == "T299"
    assert any("backlog overflow dropped=50 capacity=512" in r.message for r in caplog.records)


def test_cycle_counters_reset_on_the_et_date_rollover():
    tr = _UniverseTracker()
    tr._basis_session_date = "1999-01-01"
    tr._onset_cycle = {"OLDY": 7}
    tr._onset_active = {"OLDY"}
    tr._onset_binding_rise_pct = 1.2
    tr._roll_basis_session()
    assert tr._onset_cycle == {}
    assert tr._onset_active == set()
    assert tr.velocity_floor() is None


def test_a_name_already_in_the_universe_still_gets_a_receipt():
    """Ang PANGALAWANG spike ng pangalang nasa screen na ay ang mismong reklamo —
    kaya ito ay naitatala kahit walang bagong admission."""
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL), universe=["T299"])
    _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL), universe=["T299"])
    rec = _receipts(tr)
    assert rec["T299"]["outcome"] == "snapshot_onset_already_watched"
    assert rec["T299"]["receipt"]["already_in_universe"] is True


def test_fired_at_is_the_snapshot_print_time_not_the_receipt_clock():
    """ANG BUONG DAHILAN ng pangalawang prodyuser sa PAREHONG table ay para
    masukat nang magkatabi ang latency — at ang IQFeed na prodyuser ay nagsa-stamp
    ng oras ng PRINT. Ang wall clock dito ay gagawing ~0 by construction ang
    `recorded_at - fired_at` at itatago ang mismong cache na sinusukat."""
    printed = datetime(2026, 9, 10, 13, 47, 11, tzinfo=timezone.utc)
    ns = int(printed.timestamp() * 1_000_000_000)
    tr = _UniverseTracker()
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL, at_ns=ns))
    _refresh(tr, _band(300, lambda i: i * 0.0001, _DVOL, at_ns=ns))
    rec = _receipts(tr)
    o = rec["T299"]
    assert o["fired_at"] == printed
    assert o["receipt"]["fired_at_source"] == "snapshot_print"
    # ...at ang `received_at` ay ang ATIN, kaya ang agwat ay nababasa
    assert o["received_at"] >= printed

    # walang orasan sa hilera -> PINANGANGALANAN ang wall clock, hindi tahimik
    tr2 = _UniverseTracker()
    _refresh(tr2, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr2, _band(300, lambda i: i * 0.0001, _DVOL))
    assert _receipts(tr2)["T299"]["receipt"]["fired_at_source"] == "wall_clock"


@pytest.mark.parametrize("trade_clock", ["valid", "missing", "invalid"])
def test_receipt_distinguishes_trade_time_from_generic_snapshot_update(trade_clock):
    printed = datetime(2026, 9, 10, 13, 47, 11, tzinfo=timezone.utc)
    updated = printed + timedelta(seconds=60)
    tr = _UniverseTracker()
    for rise in (lambda i: 0.0, lambda i: i * 0.0001):
        rows = _band(300, rise, _DVOL, at_ns=int(printed.timestamp()) * 10**9)
        for row in rows:
            row["updated"] = int(updated.timestamp()) * 10**9
            if trade_clock == "missing":
                row["lastTrade"].pop("t")
            elif trade_clock == "invalid":
                row["lastTrade"]["t"] = "not-a-clock"
        _refresh(tr, rows)
    onset = _receipts(tr)["T299"]
    if trade_clock == "valid":
        assert onset["fired_at"] == printed
        assert onset["receipt"]["fired_at_source"] == "snapshot_print"
    else:
        assert onset["fired_at"] == updated
        assert onset["receipt"]["fired_at_source"] == "snapshot_updated"


# ── ang degraded provider: ang onset ay DAGDAG, hindi kapalit ───────────────


def test_the_onset_leg_does_not_defeat_the_degraded_provider_retain():
    """`want |= _admits` ay tumatakbo BAGO ang retain, at ang retain ay
    nangangailangan ng `not want` — kaya sa `build_error` (malusog ang snapshot,
    bumagsak ang screen) ay TAHIMIK na napapalitan ang buong screened na watch
    set ng mga pangalang dumaan LAMANG sa band membership. Walang babala,
    walang bakas.

    Ngayon ang retain ay itinatanong sa SCREEN, at ang onset ay dagdag.
    """
    tr = _UniverseTracker()
    # isang malusog na screen muna
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL), universe=[f"S{i:03d}" for i in range(50)])
    assert len(tr.get_symbols()) == 50

    got = _refresh(
        tr, _band(300, lambda i: i * 0.0005, _DVOL), build_raises=True
    )
    assert tr.last_outcome() == il._UNIVERSE_BUILD_ERROR
    # ang screen ay NAPANATILI...
    assert {f"S{i:03d}" for i in range(50)} <= got
    # ...at ang onset ay DAGDAG, hindi kapalit
    assert "T299" in got
    assert len(got) == 100


# ── ang IGNITE axis ay may PAREHONG mata ────────────────────────────────────


def test_the_tracker_publishes_the_binding_eye():
    tr = _UniverseTracker()
    assert tr.velocity_floor() is None
    _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
    _refresh(tr, _band(300, lambda i: i * 0.0001, _DVOL))
    rec = _receipts(tr)
    eye = next(iter(rec.values()))["receipt"]["binding_rise_pct"]
    assert tr.velocity_floor() == pytest.approx(eye)
    assert tr.velocity_floor() == pytest.approx(2.5, abs=1e-6)


def test_the_ignite_axis_reads_the_same_eye_as_the_admission():
    """Kung hindi, ang admission ay DEKORASYON: ang pangalang pinapasok sa 1.9%
    ay tinatanggihan ng axis sa 7.0 at kumakain lamang ng watch slot at HINT
    slot. Ang deskripsyon mismo ng knob sa app/config.py ay nagsasabing IISA ito
    para sa admission at sa axis."""
    from app.services.trading.momentum_neural.nbbo_tape import _ross_threshold_crossed

    kw = dict(rvol=None, gap_pct=0.0, move_pct=0.0, price=5.0)
    # walang binding na mata: ang NAMED na floor pa rin
    assert _ross_threshold_crossed("AAAA", velocity_pct=2.0, **kw) is False
    # may binding na mata mula sa pull: ang PAREHONG halaga ang sinusukatan
    assert _ross_threshold_crossed(
        "AAAA", velocity_pct=2.0, velocity_floor_pct=1.9, **kw
    ) is True
    # ISANG DIREKSYON lamang: hindi kailanman mas mahigpit sa NAMED na floor
    assert _ross_threshold_crossed(
        "AAAA", velocity_pct=8.0, velocity_floor_pct=20.0, **kw
    ) is True


def test_the_arm_gate_reads_the_same_eye_as_the_admission():
    """Ang `universe.ross_smallcap_profile_evidence` ang pangalawang lugar na may
    literal na 7.0 — ang arm gate. Ang stamped na mata ay dapat ding basahin
    doon, kung hindi ay hindi maa-arm ang eksaktong pangalang pinapasok ng [61]."""
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    base = {
        "ticker": "AAAA", "price": 5.0, "volume": 2_000_000.0,
        "todays_change_perc": 0.5, "velocity_pct": 2.0,
    }
    ok, reason, _dbg = ross_smallcap_profile_evidence("AAAA", signal=dict(base))
    assert ok is False and reason == "ross_universe_change_below_profile"

    ok, reason, dbg = ross_smallcap_profile_evidence(
        "AAAA", signal=dict(base, velocity_floor_pct=1.9)
    )
    assert ok is True and dbg["change_leg"] == "velocity"
    assert dbg["velocity_floor_pct"] == pytest.approx(1.9)

    # nagpapaluwang lamang: ang mas MATAAS na stamped na halaga ay walang epekto
    ok, _reason, dbg = ross_smallcap_profile_evidence(
        "AAAA", signal=dict(base, velocity_pct=8.0, velocity_floor_pct=20.0)
    )
    assert ok is True
    assert dbg["velocity_floor_pct"] == pytest.approx(7.0)


# ── ang HINT source: ang onset ay hindi nagpapaalis ng ROSS/ELIGIBLE ────────


def test_onset_hints_yield_the_last_watch_slot():
    """Ang HINT ay nasa IBABAW ng ROSS at ELIGIBLE sa capacity priority ng
    bridge (`iqfeed_subscription_policy._TARGET_PRIORITY_ORDER`), kaya sa cap ay
    ang Ross universe at ang eligible board ang naaalis — hindi kailanman ang
    hint. Sinukat sa live DB: mean 67.9 / max 117 na natatanging HINT symbol kada
    180 s; ang onset ay makakadagdag ng hanggang K kada pull. Kaya ang onset ang
    UNANG binibitawan ng cap, hindi ang pangalang inaarmahan natin."""
    from app.services.trading.momentum_neural.bridge_subscribe import (
        select_fresh_subscribe_symbols,
    )

    now = datetime(2026, 9, 11, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        ("ONSETA", now - timedelta(seconds=1), "snapshot_onset"),
        ("ONSETB", now - timedelta(seconds=2), "snapshot_onset"),
        ("ALERTA", now - timedelta(seconds=30), "first_alert"),
        ("ALERTB", now - timedelta(seconds=40), "first_alert"),
    ]
    # ang cap ay 2: ang dalawang first_alert ang nananatili kahit MAS LUMA sila
    assert select_fresh_subscribe_symbols(rows, now_utc=now, max_new=2) == [
        "ALERTA", "ALERTB",
    ]
    # walang cap: nasa huli pa rin ang onset, pero newest-first sa loob ng klase
    assert select_fresh_subscribe_symbols(rows, now_utc=now) == [
        "ALERTA", "ALERTB", "ONSETA", "ONSETB",
    ]


def test_a_symbol_hinted_by_both_reasons_keeps_its_slot():
    from app.services.trading.momentum_neural.bridge_subscribe import (
        select_fresh_subscribe_symbols,
    )

    now = datetime(2026, 9, 11, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        ("BOTH", now - timedelta(seconds=1), "snapshot_onset"),
        ("BOTH", now - timedelta(seconds=60), "first_alert"),
        ("ONSET", now - timedelta(seconds=2), "snapshot_onset"),
        ("ALERT", now - timedelta(seconds=90), "first_alert"),
    ]
    # ang BOTH ay may isang first_alert, kaya HINDI ito bumibitaw — at ang
    # `freshest` nito ay ang max sa LAHAT ng hilera nito (ang dating kontrata)
    assert select_fresh_subscribe_symbols(rows, now_utc=now, max_new=1) == ["BOTH"]
    assert select_fresh_subscribe_symbols(rows, now_utc=now) == [
        "BOTH", "ALERT", "ONSET",
    ]


def test_two_tuple_rows_keep_the_exact_previous_ordering():
    """Ang dating kontrata (walang reason) ay hindi ginagalaw."""
    from app.services.trading.momentum_neural.bridge_subscribe import (
        select_fresh_subscribe_symbols,
    )

    now = datetime(2026, 9, 11, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        ("OLD", now - timedelta(seconds=60)),
        ("NEW", now - timedelta(seconds=1)),
        ("MID", now - timedelta(seconds=30)),
    ]
    assert select_fresh_subscribe_symbols(rows, now_utc=now) == ["NEW", "MID", "OLD"]


# ── kill switch: walang bagong dark flag, ang luma ay kumikilos pa rin ───────


def test_intake_flag_off_is_still_byte_identical():
    from app.config import settings

    tr = _UniverseTracker()
    old = getattr(settings, "chili_momentum_velocity_intake_enabled", True)
    try:
        settings.chili_momentum_velocity_intake_enabled = False
        _refresh(tr, _band(300, lambda i: 0.0, _DVOL))
        got = _refresh(tr, _band(300, lambda i: i * 0.0005, _DVOL))
    finally:
        settings.chili_momentum_velocity_intake_enabled = old
    assert got == set()
    assert tr.drain_onset_receipts() == []
    assert tr.velocity_floor() is None


# ── ang hilera: DB round trip ───────────────────────────────────────────────


def test_migration_377_is_registered_once_with_a_free_id():
    from app.migrations import MIGRATIONS

    ids = [mid for mid, _fn in MIGRATIONS]
    assert ids.count("377_ignition_nomination_onset_receipt") == 1
    assert len(ids) == len(set(ids))


def test_onset_row_round_trips_with_source_cycle_and_receipt(db):
    """Ang bound na parameter ay dumadaan sa TUNAY na column type (JSONB kasama)."""
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


def _cleanup(symbols):
    from app.db import engine

    with engine.begin() as conn:
        for sym in symbols:
            conn.execute(
                text(f"DELETE FROM {_NOMINATIONS} WHERE symbol=:s"), {"s": sym}
            )
            conn.execute(text(f"DELETE FROM {_HINTS} WHERE symbol=:s"), {"s": sym})


def _onset(symbol, **over):
    out = {
        "symbol": symbol,
        "fired_at": datetime.now(timezone.utc),
        "last_price": 3.30,
        "dollar_vol_60s": 640_000.0,
        "pct_change_60s": None,
        "cycle_index": 0,
        "outcome": "snapshot_onset_admitted",
        "receipt": {"binding": "cross_section_rank", "rise_cut_pct": 12.5},
    }
    out.update(over)
    return out


def test_publish_writes_the_row_and_the_subscribe_hint_together(db):
    """Ang ebidensya at ang TAPE ay iisang sandali — magkasama sila sa transaksyon."""
    _cleanup(["FTFT"])
    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    tr = _UniverseTracker()
    tr._pending_onsets = [_onset("FTFT")]
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
        _cleanup(["FTFT"])


def test_the_row_and_the_hint_share_one_savepoint(db):
    """ANG DOCSTRING AY NANGAKO NG ATOMICITY; ANG CODE AY HINDI ITO GINAGAWA.

    Konkretong kaso: ang app ay na-deploy nang may bagong code laban sa isang DB
    na hindi pa tumatakbo ang mig 377 (normal — hiwalay ang sandali ng deploy at
    ng startup migration). Ang nomination INSERT ay bumabagsak, bumabalik sa
    SARILING savepoint, `recorded=False` — at ang hint ay NAGKA-COMMIT pa rin.
    Tape na walang ebidensya: ang mismong sinungaling na libro na ipinagbabawal
    ng docstring.
    """
    _cleanup(["BIAF"])
    broken = ir._INSERT_SQL.replace(
        "cycle_index, receipt", "cycle_index, receipt_column_that_does_not_exist"
    )
    orig = ir._INSERT_SQL
    try:
        ir._INSERT_SQL = broken
        out = ir.record_snapshot_onset(_onset("BIAF"))
        assert out["recorded"] is False
        assert out["subscribed"] is False
    finally:
        ir._INSERT_SQL = orig
    try:
        rows = db.execute(
            text(f"SELECT count(*) FROM {_NOMINATIONS} WHERE symbol='BIAF'")
        ).scalar()
        hints = db.execute(
            text(f"SELECT count(*) FROM {_HINTS} WHERE symbol='BIAF'")
        ).scalar()
        assert rows == 0
        assert hints == 0, "hint na walang ebidensya — sinungaling ang libro"
    finally:
        _cleanup(["BIAF"])


class _CommitFails:
    """Session na tumatanggap ng lahat at bumabagsak lamang sa `commit()`."""

    class _NestedCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def __init__(self):
        self.rolled_back = False

    def begin_nested(self):
        return self._NestedCtx()

    def execute(self, *a, **kw):
        class _R:
            def scalar(self_inner):
                return 0

        return _R()

    def commit(self):
        raise RuntimeError("serialization failure (simulated)")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def test_recorded_is_false_when_the_commit_fails():
    """`out['recorded']` ay dating naitatakda BAGO ang commit, kaya ang bawat
    bigong commit ay nag-uulat ng `recorded=True subscribed=True` para sa isang
    transaksyong na-rollback — at ang bilang na iyon ang binibilang ng
    `_publish_onset_receipts` bilang naisulat."""
    sess = _CommitFails()
    out = ir.record_snapshot_onset(_onset("ROLL"), session_factory=lambda: sess)
    assert out["recorded"] is False
    assert out["subscribed"] is False
    assert sess.rolled_back is True


def test_cycle_index_comes_from_the_ledger_not_the_process(db):
    """Ang in-process na counter ay namamatay kasama ng proseso: ang lane na
    nag-restart ng 11:00Z ay magsusulat ng `cycle_index=0` para sa pangalang may
    0,1,2 na sa session na iyon — at hindi ito maaayos sa pamamagitan ng
    pagbilang ng hilera dahil ang halaga mismo ang mali. Ang LIBRO ang sagot."""
    _cleanup(["FTFT"])
    now = datetime.now(timezone.utc)
    for idx in (0, 1):
        db.execute(
            text(
                f"INSERT INTO {_NOMINATIONS} "
                "(symbol, fired_at, received_at, outcome, source, cycle_index) "
                "VALUES ('FTFT', :t, :t, 'snapshot_onset_admitted', "
                "'snapshot_onset', :c)"
            ),
            {"t": now, "c": idx},
        )
    db.commit()
    try:
        out = ir.record_snapshot_onset(_onset("FTFT", fired_at=now, cycle_index=0))
        assert out["recorded"] is True
        assert out["cycle_index"] == 2, "ang libro ang nagbibilang, hindi ang proseso"
        assert out["cycle_index_source"] == "ledger"
        row = db.execute(
            text(
                "SELECT cycle_index, receipt->>'cycle_index_source' "
                f"FROM {_NOMINATIONS} WHERE symbol='FTFT' AND cycle_index=2"
            )
        ).fetchone()
        assert row is not None
        assert row[1] == "ledger"
    finally:
        _cleanup(["FTFT"])


def test_late_snapshot_cycle_uses_only_its_et_date_across_dst(db):
    """A delayed prior-date print cannot count later dates already in the ledger.

    These ET dates last 23 and 25 hours. A fixed 24-hour upper bound either
    includes the next date or loses the final hour of the intended date.
    """
    symbols = ["DSTSPR", "DSTFALL"]
    _cleanup(symbols)
    try:
        for symbol, month, day in ((symbols[0], 3, 8), (symbols[1], 11, 1)):
            local_start = datetime(2026, month, day, tzinfo=ir._ET)
            since = local_start.astimezone(timezone.utc)
            until = (local_start + timedelta(days=1)).astimezone(timezone.utc)
            # Both boundaries matter: previous day, first and final instants of
            # this day, then the next day (which is already in the ledger).
            times = (
                since - timedelta(microseconds=1),
                since,
                until - timedelta(microseconds=1),
                until,
                until + timedelta(hours=1),
            )
            for fired_at in times:
                db.execute(
                    text(
                        f"INSERT INTO {_NOMINATIONS} "
                        "(symbol, fired_at, received_at, outcome, source) "
                        "VALUES (:symbol, :at, :at, 'snapshot_onset_admitted', "
                        "'snapshot_onset')"
                    ),
                    {"symbol": symbol, "at": fired_at},
                )
            db.commit()
            cycle, source = ir.resolve_cycle_index(
                db, symbol, since + timedelta(hours=12), fallback=99
            )
            assert source == "ledger"
            assert cycle == 2, f"{symbol}: another ET date contaminated the cycle"
    finally:
        _cleanup(symbols)


def test_an_already_watched_onset_writes_no_hint(db):
    """Ang pangalang nasa screen na ay umaabot sa bridge sa pamamagitan ng ROSS
    source; ang HINT row para dito ay nagtutulak lamang sa kanya sa unahan ng
    SARILING universe niya sa capacity priority at sinasayang ang slot."""
    _cleanup(["RDHL"])
    try:
        out = ir.record_snapshot_onset(
            _onset("RDHL", outcome="snapshot_onset_already_watched")
        )
        assert out["recorded"] is True
        assert out["subscribed"] is False
        rows = db.execute(
            text(f"SELECT count(*) FROM {_NOMINATIONS} WHERE symbol='RDHL'")
        ).scalar()
        hints = db.execute(
            text(f"SELECT count(*) FROM {_HINTS} WHERE symbol='RDHL'")
        ).scalar()
        assert rows == 1
        assert hints == 0
    finally:
        _cleanup(["RDHL"])


# ── ang derive script ay hindi dapat malason ng bagong prodyuser ────────────


def _as_sqlalchemy(sql: str) -> str:
    """psycopg2 `%(name)s` -> SQLAlchemy `:name`, at `%%` -> `%`."""
    return sql.replace("%(start)s", ":start").replace("%(end)s", ":end").replace("%%", "%")


def test_governor_derivation_excludes_snapshot_onset_rows(db):
    """BEHAVIOURAL, hindi source-text.

    Ang unang bersyon ng test na ito ay binabasa ang script bilang teksto at
    hinahanap ang literal na `source = 'iqfeed_ignition'` sa loob ng bawat SQL
    block. Pumapasa iyon kung ang string ay nasa isang SQL comment, at bumabagsak
    kung ang parehong scoping ay isinulat bilang bound parameter o may ibang
    quoting — walang direksyon nito ang sumusukat kung TALAGANG naibubukod ang
    onset rows. Kaya ang mismong SQL ng script ang pinapatakbo dito laban sa
    magkahalong hilera.
    """
    import scripts.derive_ignition_governors as dg

    # PRIBADONG WINDOW. Ang `momentum_ignition_nominations` ay migration-owned,
    # kaya hindi ito tinatanggal ng `db` fixture at may mga naunang hilera:
    # ang assertion ay dapat tungkol sa MGA HILERANG ITO lamang.
    now = datetime(2031, 3, 5, 14, 22, 0, tzinfo=timezone.utc)
    start = now - timedelta(minutes=5)
    end = now + timedelta(minutes=5)
    rows = [
        ("IQFA", "iqfeed_ignition", "ross_event_admitted", 5.0),
        ("IQFB", "iqfeed_ignition", "ross_event_admitted", 7.0),
        ("ONSA", "snapshot_onset", "snapshot_onset_admitted", 900.0),
        ("ONSB", "snapshot_onset", "snapshot_onset_admitted", 900.0),
        ("ONSC", "snapshot_onset", "snapshot_onset_admitted", 900.0),
    ]
    _cleanup([r[0] for r in rows])
    for sym, source, outcome, lag_s in rows:
        db.execute(
            text(
                f"INSERT INTO {_NOMINATIONS} "
                "(symbol, fired_at, received_at, recorded_at, outcome, source) "
                "VALUES (:s, :t, :t, :r, :o, :src)"
            ),
            {
                "s": sym, "t": now, "r": now + timedelta(seconds=lag_s),
                "o": outcome, "src": source,
            },
        )
    db.flush()
    params = {"start": start, "end": end}

    minutes = db.execute(
        text(_as_sqlalchemy(dg.NOMINATION_MINUTES_SQL)), params
    ).fetchall()
    assert sum(int(r[1]) for r in minutes) == 2, "onset rows moved the arrival rate"

    lat = [
        float(r[0])
        for r in db.execute(
            text(_as_sqlalchemy(dg.ADMISSION_LATENCY_SQL)), params
        ).fetchall()
    ]
    assert sorted(lat) == [5.0, 7.0], "onset latency poisoned the TTL percentile"
    assert dg.percentile(sorted(lat), 0.9) < 900.0

    outcomes = {
        str(r[0]): int(r[1])
        for r in db.execute(
            text(_as_sqlalchemy(dg.OUTCOME_CENSUS_SQL)), params
        ).fetchall()
    }
    assert "snapshot_onset_admitted" not in outcomes
    assert outcomes.get("ross_event_admitted") == 2

    reasons = db.execute(
        text(_as_sqlalchemy(dg.UNIVERSE_REASON_CENSUS_SQL)), params
    ).fetchall()
    assert sum(int(r[1]) for r in reasons) == 2


def test_the_bridge_sql_and_the_pure_spec_agree_on_yielding_hints(db):
    """PARITY (CLAUDE.md): dalawang landas, PAREHONG input, PAREHONG output.

    Ang host bridge ay nananatiling standalone (walang app-package import doon),
    kaya ang pag-uuna ng hint ay nakasulat NANG DALAWANG BESES — sa SQL ng
    `_alert_symbols_read` at sa pure na `select_fresh_subscribe_symbols`. Ang
    ikalawa ay ang TESTED na espesipikasyon, kaya ang una ay sinusukat laban
    dito sa tunay na Postgres, hindi laban sa teksto ng sarili nitong source.
    """
    import scripts.iqfeed_depth_bridge as depth_bridge
    import scripts.iqfeed_trade_bridge as bridge
    from app.db import engine as test_engine
    from app.services.trading.momentum_neural.bridge_subscribe import (
        select_fresh_subscribe_symbols,
    )

    now = datetime.now(timezone.utc)
    rows = [
        ("ZZONSA", now - timedelta(seconds=1), "snapshot_onset"),
        ("ZZONSB", now - timedelta(seconds=2), "snapshot_onset"),
        ("ZZALTA", now - timedelta(seconds=60), "first_alert"),
        ("ZZALTB", now - timedelta(seconds=90), "first_alert"),
        ("ZZBOTH", now - timedelta(seconds=3), "snapshot_onset"),
        ("ZZBOTH", now - timedelta(seconds=120), "first_alert"),
    ]
    syms = sorted({r[0] for r in rows})
    _cleanup(syms)
    try:
        for sym, at, reason in rows:
            db.execute(
                text(
                    f"INSERT INTO {_HINTS} (symbol, requested_at, reason) "
                    "VALUES (:s, :at, :r)"
                ),
                {"s": sym, "at": at.replace(tzinfo=None), "r": reason},
            )
        db.commit()
        orig = bridge.engine
        orig_depth = depth_bridge.engine
        try:
            bridge.engine = test_engine
            depth_bridge.engine = test_engine
            l1_read = bridge._alert_symbols_read(180.0, limit=50)
            from_sql = list(l1_read.symbols)
            # ang L2 depth bridge ay may SARILING kopya ng parehong reader, at
            # mas kakaunti pa ang slot doon — parehong pagkakasunod-sunod
            l2_read = depth_bridge._alert_symbols_read(180.0, limit=50)
            from_depth = list(l2_read.symbols)
        finally:
            bridge.engine = orig
            depth_bridge.engine = orig_depth
        from_spec = select_fresh_subscribe_symbols(rows, now_utc=now)
        assert [s for s in from_sql if s in syms] == from_spec
        assert [s for s in from_depth if s in syms] == from_spec
        # ...at ang onset-only ay talagang nasa huli, ang BOTH ay hindi
        assert from_spec[-2:] == ["ZZONSA", "ZZONSB"]
        assert from_spec[0] == "ZZBOTH"
        # Sorting within HINT is insufficient: carry the reason through the
        # actual SQL reader into the final resolver across all source tiers.
        from scripts.iqfeed_subscription_policy import SourceRead, TargetCause

        for reader, resolver in (
            (l1_read, bridge._resolve_target),
            (l2_read, depth_bridge._resolve_target),
        ):
            reads = [
                SourceRead.success(TargetCause.ACTIVE, ()),
                reader,
                SourceRead.success(TargetCause.ROSS, ("ZZROSS",)),
                SourceRead.success(TargetCause.ELIGIBLE, ("ZZELIG",)),
            ]
            resolved = resolver(reads=reads, prior_causes={}, capacity=5)
            assert resolved.symbols == {"ZZBOTH", "ZZALTA", "ZZALTB", "ZZROSS", "ZZELIG"}
            reads[0] = SourceRead.failure(TargetCause.ACTIVE, error_code="blocked")
            resolved = resolver(
                reads=reads,
                prior_causes={"ZZPREV1": {TargetCause.ROSS}, "ZZPREV2": {TargetCause.ELIGIBLE}},
                capacity=5,
            )
            assert resolved.symbols == {"ZZBOTH", "ZZALTA", "ZZALTB", "ZZPREV1", "ZZPREV2"}
            assert resolved.retained_prior_on_failure
    finally:
        _cleanup(syms)


# -- ANG BULOK NA RESIBO AY HINDI PUMAPATAY NG IGNITION LOOP (verifier 09-11) --
#
# Ang `_publish_onset_receipts` ay may per-receipt na `try` mula pa sa unang
# bersyon - pero sa paligid LAMANG ng `record_snapshot_onset`. Ang drain, ang
# `except` handler mismo (`onset.get("symbol")`) at ang buong `_log.info`
# (dalawang `float()` sa hilaw na field ng resibo) ay nasa LABAS nito, kaya ang
# isang bulok na resibo ay umaakyat palabas ng method. DALAWA ang tumatawag at
# ang mas masakit ay WALANG guard: ang `start()` - kaya ang pagsabog doon ay
# hindi "isang nawawalang hilera" kundi isang ignition loop na hindi kailanman
# nagsimula (walang refresher thread, walang `_post_wake_session_refresh`).


def _fake_writer(seen, *, recorded=True):
    """Kapalit ng `record_snapshot_onset` na walang DB - itinatala ang narating."""

    def _write(onset):
        seen.append(onset)
        return {
            "recorded": recorded,
            "subscribed": recorded,
            "cycle_index": 0,
            "cycle_index_source": "ledger",
        }

    return _write


def _loop_with(pending):
    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    tr = _UniverseTracker()
    tr._pending_onsets = list(pending)
    loop._tracker = tr
    return loop, tr


def _onset_sym(onset):
    return onset.get("symbol") if isinstance(onset, dict) else onset


def test_a_poison_receipt_does_not_kill_the_publish_pass(db):
    """ANG TUNAY NA LANDAS: isang resibong hindi dict sa gitna ng pila.

    `record_snapshot_onset` ay nagre-raise ng AttributeError BAGO ang sarili
    nitong `try` (`str(onset.get("symbol"))`); nahuhuli iyon ng per-receipt na
    `except` - at ang handler mismo ay `onset.get("symbol")` DIN, kaya
    nagre-raise ULIT at ang pangalawang raise ang umaakyat palabas. Ang mga
    resibong nasa likod nito ay hindi kailanman naisusulat.
    """
    _cleanup(["PSNA", "PSNB"])
    loop, tr = _loop_with([_onset("PSNA"), "PSNB-is-not-a-dict", _onset("PSNB")])
    try:
        assert loop._publish_onset_receipts() == 2
        got = {
            str(r[0])
            for r in db.execute(
                text(
                    f"SELECT symbol FROM {_NOMINATIONS} "
                    "WHERE symbol IN ('PSNA', 'PSNB')"
                )
            ).fetchall()
        }
        assert got == {"PSNA", "PSNB"}, "ang resibo sa likod ng bulok ay nawala"
        assert tr.drain_onset_receipts() == [], "hindi naubos ang pila"
    finally:
        _cleanup(["PSNA", "PSNB"])


def test_a_poison_log_field_does_not_kill_the_publish_pass():
    """Ang LOG LINE mismo ang pumuputok, hindi ang pagsusulat.

    Tatlong hugis, lahat galing sa hilaw na resibo at lahat nasa labas ng dating
    `try`: hindi numero ang `receipt.rise_pct` (`float()` -> ValueError), hindi
    dict ang `receipt` (`.get` sa isang str -> AttributeError), at hindi numero
    ang `dollar_vol_60s`. Ang resibong nasa likod ay hindi kailanman naaabot.
    """
    seen: list = []
    pending = [
        _onset("LOGA", receipt={"binding": "cross_section_rank", "rise_pct": "n/a"}),
        _onset("LOGB", receipt="not-a-dict"),
        _onset("LOGC", dollar_vol_60s="lots"),
        _onset("LOGD"),
    ]
    loop, tr = _loop_with(pending)
    orig = ir.record_snapshot_onset
    try:
        ir.record_snapshot_onset = _fake_writer(seen)
        assert loop._publish_onset_receipts() == 4
    finally:
        ir.record_snapshot_onset = orig
    assert [_onset_sym(o) for o in seen] == ["LOGA", "LOGB", "LOGC", "LOGD"]
    assert tr.drain_onset_receipts() == []


def test_a_failing_drain_returns_zero_instead_of_raising():
    """Ang drain ay humahawak ng `self._lock` na hawak din ng WS receive path;
    kapag ang linyang iyon ang pumutok, ang `for` statement MISMO ang nagre-raise
    at wala man lang naabot na resibo para bilangin."""

    class _Blows:
        def drain_onset_receipts(self):
            raise RuntimeError("tracker lock blew up (simulated)")

    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    loop._tracker = _Blows()
    assert loop._publish_onset_receipts() == 0


def test_the_ignition_loop_still_starts_when_the_first_drain_blows_up():
    """ANG PRODUCTION SEVERITY, SINUKAT SA `start()` MISMO.

    Ang `start()` ay tumatawag ng `_publish_onset_receipts()` nang WALANG guard
    at BAGO nito simulan ang refresher thread at itakda ang
    `_post_wake_session_refresh`. Kaya ang isang bulok na resibo sa unang pull ay
    isang ignition loop na hindi kailanman nagsimula: walang universe refresh,
    walang session refresh, walang post-wake subscribe sync - habang ang
    scheduler job ay nag-uulat ng pagkabigo ng buong pagsisimula.
    """
    from app.config import settings

    class _Tracker:
        def refresh(self):
            return set()

        def drain_onset_receipts(self):
            raise RuntimeError("poison receipt (simulated)")

        def get_symbols(self):
            return set()

        def count(self):
            return 0

        def last_outcome(self):
            return "ok"

    class _Sessions:
        def refresh(self):
            return None

        def symbols(self):
            return set()

    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    loop._tracker = _Tracker()
    loop._sessions = _Sessions()
    loop._running = False
    loop._refresher = None
    loop._pool = None
    loop._subscribed = set()
    loop._last_heartbeat_mono = 0.0
    # ang bus ay hindi ang sinusukat dito
    loop._sync_subscriptions = lambda: None

    old_flag = getattr(settings, "chili_momentum_ws_ignition_enabled", False)
    old_hook = il._post_wake_session_refresh
    try:
        settings.chili_momentum_ws_ignition_enabled = True
        loop.start()
        assert loop._refresher is not None and loop._refresher.is_alive()
        assert il._post_wake_session_refresh is not None
    finally:
        loop._running = False
        il._post_wake_session_refresh = old_hook
        settings.chili_momentum_ws_ignition_enabled = old_flag
        if loop._pool is not None:
            loop._pool.shutdown(wait=False)
            loop._pool = None


# -- ang haba ng column ay PINANGALANAN, at ang pangalan ay nakapako sa schema --


def test_the_named_column_widths_are_the_real_column_widths(db):
    """NIT NG VERIFIER (09-11): ang `48`/`64`/`64`/`32`/`16` sa binder ay salamin
    ng DDL ng mig 376/377. Isang pangalan na lamang sila ngayon - at ang pangalan
    ay sinusukat laban sa TUNAY na `information_schema`, kaya ang isang ALTER na
    hindi umabot sa binder ay pumuputok DITO at hindi sa lane (kung saan ang
    `value too long for type character varying(N)` ay magpapabagsak ng BUONG
    INSERT, kasama ang subscribe hint na kasama nito sa iisang savepoint).
    """
    rows = db.execute(
        text(
            "SELECT column_name, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_name = :t AND character_maximum_length IS NOT NULL"
        ),
        {"t": _NOMINATIONS},
    ).fetchall()
    actual = {str(r[0]): int(r[1]) for r in rows}
    assert actual, "walang varchar column - mali ang pangalan ng table?"
    assert ir.NOMINATION_COLUMN_WIDTHS == actual


def test_an_overlong_value_is_cut_to_the_column_and_the_row_survives(db):
    """Ito ang binibili ng pagputol: isang mahabang halaga ay hindi
    nagpapabagsak ng INSERT."""
    now = datetime.now(timezone.utc)
    params = ignition_nomination_params(
        {"symbol": "LONG" + "X" * 40, "fired_at": now, "last_price": 1.0},
        received_at=now,
        outcome="snapshot_onset_" + "y" * 200,
        result={"skipped": "s" * 200, "ross_universe_reason": "r" * 200},
        source=SOURCE_SNAPSHOT_ONSET + "z" * 200,
    )
    widths = ir.NOMINATION_COLUMN_WIDTHS
    assert len(params["symbol"]) == widths["symbol"]
    assert len(params["outcome"]) == widths["outcome"]
    assert len(params["skipped"]) == widths["skipped"]
    assert len(params["ross_universe_reason"]) == widths["ross_universe_reason"]
    assert len(params["source"]) == widths["source"]
    try:
        assert write_ignition_nomination(db, params) is True
        db.flush()
    finally:
        db.execute(
            text(f"DELETE FROM {_NOMINATIONS} WHERE symbol = :s"),
            {"s": params["symbol"]},
        )
        db.flush()
