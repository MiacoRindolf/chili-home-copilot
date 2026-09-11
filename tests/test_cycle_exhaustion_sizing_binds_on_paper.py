"""[62] — ANG CYCLE-EXHAUSTION CONDITIONING AY DAPAT PUMUTOK SA PAPER LANE.

Ang aral ng "cooldown ay resibo lang": ang buong size-DOWN stack ay ibinabalik ng
`paper_full_size_floor` sa base kapag alpaca_spot + chili_alpaca_paper, kaya ang multiplier na
nasa PRODUCT lamang ay hindi kailanman nakakagalaw ng laki sa lane na tumatakbo ngayon (ganoon
nga ang nangyari sa day_open_ramp: INERT mula nang isilang hanggang sa 2026-08-18).

⚠️ MULING ISINULAT (refuter, 2026-09-11). Pito sa labing-anim na test dito ay
`inspect.getsource(...).index(...)` na paghahambing ng OFFSET — kabilang ang isang
`assert i_evt < i_post < i_evt + 4000` (distansya sa karakter). Ang
`test_feed_runs_on_every_tick_before_the_state_machine` ay papasa pa rin kahit ilagay ang
tawag sa loob ng `if False:`. WALANG isa sa kanila ang nagpapatakbo ng mekanismo. Ngayon:
ang conditioning ay HIWALAY nang function (`_cycle_exhaustion_conditioning`) at DINADRAYB dito
ng tunay na ledger; ang feed ay dinadrayb laban sa TUNAY na Postgres (ang `db` fixture) kaya
ang SAVEPOINT, ang `SET LOCAL statement_timeout` at ang mismong predicate ay tumatakbo; at ang
post-floor na aritmetika ay ini-exec pa rin MULA SA IPINADALANG SOURCE.
"""
from __future__ import annotations

import inspect
import textwrap
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.config import Settings
from app.services.trading.momentum_neural.tape_cycles import (
    CYCLE_EXHAUSTION_FLOOR,
    CYCLE_EXHAUSTION_Q50,
    CYCLE_EXHAUSTION_Q90,
    CYCLE_EXHAUSTION_TERMS,
    CYCLE_FEED_BUDGET_MS,
    CYCLE_FEED_READS_PER_TICK,
    CYCLE_FEED_STATEMENT_TIMEOUT_MS,
    PullbackCycleScanner,
    feed_scanner_from_db,
)

import app.services.trading.momentum_neural.live_runner as lr

SOURCE = inspect.getsource(lr.tick_live_session)
T0 = datetime(2026, 9, 10, 13, 30, 0)


def _post_floor_block() -> str:
    """Ang IPINADALANG post-floor re-apply block, hiwalay mula sa source."""
    start = SOURCE.index("        # ⚠️ BUBURAHIN MUNA")
    end = SOURCE.index("        # SHELF-REGISTRATION DAMPER", start)
    return textwrap.dedent(SOURCE[start:end])


def _tape(prices, *, buys=None, step_ms=250, start_id=1):
    rows = []
    for i, px in enumerate(prices):
        is_buy = True if buys is None else bool(buys[i])
        bid = px - 0.01 if is_buy else px
        ask = px if is_buy else px + 0.01
        rows.append((T0 + timedelta(milliseconds=step_ms * i), start_id + i, float(px), 100.0, bid, ask))
    return rows


def _ledger(prices, *, buys=None, caught_up=True, frac=0.5) -> dict[str, Any]:
    """Isang `tape_cycle_state` na gaya ng isinusulat ng `_feed_tape_cycle_state`."""
    sc = PullbackCycleScanner(frac)
    sc.feed(_tape(prices, buys=buys))
    st = sc.to_dict()
    st["day"] = "2026-09-10"
    st["feed"] = {"fed": sc.n_prints, "reads": 1, "caught_up": bool(caught_up)}
    return st


# TIRED: maliit ang unang amplitude (malaki ang ext_x_amp0), mababa sa saklaw ang presyo,
# mababaw ang huling pullback, at sell-side ang kasalukuyang spike.
_TIRED = (
    [1.00, 1.01, 1.005, 1.011]                        # cycle 0: amp0 = 0.01
    + [1.60, 1.30, 1.61]                              # cycle 1: amp 0.595, pb depth 0.504
    + [2.00, 1.615, 2.01]                             # cycle 2: amp 0.70, MABABAW na pb (0.55)
    + [1.60, 1.50, 1.45, 1.40]                        # kasalukuyan: pababa, sell-side
)
_TIRED_BUYS = [True] * 10 + [False] * 4


# ── ANG MEKANISMO MISMO: DINADRAYB, HINDI BINABASA ──────────────────────────
def test_a_tired_ledger_sizes_down_and_the_receipt_carries_every_binding():
    le = {"tape_cycle_state": _ledger(_TIRED, buys=_TIRED_BUYS)}
    mult, rec = lr._cycle_exhaustion_conditioning(le, mid=1.60)
    assert mult < 1.0
    assert mult >= CYCLE_EXHAUSTION_FLOOR
    assert rec["mult"] == pytest.approx(round(mult, 4))
    assert rec["score"] is not None and rec["score"] > CYCLE_EXHAUSTION_Q50
    assert rec["reason"] is None
    assert rec["tape_caught_up"] is True
    assert rec["cycle_index"] >= 2
    assert rec["detail"]["n_terms"] == len(CYCLE_EXHAUSTION_TERMS)
    b = rec["binding"]
    assert b["q50_score"] == pytest.approx(CYCLE_EXHAUSTION_Q50)
    assert b["q90_score"] == pytest.approx(CYCLE_EXHAUSTION_Q90)
    assert b["floor"] >= CYCLE_EXHAUSTION_FLOOR
    assert b["min_terms"] == len(CYCLE_EXHAUSTION_TERMS)
    assert b["pullback_frac"] == pytest.approx(0.5)
    assert "derived_from" in b
    import json

    json.dumps(rec)  # nakatira ito sa risk_snapshot_json at sa event payload


def test_a_fresh_ledger_is_not_sized_down():
    """Presyo sa TAAS ng saklaw, malalim na huling pullback, buy-side ang kasalukuyang spike."""
    px = [1.00, 1.30, 1.05, 1.31, 1.60, 1.20, 1.61, 1.80, 1.79]
    le = {"tape_cycle_state": _ledger(px)}
    mult, rec = lr._cycle_exhaustion_conditioning(le, mid=1.79)
    assert mult == pytest.approx(1.0)
    assert rec["score"] is not None and rec["score"] <= CYCLE_EXHAUSTION_Q50
    assert rec["detail"]["n_terms"] == len(CYCLE_EXHAUSTION_TERMS)


def test_zero_completed_cycles_is_never_the_maximum_size_cut():
    """ANG BUTAS: ang `ext_x_amp0` at `last_pb_depth_ratio` ay STRUKTURAL na None hanggang
    matapos ang UNANG cycle, kaya ang dating 2-terminong average ay nagpapadala sa
    PINAKASARIWANG tape (cycle_index 0) sa mismong floor na inilalaan sa pinakapagod."""
    px = [1.00, 1.50, 2.00, 1.70, 1.45, 1.50, 1.50, 1.50]
    buys = [True, True, True, False, False, False, False, False]
    le = {"tape_cycle_state": _ledger(px, buys=buys)}
    mult, rec = lr._cycle_exhaustion_conditioning(le, mid=1.50)
    assert rec["cycle_index"] == 0
    assert rec["ext_x_amp0"] is None and rec["last_pb_depth_ratio"] is None
    assert mult == pytest.approx(1.0)              # WALANG conditioning
    assert rec["score"] is None
    assert rec["reason"] == "insufficient_terms"   # ...at may PANGALANG dahilan
    assert rec["detail"]["n_terms"] == 2
    assert rec["detail"]["min_terms"] == len(CYCLE_EXHAUSTION_TERMS)


def test_a_ledger_that_is_still_backfilling_does_not_decide_the_size():
    """Ang hindi pa naaabutang ledger ay naglalarawan ng NAUNANG bahagi ng araw. Ang
    `tape_caught_up` ay iniuulat na noon pa — ngayon ay nagdedesisyon na ito."""
    st = _ledger(_TIRED, buys=_TIRED_BUYS, caught_up=False)
    mult, rec = lr._cycle_exhaustion_conditioning({"tape_cycle_state": st}, mid=1.60)
    assert mult == pytest.approx(1.0)
    assert rec["score"] is None
    assert rec["reason"] == "tape_not_caught_up"
    assert rec["tape_caught_up"] is False
    # ...pero ang features ay iniuulat pa rin, kaya masusukat ang divergence.
    assert rec["cycle_index"] >= 2
    assert rec["pos_in_range"] is not None
    # KATUMBAS na ledger na NAABUTAN na: doon lamang kumakagat.
    st2 = dict(st)
    st2["feed"] = dict(st["feed"], caught_up=True)
    mult2, _ = lr._cycle_exhaustion_conditioning({"tape_cycle_state": st2}, mid=1.60)
    assert mult2 < 1.0


def test_the_score_reads_the_last_print_not_the_quote_mid():
    """Doktrina (at ang docstring ng modyul): ang tick ang sumasagot. Ang derivation ay
    nag-i-score sa presyo ng PRINT; ang lane na ito ay may sinukat na `wide_bbo_spread` na
    33.9 bps laban sa 16.9 na cap, kaya ang mid ay ibang dami."""
    st = _ledger(_TIRED, buys=_TIRED_BUYS)
    last_print = st["last_px"]
    a_mult, a_rec = lr._cycle_exhaustion_conditioning({"tape_cycle_state": st}, mid=1.60)
    b_mult, b_rec = lr._cycle_exhaustion_conditioning({"tape_cycle_state": st}, mid=1.95)
    assert a_rec["price_source"] == "last_print"
    assert a_rec["scored_price"] == pytest.approx(last_print)
    assert a_rec["quote_mid"] == pytest.approx(1.60)
    assert b_rec["quote_mid"] == pytest.approx(1.95)
    # Ang mid ay iniuulat pero HINDI nagdedesisyon: pareho ang score at ang laki.
    assert a_rec["score"] == b_rec["score"]
    assert a_rec["pos_in_range"] == b_rec["pos_in_range"]
    assert a_mult == pytest.approx(b_mult)


def test_conditioning_fails_open_with_a_named_reason_when_there_is_no_ledger():
    mult, rec = lr._cycle_exhaustion_conditioning({}, mid=1.0)
    assert mult == pytest.approx(1.0)
    assert rec["reason"] == "no_tape_state"
    assert rec["price_source"] == "none"
    assert rec["binding"]["floor"] >= CYCLE_EXHAUSTION_FLOOR
    mult2, rec2 = lr._cycle_exhaustion_conditioning({"tape_cycle_state": {"n_prints": 0}}, mid=1.0)
    assert mult2 == pytest.approx(1.0)
    assert rec2["reason"] == "no_tape_state"


def test_the_floor_never_vetoes_even_on_the_most_exhausted_tape(monkeypatch):
    monkeypatch.setattr(lr.settings, "chili_momentum_frontside_size_floor", 0.25, raising=False)
    le = {"tape_cycle_state": _ledger(_TIRED, buys=_TIRED_BUYS)}
    mult, rec = lr._cycle_exhaustion_conditioning(le, mid=1.60)
    assert mult > 0.0
    assert mult >= CYCLE_EXHAUSTION_FLOOR
    assert rec["binding"]["floor"] == pytest.approx(CYCLE_EXHAUSTION_FLOOR)


# ── CONTROL FLOW: saan nakatayo ang multiplier (mga hindi maipapahayag sa ibang paraan) ──
def test_multiplier_is_in_the_pre_floor_product_and_in_the_receipt():
    i_prod = SOURCE.index("_safe_mult(_cycle_exhaustion_mult)")
    i_receipt = SOURCE.index('"cycle_exhaustion": round(float(_safe_mult(_cycle_exhaustion_mult))')
    i_floor = SOURCE.index('"paper_full_size_floor"')
    assert i_prod < i_floor  # nasa product BAGO ang floor
    assert i_receipt < i_floor


def test_reapplied_after_the_paper_floor_and_gated_on_it():
    i_floor = SOURCE.index('"paper_full_size_floor"')
    i_ramp = SOURCE.index('"day_open_risk_ramp_post_floor"')
    i_cycle = SOURCE.index('le["cycle_exhaustion_post_floor"]', i_ramp)
    i_shelf = SOURCE.index('"shelf_registration_damper"')
    assert i_floor < i_ramp < i_cycle < i_shelf
    block = _post_floor_block()
    assert "_paper_floor_fired" in block  # walang double-apply sa real-money path
    assert "_cycle_exhaustion_mult" in block


def test_the_feed_is_skipped_once_a_position_is_held():
    """Ang ledger ay binabasa LAMANG ng entry sizing, kaya ang isang HELD na sesyon ay hindi
    dapat magbayad ng DB na oras sa UNAHAN ng stop/trail/scale-out sa loob ng parehong
    FOR UPDATE na lock (ang hugis ng 2026-08-19 na 10.8-minutong pagka-freeze)."""
    for st in (
        lr.STATE_WATCHING_LIVE,
        lr.STATE_LIVE_ENTRY_CANDIDATE,
        lr.STATE_LIVE_PENDING_ENTRY,
        lr.STATE_QUEUED_LIVE,
    ):
        assert st in lr._TAPE_CYCLE_FEED_STATES, st
    for st in (
        lr.STATE_LIVE_ENTERED,
        lr.STATE_LIVE_SCALING_OUT,
        lr.STATE_LIVE_TRAILING,
        lr.STATE_LIVE_BAILOUT,
        lr.STATE_LIVE_EXITED,
    ):
        assert st not in lr._TAPE_CYCLE_FEED_STATES, st
    # ...at ang gate ay ang MISMONG kondisyon na ipinadala, hindi isang kopya.
    assert "if sess.state in _TAPE_CYCLE_FEED_STATES:" in SOURCE
    i_gate = SOURCE.index("if sess.state in _TAPE_CYCLE_FEED_STATES:")
    assert i_gate < SOURCE.index("    st = sess.state")


def test_entry_filled_payload_carries_the_receipt():
    i_evt = SOURCE.index('"live_entry_filled"')
    assert '"cycle_exhaustion": le.get("cycle_exhaustion")' in SOURCE[i_evt:]
    assert '"cycle_exhaustion_post_floor": le.get("cycle_exhaustion_post_floor")' in SOURCE[i_evt:]


# ── ARITMETIKA: ang ipinadalang block, ini-exec ──────────────────────────────
def _run_block(*, paper_floor_fired: bool, mult: float, eff: float, le: dict | None = None) -> dict[str, Any]:
    le = {"cycle_exhaustion": {"cycle_index": 7, "score": 0.66}} if le is None else le
    ns: dict[str, Any] = {
        "_paper_floor_fired": paper_floor_fired,
        "_cycle_exhaustion_mult": mult,
        "_eff_max_loss": eff,
        "le": le,
    }
    exec(compile(_post_floor_block(), "<post_floor>", "exec"), ns, ns)  # noqa: S102 — shipped source
    return {"eff": ns["_eff_max_loss"], "le": le}


def test_paper_floor_restores_base_then_the_conditioning_multiplies_it_again():
    """Ang eksaktong daan sa paper: base 390.00 -> stacked 195.00 -> floor -> 390.00 ->
    cycle-exhaustion 0.3125 -> 121.88. Kung wala ang re-apply, 390.00 ito (resibo lamang)."""
    base = 390.0
    out = _run_block(paper_floor_fired=True, mult=CYCLE_EXHAUSTION_FLOOR, eff=base)
    assert out["eff"] == pytest.approx(base * CYCLE_EXHAUSTION_FLOOR)
    rec = out["le"]["cycle_exhaustion_post_floor"]
    assert rec["mult"] == pytest.approx(CYCLE_EXHAUSTION_FLOOR)
    assert rec["effective_usd"] == pytest.approx(round(base * CYCLE_EXHAUSTION_FLOOR, 2))
    assert rec["cycle_index"] == 7
    assert rec["score"] == 0.66


def test_no_reapply_when_the_floor_did_not_fire():
    out = _run_block(paper_floor_fired=False, mult=0.5, eff=200.0)
    assert out["eff"] == pytest.approx(200.0)
    assert "cycle_exhaustion_post_floor" not in out["le"]


def test_no_reapply_at_full_size():
    out = _run_block(paper_floor_fired=True, mult=1.0, eff=390.0)
    assert out["eff"] == pytest.approx(390.0)
    assert "cycle_exhaustion_post_floor" not in out["le"]


def test_block_never_raises_on_a_broken_multiplier():
    out = _run_block(paper_floor_fired=True, mult=float("nan"), eff=390.0)
    assert out["eff"] == pytest.approx(390.0)  # NaN comparisons are False => walang apply


def test_the_post_floor_receipt_never_survives_into_a_full_size_leg():
    """Ang depekto ng `frontside_size_tilt` (ayos 2026-09-07), naabot muli: isinusulat lamang
    kapag kumakagat, hindi kailanman binubura. Ang leg 2 (full size, $390.00) ay magdadala ng
    resibo ng leg 1 na nagsasabing $121.88 ang tinaya."""
    le: dict[str, Any] = {"cycle_exhaustion": {"cycle_index": 7, "score": 0.72}}
    first = _run_block(paper_floor_fired=True, mult=CYCLE_EXHAUSTION_FLOOR, eff=390.0, le=le)
    assert first["le"]["cycle_exhaustion_post_floor"]["effective_usd"] == pytest.approx(121.88)
    # ...susunod na pass (re-peg o bagong leg): bumaba ang score, mult 1.0
    le["cycle_exhaustion"] = {"cycle_index": 1, "score": 0.27}
    second = _run_block(paper_floor_fired=True, mult=1.0, eff=390.0, le=le)
    assert second["eff"] == pytest.approx(390.0)
    assert "cycle_exhaustion_post_floor" not in second["le"]


def test_both_receipts_are_cleared_on_recycle_but_the_symbol_day_ledger_is_not():
    keys = lr._RECYCLE_ENTRY_STATE_KEYS
    assert "cycle_exhaustion" in keys
    assert "cycle_exhaustion_post_floor" in keys
    assert "tape_cycle_state" not in keys  # symbol-day, hindi per-trade


# ── ANG BINDING NA HALAGA ────────────────────────────────────────────────────
def test_settings_defaults_are_the_reported_bindings_and_there_is_no_enabled_knob():
    from app.services.trading.momentum_neural.tape_cycles import CYCLE_PULLBACK_FRAC_BASE

    frac = Settings.model_fields["chili_momentum_cycle_pullback_frac"]
    assert 0.0 < float(frac.default) < 1.0
    assert float(frac.default) == CYCLE_PULLBACK_FRAC_BASE
    assert "DERIVATION" in (frac.description or "")
    # Ang derivation text ay dapat sumusuporta sa IPINADALANG halaga, hindi sa kabaligtaran
    # (refuter: ang dating teksto ay nangangatwiran para sa 0.25 habang 0.50 ang default).
    assert "Kaya 0.50 ang default" in (frac.description or "")
    feed = Settings.model_fields["chili_momentum_cycle_feed_max_prints"]
    assert int(feed.default) == 5000
    assert "DERIVATION" in (feed.description or "")
    assert not [
        n for n in Settings.model_fields if n.startswith("chili_momentum_cycle_") and n.endswith("_enabled")
    ]


def test_the_receipt_reports_every_key_the_forward_measurement_needs():
    """Kahit full-size ang arm, may talaan — kung hindi, hindi masusukat ang divergence."""
    _, rec = lr._cycle_exhaustion_conditioning(
        {"tape_cycle_state": _ledger(_TIRED, buys=_TIRED_BUYS)}, mid=1.60
    )
    for key in (
        "cycle_index", "in_pullback", "pos_in_range", "ext_x_amp0", "amp_ratio", "rate_ratio",
        "buy_share_delta", "cur_buy_share", "prints_since_high", "last_pb_depth_ratio",
        "n_prints", "score", "mult", "reason", "detail", "tape_caught_up", "scored_price",
        "price_source", "quote_mid", "binding", "ramp",
    ):
        assert key in rec, key


def test_floor_is_never_below_the_documented_frontside_floor(monkeypatch):
    monkeypatch.setattr(lr.settings, "chili_momentum_frontside_size_floor", 0.10, raising=False)
    _, rec = lr._cycle_exhaustion_conditioning({}, mid=1.0)
    assert rec["binding"]["floor"] == pytest.approx(CYCLE_EXHAUSTION_FLOOR)
    assert CYCLE_EXHAUSTION_FLOOR >= Settings.model_fields["chili_momentum_frontside_size_floor"].default


# ── ANG FEED MISMO (bounded, paunti-unti, JSON-safe) ─────────────────────────
class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _Savepoint:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        self.owner.savepoints += 1
        return self

    def __exit__(self, *exc):
        return False


class _TapeDB:
    """Tumutugon sa CURSOR na anyo ng feed query. May `begin_nested` (gaya ng tunay na
    Session) kaya TUMATAKBO ang SAVEPOINT na daan sa `optional_fetchall` — ang dating fake
    ay walang nito, kaya hindi kailanman nasusuri ang wrapper."""

    def __init__(self, rows, *, dialect="sqlite"):
        self._rows = rows
        self.calls = 0
        self.savepoints = 0
        self.timeouts: list[str] = []
        self._dialect = dialect

    def begin_nested(self):
        return _Savepoint(self)

    def get_bind(self):
        class _D:
            name = self._dialect

        class _B:
            dialect = _D()

        return _B()

    def execute(self, statement, params=None):
        sql = str(statement)
        if "statement_timeout" in sql:
            self.timeouts.append(sql)
            return _Res([])
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
    state = lr.STATE_WATCHING_LIVE


def _relative_tape(n=40):
    now = lr._utcnow()
    px = [1.00, 1.40, 1.10, 1.41, 1.80, 1.05, 1.81, 1.90, 1.60, 1.91]
    px = (px * ((n // len(px)) + 1))[:n]
    rows = []
    for i, v in enumerate(px):
        ts = now - timedelta(seconds=(n - i))
        rows.append((ts, i + 1, float(v), 100.0, v - 0.01, v))
    return rows


def test_feed_accumulates_across_ticks_and_reports_catch_up(monkeypatch):
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
    assert db.savepoints == db.calls  # BAWAT pagbasa ay dumaan sa SAVEPOINT
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
    rows = _relative_tape(40)
    db = _TapeDB(rows)
    le: dict[str, Any] = {}
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 10, raising=False)
    st = lr._feed_tape_cycle_state(db, _Sess(), le)
    assert st["n_prints"] == 40
    assert st["feed"]["caught_up"] is True
    assert st["feed"]["reads"] <= 5  # 4 na puno + 1 na kulang
    assert st["feed"]["ms"] >= 0.0


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
        state = lr.STATE_WATCHING_LIVE

    assert lr._feed_tape_cycle_state(_TapeDB([]), _Crypto(), {}) is None

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db exploded")

    le: dict[str, Any] = {}
    st = lr._feed_tape_cycle_state(_Boom(), _Sess(), le)
    assert st is not None and st["feed"]["reason"] == "read_failed"
    assert st["n_prints"] == 0


def test_max_reads_zero_does_no_db_work_at_all():
    db = _TapeDB(_relative_tape(40))
    le: dict[str, Any] = {}
    st = lr._feed_tape_cycle_state(db, _Sess(), le, max_reads=0)
    assert db.calls == 0
    assert st["feed"]["reason"] == "no_reads_allowed"
    assert st["feed"]["caught_up"] is False


def test_the_statement_timeout_fence_is_set_and_reset_on_postgres():
    """Ang buhay na DB ay nag-uulat ng `statement_timeout = 0` at walang naka-set na role o
    database, kaya walang fence kung hindi ito ilalagay — at ang `SET LOCAL` ay dapat
    ibalik, kung hindi ay ang BUONG natitirang tick ang tatakbo sa 2 s na hangganan."""
    db = _TapeDB(_relative_tape(8), dialect="postgresql")
    sc = PullbackCycleScanner(0.5)
    out = feed_scanner_from_db(
        sc, "TSTX", db=db, session_start=lr._utcnow() - timedelta(hours=1), max_prints=100
    )
    assert out["fed"] == 8
    assert len(db.timeouts) == 2
    assert f"'{CYCLE_FEED_STATEMENT_TIMEOUT_MS}ms'" in db.timeouts[0]
    assert "DEFAULT" in db.timeouts[1]
    # Sa hindi-Postgres ay walang SET LOCAL (ang sqlite fake sa suite ay hindi ito kilala).
    db2 = _TapeDB(_relative_tape(8), dialect="sqlite")
    feed_scanner_from_db(
        PullbackCycleScanner(0.5), "TSTX", db=db2,
        session_start=lr._utcnow() - timedelta(hours=1), max_prints=100,
    )
    assert db2.timeouts == []


def test_the_catch_up_loop_stops_on_the_time_budget(monkeypatch):
    """INFRA na hangganan: kapag mabagal ang pagbasa, humihinto tayo sa PAGBASA, hindi sa
    pagdedesisyon — at ang `caught_up` ay nananatiling False kaya hindi kumakagat ang mult."""
    import app.services.trading.momentum_neural.tape_cycles as tc

    clock = {"t": 0.0}
    monkeypatch.setattr(tc.time, "monotonic", lambda: clock["t"])
    db = _TapeDB(_relative_tape(40))
    real_execute = db.execute

    def _slow(statement, params=None):
        clock["t"] += CYCLE_FEED_BUDGET_MS / 1000.0
        return real_execute(statement, params)

    db.execute = _slow
    sc = PullbackCycleScanner(0.5)
    out = feed_scanner_from_db(
        sc, "TSTX", db=db, session_start=lr._utcnow() - timedelta(hours=1), max_prints=2
    )
    assert out["reads"] == 1  # ang unang pagbasa ay kinain na ang buong budget
    assert out["budget_hit"] is True
    assert out["caught_up"] is False
    mult, rec = lr._cycle_exhaustion_conditioning(
        {"tape_cycle_state": dict(sc.to_dict(), feed=out)}, mid=1.0
    )
    assert mult == pytest.approx(1.0)
    assert rec["reason"] == "tape_not_caught_up"


# ── ANG TUNAY NA POSTGRES: ang predicate mismo, sa isang tunay na Session ────
def test_the_cursor_predicate_runs_on_real_postgres_and_is_range_bounded(db):
    """Ang dating cursor (`observed_at > :last_at OR (...)`) ay walang MABABANG hangganan sa
    `observed_at`, kaya hindi ito naipapasok ng Postgres sa ix_iqfeed_trades_sym_at at
    nagiging Filter: SINUKAT sa buhay na DB (WYHG 2026-09-08, cursor 14:00, LIMIT 5000)
    272.9 ms / 61,208 buffer laban sa 4.9 ms / 2,053 ng bagong anyo. Dito sinusuri na ang
    ipinadalang statement ay (a) tumatakbo sa tunay na Postgres kasama ang row-comparison,
    (b) eksakto ang hangganan sa cursor, at (c) may hawak na mababang hangganan sa saklaw."""
    base = datetime(2026, 9, 10, 13, 30, 0)
    for i in range(6):
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask) "
                "VALUES (:s, :t, :p, 100.0, :b, :a)"
            ),
            {"s": "TCYC", "t": base + timedelta(seconds=i), "p": 1.0 + i / 100.0,
             "b": 0.99 + i / 100.0, "a": 1.0 + i / 100.0},
        )
    db.flush()
    ids = [
        r[1]
        for r in db.execute(
            text("SELECT observed_at, id FROM iqfeed_trade_ticks WHERE symbol='TCYC' ORDER BY id")
        ).fetchall()
    ]
    sc = PullbackCycleScanner(0.5)
    out = feed_scanner_from_db(
        sc, "TCYC", db=db, session_start=base - timedelta(hours=1), max_prints=4,
        as_of=base + timedelta(hours=1),
    )
    assert out["fed"] == 6
    assert out["caught_up"] is True
    assert sc.n_prints == 6
    assert sc.last_id == ids[-1]
    # Ang cursor ay HINDI kailanman umuulit ng hilera: pareho ang stamp, ang `id` ang
    # bumabasag ng tabla (kaya kailangan ang row-comparison, hindi lang ang `>`).
    again = feed_scanner_from_db(
        sc, "TCYC", db=db, session_start=base - timedelta(hours=1), max_prints=4,
        as_of=base + timedelta(hours=1),
    )
    assert again["fed"] == 0
    assert sc.n_prints == 6
    # ...at may MABABANG hangganan sa saklaw (ang buong punto ng plano).
    from app.services.trading.momentum_neural.tape_cycles import _FEED_SQL

    assert "observed_at >= :last_at" in _FEED_SQL
    assert "(observed_at, id) > (:last_at, :last_id)" in _FEED_SQL


def test_two_prints_at_the_same_stamp_are_both_fed_exactly_once(db):
    """Ang tabla sa loob ng parehong microsecond ay tunay (ang IQFeed ay nagpapadala ng
    batch). Ang row-comparison ang humahawak nito."""
    ts = datetime(2026, 9, 10, 14, 0, 0)
    for p in (1.00, 1.01, 1.02):
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask) "
                "VALUES ('TCYD', :t, :p, 100.0, :b, :a)"
            ),
            {"t": ts, "p": p, "b": p - 0.01, "a": p},
        )
    db.flush()
    sc = PullbackCycleScanner(0.5)
    total = 0
    for _ in range(4):
        total += feed_scanner_from_db(
            sc, "TCYD", db=db, session_start=ts - timedelta(hours=1), max_prints=1,
            as_of=ts + timedelta(hours=1), max_reads=1,
        )["fed"]
    assert total == 3
    assert sc.n_prints == 3
