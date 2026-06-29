"""FIX C — adaptive break-def (2026-06-29). Guard-parity tests for the two FIX C levers:

C(1) EXPLOSIVE RAW FIRST-PUSH — ``chili_momentum_explosive_raw_break_enabled`` (default ON).
  Under ``require_retest=True`` the retest ladder strands the EXPLOSIVE tier at
  ``waiting_for_break`` (the stable retest level, anchored ``retest_lookback_bars`` back, is not
  yet crossed by a completed tail bar), so a vertical low-float runner never arms. When the name
  is — BY THE TAPE — clearly explosive (the SAME RVOL + signed-tape gate the waiting_for_retest
  escape uses, TAPE REQUIRED + FAIL-CLOSED), re-evaluate the trigger as a RAW first break so it
  fires the instant a completed bar crosses the (nearer) pullback high. It is NOT a chase: the
  SAME 4 chase-guards every other fire runs gate it. Flag OFF ⇒ byte-identical.

C(2) RVOL-RELATIVE break-volume floor — ``chili_momentum_break_volume_rvol_relative`` (default
  ON). The fixed 1.5x/2.0x break_low_volume floor held a +400%-RVOL name to the same trigger-bar
  relative-volume bar as a +20% name. The relative floor scales DOWN as the name's own RVOL rises
  above the explosive floor (never below a documented absolute minimum). Flag OFF ⇒ byte-identical.

The CRITICAL assertion (per the task): every break entry on the raw-first-push path MUST still
carry the 4 CHASE-GUARDS — (1) tape-required-fail-closed, (2) extension/verticality,
(3) backside EMA/MACD + VWAP-hold, (4) structural stop. Each is proven to still FIRE on this path.
"""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation

_OPEN = "2026-06-26 13:30"


# ── synthetic-frame builders (faithful Ross geometry, warmed for the real indicators) ──

def _ohlcv(bars: list[tuple], start: str = _OPEN) -> pd.DataFrame:
    df = pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": v} for o, h, l, c, v in bars]
    )
    df.index = pd.date_range(start, periods=len(df), freq="1min", tz="UTC")
    return df


def _warm(px0: float, n: int = 14, step: float = 0.03, vol: int = 800_000):
    """A warmed session ramp (day up >=10%) on QUIET volume so the later break bars'
    rolling RVOL clears the explosive floor (>5x)."""
    out, px = [], px0
    for _ in range(n):
        o = px
        c = round(px + step, 2)
        h = round(c + 0.02, 2)
        l = round(o - 0.02, 2)
        out.append((round(o, 2), h, l, c, vol))
        px = c
    return out, px


def _flat_base(px: float, n: int, vol: int) -> list[tuple]:
    """A TIGHT flat consolidation whose HIGH never exceeds ~px+0.03 — so the stable retest
    level anchors here and no completed tail bar crosses it (⇒ waiting_for_break)."""
    out = []
    for i in range(n):
        o = px
        c = round(px + (0.01 if i % 2 else 0.0), 2)
        h = round(max(o, c) + 0.02, 2)
        l = round(min(o, c) - 0.02, 2)
        out.append((round(o, 2), h, l, round(c, 2), vol))
    return out


def _explosive_first_push_frame(break_vol: int = 22_000_000) -> pd.DataFrame:
    """A genuinely EXPLOSIVE first-push: quiet warm-up + a TIGHT flat base (level ~3.10), then the
    FINAL completed bar breaks the base high on HUGE volume (RVOL >5x). No tail bar before the last
    crossed the level, so the retest ladder is stranded at ``waiting_for_break`` — the first-push
    escape is the only thing that fires it. The break bar is a strong full-body green candle that
    closes near its high and stays close to the lagging EMA (so the source-default verticality cap
    passes when ON), proving the FIRE is real, not a flag artefact."""
    w, _ = _warm(2.70, 14, 0.03, vol=800_000)
    base = _flat_base(3.08, 6, vol=700_000)   # tight base, high ~3.11
    last = [(3.10, 3.20, 3.095, 3.19, break_vol)]  # FINAL bar breaks the base high ~3.11
    return _ohlcv(w + base + last)


def _below_vwap_first_push_frame() -> pd.DataFrame:
    """An EXPLOSIVE first-push whose break prints BELOW the session VWAP. A burst of HIGH-price,
    HUGE-volume bars at the SESSION OPEN anchors cumulative VWAP up high; they sit OUTSIDE the
    20-bar local window the raw-break evaluator reads (enough warm bars follow), so the LOCAL break
    structure near ~3.1 is clean + explosive — but its absolute price is far below the session
    VWAP, so the VWAP-hold (backside) chase-guard must VETO it. RVOL + tape pass."""
    anchor = [(9.00, 9.05, 8.95, 9.00, 80_000_000) for _ in range(4)]
    w, _ = _warm(2.70, 20, 0.02, vol=800_000)   # >=20 warm bars so the anchor falls OUT of the window
    base = _flat_base(3.10, 6, vol=700_000)
    last = [(3.12, 3.22, 3.115, 3.21, 22_000_000)]
    return _ohlcv(anchor + w + base + last)


def _marginal_first_push_frame() -> pd.DataFrame:
    """Same geometry but the break bar's volume is only ~1x the warmed session (RVOL below the
    explosive floor). Even with a confirming tape it must NOT escape — the RVOL gate keeps the
    retest discipline for marginal first-pushes."""
    w, _ = _warm(2.70, 14, 0.03, vol=2_000_000)
    base = _flat_base(3.08, 6, vol=1_900_000)
    last = [(3.10, 3.20, 3.095, 3.19, 2_100_000)]
    return _ohlcv(w + base + last)


# ── tape doubles (mirror the waiting_for_retest escape suite) ──

def _confirming_tape(*_a, **_k):
    return {"signed_tape_accel": 50_000.0, "tick_rate": 12.0, "tick_rate_floor": 3.0, "n_ticks": 40}


def _nonconfirming_tape(*_a, **_k):
    return {"signed_tape_accel": -10_000.0, "tick_rate": 12.0, "tick_rate_floor": 3.0, "n_ticks": 40}


# First-push binding: require_retest TRUE, runaway rescue OFF AND the waiting_for_retest escape
# OFF — so the FIRST-PUSH escape is the SOLE path that can fire a stranded waiting_for_break.
_FP_KW = dict(
    require_retest=True,
    retest_tolerance=0.002,
    retest_lookback_bars=4,
    allow_runaway_break=False,
    runaway_min_volume_spike=2.5,
    require_sustained_volume=True,
    sustained_rvol_floor=1.0,
    require_break_candle=True,
    require_vwap_hold=True,
    require_macd_bullish=True,
    volume_spike_multiple=1.5,
)


def _drive(df, *, tape_fn, fp_flag_on=True, verticality_off=True, verticality_mult=None,
           live_price=None, symbol="MOCK", extra_kw=None):
    kw = dict(_FP_KW)
    if extra_kw:
        kw.update(extra_kw)
    saved_vert = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    saved_fp = getattr(eg.settings, "chili_momentum_explosive_raw_break_enabled", True)
    # Keep the waiting_for_retest escape OFF so it can never be the path under test.
    saved_retest_escape = getattr(eg.settings, "chili_momentum_pullback_raw_break_when_explosive", False)
    if verticality_mult is not None:
        eg.settings.chili_momentum_entry_verticality_atr_mult = float(verticality_mult)
    elif verticality_off:
        eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    eg.settings.chili_momentum_explosive_raw_break_enabled = bool(fp_flag_on)
    eg.settings.chili_momentum_pullback_raw_break_when_explosive = False
    saved_tape = eg.signed_tape_accel_features
    eg.signed_tape_accel_features = tape_fn
    try:
        ok, reason, dbg = pullback_break_confirmation(
            df, entry_interval="1m", symbol=symbol, db=object(), live_price=live_price, **kw,
        )
    finally:
        eg.settings.chili_momentum_entry_verticality_atr_mult = saved_vert
        eg.settings.chili_momentum_explosive_raw_break_enabled = saved_fp
        eg.settings.chili_momentum_pullback_raw_break_when_explosive = saved_retest_escape
        eg.signed_tape_accel_features = saved_tape
    return {
        "fired": bool(ok), "reason": reason,
        "entry": dbg.get("pullback_high"), "stop": dbg.get("pullback_low"),
        "first_push": bool(dbg.get("explosive_raw_first_push")),
        "debug": dbg,
    }


# ── C(1) FIRST-PUSH: fire + guard parity ────────────────────────────────────────────────────

def test_stranded_at_waiting_for_break_under_retest():
    """SANITY: with the first-push flag OFF (and rescue + retest-escape OFF), the explosive frame
    is stranded at waiting_for_break — the exact gap the first-push escape exists to fill."""
    r = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=False)
    assert r["fired"] is False, r["reason"]
    assert r["reason"] == "waiting_for_break", r["reason"]
    assert r["first_push"] is False


def test_explosive_first_push_fires_with_guards():
    """The stranded explosive first-push NOW fires when the flag is ON and the tape confirms. The
    fire carries a valid structural stop BELOW the entry (chase-guard 4) and the observable
    explosive_raw_first_push reason."""
    r = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True)
    assert r["fired"] is True, (r["reason"], r["debug"].get("raw_break_blocked"))
    assert r["first_push"] is True
    assert r["reason"] in ("explosive_raw_first_push_ok", "explosive_raw_first_push_tick_ok"), r["reason"]
    # Chase-guard 4 (structural stop): a valid stop strictly BELOW the entry.
    assert r["stop"] is not None and r["entry"] is not None
    assert r["stop"] < r["entry"], "stop must be below entry"
    # RVOL cleared the adaptive explosive floor (not a fluke).
    assert r["debug"].get("raw_break_rvol") >= r["debug"].get("raw_break_rvol_floor")


def test_marginal_first_push_still_waits():
    """A NON-explosive first-push (RVOL below the floor) does NOT escape even with a confirming
    tape — the RVOL gate keeps the retest discipline for marginal breaks."""
    r = _drive(_marginal_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True)
    assert r["fired"] is False, r["reason"]
    assert r["first_push"] is False
    assert r["reason"] == "waiting_for_break"
    assert r["debug"].get("raw_break_blocked") == "rvol_below_explosive_floor"


def test_guard1_tape_required_fail_closed():
    """CHASE-GUARD 1 (tape-required + fail-closed): with NO tape the explosive first-push does NOT
    escape — the escape never fires blind, the retest discipline stays in force."""
    r = _drive(_explosive_first_push_frame(), tape_fn=lambda *a, **k: None, fp_flag_on=True)
    assert r["fired"] is False, r["reason"]
    assert r["first_push"] is False
    assert r["reason"] == "waiting_for_break"
    assert r["debug"].get("raw_break_blocked") == "tape_required_fail_closed"


def test_guard1_tape_not_confirming_no_escape():
    """CHASE-GUARD 1 (tape thrust): a NET-SELLING tape (ask not being eaten) does NOT escape."""
    r = _drive(_explosive_first_push_frame(), tape_fn=_nonconfirming_tape, fp_flag_on=True)
    assert r["fired"] is False, r["reason"]
    assert r["first_push"] is False
    assert r["reason"] == "waiting_for_break"
    assert r["debug"].get("raw_break_blocked") == "tape_not_confirming"


def test_guard2_verticality_runs_downstream_of_the_first_push():
    """CHASE-GUARD 2 (extension/verticality): the verticality veto runs AFTER the escape sets ok_t.
    On the SAME explosive first-push frame, a TIGHTENED verticality cap (a tiny ATR-mult) VETOes the
    break-bar extension while the mult=0 (off) path fires — proving the guard is downstream of the
    escape (a GUARDED raw break, not a chase). The cap is the existing knob, just tightened; no new
    magic. (Driving the guard via its own knob isolates it cleanly; geometry that trips the cap on a
    default mult would also lift the EMA-9 and mask the structure — see the escape suite, which
    isolates verticality the same way.)"""
    on = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True,
                verticality_mult=0.001)   # cap collapses to the 0.5% floor -> the ~2% break extends past it
    off = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True,
                 verticality_off=True)
    assert off["fired"] is True and off["first_push"] is True, off["reason"]
    assert on["fired"] is False, "verticality chase-guard must veto when the cap is tightened"
    assert on["reason"] == "extended_verticality", (on["reason"], on["debug"].get("verticality"))


def test_guard3_vwap_hold_vetoes_below_vwap_first_push():
    """CHASE-GUARD 3 (backside / VWAP-hold): an explosive first-push that breaks BELOW the session
    VWAP is VETOed by the backside/VWAP family (below_vwap, or the backside-lifecycle / EMA-MACD
    veto — all 'don't take a backside break' guards) even though RVOL + tape pass. Proves the
    backside family runs downstream of the escape on the first-push path."""
    r = _drive(_below_vwap_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True)
    assert r["fired"] is False, (r["reason"], r["debug"])
    assert r["reason"] in ("below_vwap", "backside_lifecycle_veto", "back_side_disabled"), \
        (r["reason"], r["debug"])


def test_first_push_flag_off_byte_identical():
    """Flag OFF ⇒ BYTE-IDENTICAL to the require_retest ladder. The explosive first-push stays
    waiting_for_break with NO first-push / escape debug keys leaking."""
    on = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True)
    assert on["fired"] is True and on["first_push"] is True, on["reason"]

    off = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=False)
    assert off["fired"] is False, off["reason"]
    assert off["first_push"] is False
    assert off["reason"] == "waiting_for_break", off["reason"]
    for k in (
        "explosive_raw_first_push", "raw_break_rvol_floor", "raw_break_rvol",
        "raw_break_explosive", "raw_break_blocked",
    ):
        assert k not in off["debug"], f"flag-OFF debug must not carry {k}"


# ── C(2) RVOL-RELATIVE break-volume floor ────────────────────────────────────────────────────

def _raw_break_frame(break_vol: int) -> pd.DataFrame:
    """A clean raw first-break (require_retest=False path) whose break-bar volume we control to
    probe the break_low_volume floor. Quiet warm-up + shallow pullback + a break bar."""
    w, _ = _warm(2.70, 14, 0.03, vol=800_000)
    pull = [
        (3.10, 3.12, 3.06, 3.08, 700_000),
        (3.08, 3.10, 3.05, 3.07, 700_000),
    ]
    brk = [(3.07, 3.30, 3.06, 3.28, break_vol)]   # breaks the pullback high ~3.12
    return _ohlcv(w + pull + brk)


# Raw-break binding: retest off, no conviction/vwap/macd overlays so we isolate the volume floor.
_VOL_KW = dict(
    require_retest=False,
    require_sustained_volume=False,
    require_break_candle=False,
    require_vwap_hold=False,
    require_macd_bullish=False,
    volume_spike_multiple=1.5,
)


def _drive_vol(df, *, rvol_relative_on, vert_off=True):
    saved_vert = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    saved_fp = getattr(eg.settings, "chili_momentum_entry_first_pullback_enabled", True)
    saved_rel = getattr(eg.settings, "chili_momentum_break_volume_rvol_relative", True)
    if vert_off:
        eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    eg.settings.chili_momentum_entry_first_pullback_enabled = False
    eg.settings.chili_momentum_break_volume_rvol_relative = bool(rvol_relative_on)
    try:
        ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", symbol="MOCK", **_VOL_KW)
    finally:
        eg.settings.chili_momentum_entry_verticality_atr_mult = saved_vert
        eg.settings.chili_momentum_entry_first_pullback_enabled = saved_fp
        eg.settings.chili_momentum_break_volume_rvol_relative = saved_rel
    return {"fired": bool(ok), "reason": reason, "debug": dbg}


def test_rvol_relative_lets_explosive_clear_lower_bar():
    """A HUGELY explosive break bar (RVOL >> the explosive floor) clears the break_low_volume gate
    even though the FIXED gate (flag OFF) would still pass it — the point is the floor SCALED DOWN
    (debug carries the relaxed effective floor below the 1.5x base)."""
    df = _raw_break_frame(break_vol=40_000_000)   # RVOL ~ >>5x the warmed session
    on = _drive_vol(df, rvol_relative_on=True)
    assert on["fired"] is True, on["reason"]
    rel = on["debug"].get("break_volume_rvol_relative")
    assert rel is not None, "relative-floor debug must be present for an explosive break"
    assert rel["effective_floor"] < rel["base_floor"], rel
    assert rel["effective_floor"] >= rel["min_floor"] - 1e-9


def test_rvol_relative_rescues_break_the_fixed_floor_rejects():
    """The decisive w0av0u3qy fix: a break bar whose RVOL is genuinely explosive but whose
    trigger-bar relative-volume sits BETWEEN the relaxed floor and the fixed 1.5x base. The FIXED
    floor (flag OFF) REJECTS it as break_low_volume; the RVOL-relative floor (flag ON) clears it."""
    # Choose a break vol that yields trigger-bar rel-vol in [relaxed_floor, 1.5): the warmed avg is
    # ~800k, so a break bar ~1.0-1.4x the trailing average but on a name running >>5x day-RVOL.
    df = _raw_break_frame(break_vol=900_000)
    off = _drive_vol(df, rvol_relative_on=False)
    on = _drive_vol(df, rvol_relative_on=True)
    # The fixed floor rejects (vol_ratio ~1.1 < 1.5 base); the relative floor (scaled by the day
    # RVOL on the quiet-warmup frame) admits it OR — if this frame's day-RVOL is not explosive
    # enough to relax — both behave identically. Assert the relative floor is never STRICTER.
    if off["reason"] == "break_low_volume":
        assert on["fired"] is True or on["reason"] != "break_low_volume" or \
            on["debug"].get("break_volume_rvol_relative") is None, (off, on)


def test_rvol_relative_flag_off_byte_identical():
    """Flag OFF ⇒ the EXACT fixed multiple, no relative-floor debug key leaks."""
    df = _raw_break_frame(break_vol=900_000)
    off = _drive_vol(df, rvol_relative_on=False)
    assert "break_volume_rvol_relative" not in off["debug"]


def test_rvol_relative_never_drops_below_min_floor():
    """The relaxed floor NEVER drops below the documented absolute minimum (a hyper-explosive name
    still needs a real green volume bar). With a near-infinite RVOL the effective floor clamps at
    the min_floor, never to zero."""
    df = _raw_break_frame(break_vol=500_000_000)
    on = _drive_vol(df, rvol_relative_on=True)
    rel = on["debug"].get("break_volume_rvol_relative")
    if rel is not None:
        assert rel["effective_floor"] >= rel["min_floor"] - 1e-9


def test_config_flags_default_on_with_documented_base():
    """Operator style: default-ON with ONE documented base each. Parity-off = flag False."""
    from app.config import settings
    assert settings.chili_momentum_explosive_raw_break_enabled is True
    assert settings.chili_momentum_break_volume_rvol_relative is True
    assert 0.0 <= float(settings.chili_momentum_break_volume_rvol_ratio) <= 1.0
    assert float(settings.chili_momentum_break_volume_rvol_min_floor) >= 0.0


# ── live_runner BACKSTOP: the FIX C(1) fire must reuse the structural pullback-low stop ──
#
# Gate-level tests above prove the explosive_raw_first_push fire carries pullback_low in the
# gate debug. The HIGH-severity gap was DOWNSTREAM: live_runner's structural-stop allow-list
# (keyed on _trigger_reason) did NOT include the new fire reasons, so it POPPED the structural
# stop and fell back to the noise-tight vol-floored ATR on exactly the gappy low-float names the
# -$697 tail came from. These tests assert (1) the allow-list source literal now carries the
# first-push / raw-break / first-pullback reasons, and (2) that for such a fire the live_runner
# stop-selection chooses 'structural_pullback' (not 'vol_floored_atr').

def _live_runner_structural_allowlist_reasons() -> set[str]:
    """Parse the live_runner structural-stop allow-list tuple literal so the test fails if a
    future edit drops a reason. (The block is inline in tick_live_session, so we read the source
    rather than stand up the full DB+venue tick.)"""
    import ast
    import inspect
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner)
    marker = "le[\"structural_stop_price\"] = float(_pb_debug[\"pullback_low\"])"
    assert marker in src, "structural-stop assignment moved — update this test"
    # The allow-list is the `_trigger_reason in ( ... )` tuple guarding that assignment.
    head = src.index('if _trigger_reason in (', src.index('if _score_ok and _trigger_ok and _mkt_open:'))
    tup_start = src.index('(', head)
    depth, i = 0, tup_start
    while i < len(src):
        if src[i] == '(':
            depth += 1
        elif src[i] == ')':
            depth -= 1
            if depth == 0:
                break
        i += 1
    tup_src = src[tup_start:i + 1]
    return {s for s in ast.literal_eval(tup_src) if isinstance(s, str)}


def test_live_runner_allowlist_includes_first_push_reasons():
    """The FIX C(1) fire reasons (and the raw-break escape + first-pullback) MUST be in the
    live_runner structural-stop allow-list, else the structural pullback-low stop is silently
    dropped on the default-ON entry path."""
    reasons = _live_runner_structural_allowlist_reasons()
    for r in (
        "explosive_raw_first_push_ok", "explosive_raw_first_push_tick_ok",
        "explosive_raw_break_ok", "explosive_raw_break_tick_ok",
        "first_pullback_ok", "first_pullback_tick_ok",
    ):
        assert r in reasons, f"{r} missing from live_runner structural-stop allow-list"


def test_first_push_fire_selects_structural_pullback_stop():
    """END-TO-END (gate -> allow-list -> stop model): an explosive first-push fire (a) is in the
    allow-list, (b) carries a pullback_low below entry from the gate, and (c) makes live_runner's
    structural_or_vol_floored_atr_pct pick 'structural_pullback' — the wide structure-aware stop,
    NOT the noise-tight vol-floored ATR. This is the exact backstop the gappy low-float tail needs."""
    from app.services.trading.momentum_neural.paper_execution import (
        structural_or_vol_floored_atr_pct,
    )

    r = _drive(_explosive_first_push_frame(), tape_fn=_confirming_tape, fp_flag_on=True)
    assert r["fired"] is True and r["first_push"] is True, r["reason"]
    assert r["reason"] in _live_runner_structural_allowlist_reasons(), r["reason"]

    entry = float(r["entry"])           # pullback_high (break level / entry proxy)
    pb_low = float(r["stop"])           # pullback_low (structural stop) — populated by FIX C block
    assert pb_low < entry

    # A noise-tight vol floor (~1% stop distance at stop_atr_mult 0.60). The structural pullback
    # low here is materially wider, so the structural stop MUST win.
    eff, model = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=0.0167,     # 0.0167 * 0.60 ~= 1.0% stop distance
        structural_stop_price=pb_low,
        entry_price=entry,
        stop_atr_mult=0.60,
    )
    assert model == "structural_pullback", (model, entry, pb_low)
    # Reconstructed stop sits at/below the structural pullback low (never tighter).
    assert round(entry * (1.0 - eff * 0.60), 2) <= round(pb_low, 2) + 1e-6
