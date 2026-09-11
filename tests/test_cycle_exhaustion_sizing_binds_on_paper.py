"""[62] — ANG CYCLE-EXHAUSTION CONDITIONING AY DAPAT PUMUTOK SA PAPER LANE.

Ang aral ng "cooldown ay resibo lang": ang buong size-DOWN stack ay ibinabalik ng
`paper_full_size_floor` sa base kapag alpaca_spot + chili_alpaca_paper, kaya ang multiplier na
nasa PRODUCT lamang ay hindi kailanman nakakagalaw ng laki sa lane na tumatakbo ngayon (ganoon
nga ang nangyari sa day_open_ramp: INERT mula nang isilang hanggang sa 2026-08-18). Dito
sinusuri na (a) nasa product ito AT nasa risk_mults na resibo, (b) MULING ina-apply pagkatapos
ng floor, at (c) ang aritmetika mismo ng IPINADALANG block ay nagbibigay ng base x mult.

Ang block ay KINUKUHA MULA SA SOURCE at ini-exec — hindi ito muling isinusulat dito, kaya
hindi maaaring magkahiwalay ang test at ang code.
"""
from __future__ import annotations

import inspect
import textwrap
from typing import Any

import pytest

from app.config import Settings
from app.services.trading.momentum_neural.tape_cycles import (
    CYCLE_EXHAUSTION_FLOOR,
    CYCLE_EXHAUSTION_Q50,
    CYCLE_EXHAUSTION_Q90,
)

import app.services.trading.momentum_neural.live_runner as lr

SOURCE = inspect.getsource(lr.tick_live_session)


def _post_floor_block() -> str:
    """Ang IPINADALANG post-floor re-apply block, hiwalay mula sa source."""
    start = SOURCE.index("        # CYCLE-EXHAUSTION BINDS ON PAPER TOO")
    end = SOURCE.index("        # SHELF-REGISTRATION DAMPER", start)
    return textwrap.dedent(SOURCE[start:end])


# ── CONTROL FLOW: saan nakatayo ang multiplier ───────────────────────────────
def test_multiplier_is_in_the_pre_floor_product_and_in_the_receipt():
    i_prod = SOURCE.index("_safe_mult(_cycle_exhaustion_mult)")
    i_receipt = SOURCE.index('"cycle_exhaustion": round(float(_safe_mult(_cycle_exhaustion_mult))')
    i_floor = SOURCE.index('"paper_full_size_floor"')
    assert i_prod < i_floor  # nasa product BAGO ang floor
    assert i_receipt < i_floor


def test_reapplied_after_the_paper_floor_and_gated_on_it():
    i_floor = SOURCE.index('"paper_full_size_floor"')
    i_ramp = SOURCE.index('"day_open_risk_ramp_post_floor"')
    # hinahanap PAGKATAPOS ng ramp: ang unang banggit ng key ay nasa live_entry_filled payload
    i_cycle = SOURCE.index('le["cycle_exhaustion_post_floor"]', i_ramp)
    i_shelf = SOURCE.index('"shelf_registration_damper"')
    assert i_floor < i_ramp < i_cycle < i_shelf
    block = _post_floor_block()
    assert "_paper_floor_fired" in block  # walang double-apply sa real-money path
    assert "_cycle_exhaustion_mult" in block


def test_feed_runs_on_every_tick_before_the_state_machine():
    """Ang ledger ay dapat umaandar habang WATCHING pa lang — ang cycle index sa entry ay
    kasaysayan ng BUONG symbol-day, hindi ng huling ilang segundo."""
    i_feed = SOURCE.index("_feed_tape_cycle_state(db, sess, le)")
    i_tick = SOURCE.index('le["tick_count"]')
    i_state = SOURCE.index("    st = sess.state")
    assert i_tick < i_feed < i_state


def test_entry_filled_payload_carries_the_receipt():
    i_evt = SOURCE.index('"live_entry_filled"')
    i_cycle = SOURCE.index('"cycle_exhaustion": le.get("cycle_exhaustion")', i_evt)
    i_post = SOURCE.index('"cycle_exhaustion_post_floor": le.get("cycle_exhaustion_post_floor")', i_evt)
    assert i_evt < i_cycle < i_evt + 4000
    assert i_evt < i_post < i_evt + 4000


# ── ARITMETIKA: ang ipinadalang block, ini-exec ──────────────────────────────
def _run_block(*, paper_floor_fired: bool, mult: float, eff: float, base: float) -> dict[str, Any]:
    le: dict[str, Any] = {"cycle_exhaustion": {"cycle_index": 7, "score": 0.66}}
    ns: dict[str, Any] = {
        "_paper_floor_fired": paper_floor_fired,
        "_cycle_exhaustion_mult": mult,
        "_eff_max_loss": eff,
        "le": le,
    }
    exec(compile(_post_floor_block(), "<post_floor>", "exec"), ns, ns)  # noqa: S102 — shipped source
    return {"eff": ns["_eff_max_loss"], "le": le, "base": base}


def test_paper_floor_restores_base_then_the_conditioning_multiplies_it_again():
    """Ang eksaktong daan sa paper: base 390.00 -> stacked 195.00 -> floor -> 390.00 ->
    cycle-exhaustion 0.3125 -> 121.88. Kung wala ang re-apply, 390.00 ito (resibo lamang)."""
    base = 390.0
    stacked = base * 0.5
    assert stacked < base
    after_floor = base  # ito ang ginagawa ng paper_full_size_floor
    out = _run_block(paper_floor_fired=True, mult=CYCLE_EXHAUSTION_FLOOR, eff=after_floor, base=base)
    assert out["eff"] == pytest.approx(base * CYCLE_EXHAUSTION_FLOOR)
    rec = out["le"]["cycle_exhaustion_post_floor"]
    assert rec["mult"] == pytest.approx(CYCLE_EXHAUSTION_FLOOR)
    assert rec["effective_usd"] == pytest.approx(round(base * CYCLE_EXHAUSTION_FLOOR, 2))
    assert rec["cycle_index"] == 7
    assert rec["score"] == 0.66


def test_no_reapply_when_the_floor_did_not_fire():
    """Ang real-money path ay hindi dumadaan sa floor — ang multiplier ay nasa product na,
    kaya walang double-apply dito."""
    out = _run_block(paper_floor_fired=False, mult=0.5, eff=200.0, base=390.0)
    assert out["eff"] == pytest.approx(200.0)
    assert "cycle_exhaustion_post_floor" not in out["le"]


def test_no_reapply_at_full_size():
    out = _run_block(paper_floor_fired=True, mult=1.0, eff=390.0, base=390.0)
    assert out["eff"] == pytest.approx(390.0)
    assert "cycle_exhaustion_post_floor" not in out["le"]


def test_block_never_raises_on_a_broken_multiplier():
    out = _run_block(paper_floor_fired=True, mult=float("nan"), eff=390.0, base=390.0)
    assert out["eff"] == pytest.approx(390.0)  # NaN comparisons are False => walang apply


# ── ANG BINDING NA HALAGA ────────────────────────────────────────────────────
def test_settings_defaults_are_the_reported_bindings_and_there_is_no_enabled_knob():
    from app.services.trading.momentum_neural.tape_cycles import CYCLE_PULLBACK_FRAC_BASE

    # Ang praksyon ay nakalantad bilang setting at ang default ay ang SINUKAT na halaga.
    frac = Settings.model_fields["chili_momentum_cycle_pullback_frac"]
    assert 0.0 < float(frac.default) < 1.0
    assert float(frac.default) == CYCLE_PULLBACK_FRAC_BASE
    assert "DERIVATION" in (frac.description or "")
    feed = Settings.model_fields["chili_momentum_cycle_feed_max_prints"]
    assert int(feed.default) == 5000
    assert "DERIVATION" in (feed.description or "")
    # WALANG dark flag: walang `enabled` na knob para sa mekanismong ito.
    assert not [
        n for n in Settings.model_fields if n.startswith("chili_momentum_cycle_") and n.endswith("_enabled")
    ]


def test_receipt_is_written_on_every_pass_with_the_binding_keys():
    """Kahit full-size ang arm, may talaan — kung hindi, hindi masusukat ang divergence
    (ang aral ng frontside_size_tilt 2026-09-07)."""
    i_sz = SOURCE.index('le["cycle_exhaustion"] = {')
    block = SOURCE[i_sz : SOURCE.index("# LOW-7: sanitize EACH per-factor multiplier", i_sz)]
    for key in (
        '"cycle_index"',
        '"in_pullback"',
        '"pos_in_range"',
        '"ext_x_amp0"',
        '"amp_ratio"',
        '"rate_ratio"',
        '"buy_share_delta"',
        '"prints_since_high"',
        '"score"',
        '"mult"',
        '"binding"',
        '"pullback_frac"',
        '"q50_score"',
        '"q90_score"',
        '"floor"',
        '"derived_from"',
        '"tape_caught_up"',
    ):
        assert key in block, key
    # Ang legacy na ugali ay may PANGALANG dahilan, hindi katahimikan.
    assert "no_tape_state" in block
    # Hindi ito naka-gate sa "kapag kumagat lang".
    assert "if _cycle_exhaustion_mult < 1.0" not in block


def test_the_ramp_anchors_come_from_the_module_not_from_literals_in_the_runner():
    i_sz = SOURCE.index("_cycle_exhaustion_mult, _ce_mdbg = cycle_exhaustion_size_multiplier(")
    call = SOURCE[i_sz : i_sz + 400]
    assert "CYCLE_EXHAUSTION_Q50" in call
    assert "CYCLE_EXHAUSTION_Q90" in call
    assert CYCLE_EXHAUSTION_Q50 < CYCLE_EXHAUSTION_Q90
    assert 0.0 < CYCLE_EXHAUSTION_FLOOR < 1.0


def test_floor_is_never_below_the_documented_frontside_floor():
    i = SOURCE.index("_ce_floor = max(")
    block = SOURCE[i : i + 300]
    assert "CYCLE_EXHAUSTION_FLOOR" in block
    assert "chili_momentum_frontside_size_floor" in SOURCE[i - 400 : i + 400]
    assert CYCLE_EXHAUSTION_FLOOR >= Settings.model_fields["chili_momentum_frontside_size_floor"].default


# ── ANG FEED MISMO (bounded, paunti-unti, JSON-safe) ─────────────────────────
class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _TapeDB:
    """Tumutugon sa CURSOR na anyo ng feed query. Walang begin_nested (gaya ng maliliit na
    fake sa suite) kaya direktang execute ang daan."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def execute(self, statement, params=None):
        p = dict(params or {})
        self.calls += 1
        last_at, last_id, n, as_of = p["last_at"], int(p["last_id"]), int(p["n"]), p["as_of"]
        out = [
            r
            for r in self._rows
            if r[0] <= as_of and (r[0] > last_at or (r[0] == last_at and r[1] > last_id))
        ]
        return _Res(out[:n])


class _Sess:
    symbol = "TSTX"
    id = 1


def _relative_tape(n=40):
    from datetime import timedelta as _td

    now = lr._utcnow()
    px = [1.00, 1.40, 1.10, 1.41, 1.80, 1.05, 1.81, 1.90, 1.60, 1.91]
    px = (px * ((n // len(px)) + 1))[:n]
    rows = []
    for i, v in enumerate(px):
        ts = now - _td(seconds=(n - i))
        rows.append((ts, i + 1, float(v), 100.0, v - 0.01, v))
    return rows


def test_feed_accumulates_across_ticks_and_reports_catch_up(monkeypatch):
    """Ang catch-up ay may hangganan KADA TICK (CYCLE_FEED_READS_PER_TICK na pagbasa), kaya
    kapag napakalayo ng ledger ay HINDI ito nagpapanggap na kumpleto: `caught_up` ay False at
    nasa resibo ito."""
    from app.services.trading.momentum_neural.tape_cycles import CYCLE_FEED_READS_PER_TICK

    rows = _relative_tape(40)
    db = _TapeDB(rows)
    le: dict[str, Any] = {}
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 2, raising=False)
    st = lr._feed_tape_cycle_state(db, _Sess(), le)
    assert st is not None
    assert st["feed"]["reads"] == CYCLE_FEED_READS_PER_TICK
    assert st["n_prints"] == 2 * CYCLE_FEED_READS_PER_TICK
    assert st["feed"]["caught_up"] is False
    assert le["tape_cycle_state"] is st
    # sunod-sunod na tick: sumusulong ang cursor hanggang maabutan ang tape
    for _ in range(10):
        st = lr._feed_tape_cycle_state(db, _Sess(), le)
        if st["feed"]["caught_up"]:
            break
    assert st["n_prints"] == 40
    assert st["feed"]["caught_up"] is True
    assert st["n_cycles"] >= 1
    import json

    json.dumps(st)  # nakatira ito sa risk_snapshot_json — dapat JSON-safe


def test_one_tick_catches_up_a_bounded_backlog(monkeypatch):
    """40,000 print kada tick sa default (5,000 x 8): ang p90 na symbol-day (346,769) ay
    naaabutan sa <= 9 tick, hindi sa 70."""
    rows = _relative_tape(40)
    db = _TapeDB(rows)
    le: dict[str, Any] = {}
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 10, raising=False)
    st = lr._feed_tape_cycle_state(db, _Sess(), le)
    assert st["n_prints"] == 40
    assert st["feed"]["caught_up"] is True
    assert st["feed"]["reads"] <= 5  # 4 na puno + 1 na kulang


def test_feed_state_restarts_when_the_pullback_fraction_changes(monkeypatch):
    rows = _relative_tape(20)
    db = _TapeDB(rows)
    le: dict[str, Any] = {}
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 5000, raising=False)
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_pullback_frac", 0.50, raising=False)
    st = lr._feed_tape_cycle_state(db, _Sess(), le)
    assert st["n_prints"] == 20
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_pullback_frac", 0.25, raising=False)
    st2 = lr._feed_tape_cycle_state(db, _Sess(), le)
    assert st2["pullback_frac"] == 0.25
    assert st2["n_prints"] == 20  # muling binasa mula sa simula ng araw, hindi nagpatuloy sa 40


def test_feed_skips_crypto_and_never_raises_on_a_dead_db():
    class _Crypto:
        symbol = "BTC-USD"
        id = 2

    assert lr._feed_tape_cycle_state(_TapeDB([]), _Crypto(), {}) is None

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db exploded")

    le: dict[str, Any] = {}
    st = lr._feed_tape_cycle_state(_Boom(), _Sess(), le)
    assert st is not None and st["feed"]["reason"] == "read_failed"
    assert st["n_prints"] == 0
