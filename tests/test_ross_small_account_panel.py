"""Ross SMALL-ACCOUNT-CHALLENGE mock panel — drive CHILI's REAL live entry-decision code
with FAITHFUL Ross geometry and report whether CHILI AGREES.

This is NOT the firing-mock style of ``test_momentum_mock_fire_*.py`` (which patches the four
chase-guards to PASS so a clean baseline reaches the fire path). Here the guards run FOR REAL:
each setup is built from Ross Cameron's DOCUMENTED small-account-challenge geometry, then driven
through the EXACT functions ``live_runner.py`` calls —

  * ``momentum_pullback_trigger`` (entry_gates.py ~:8598) — the wrapper the live runner invokes;
    it resolves every ``chili_momentum_*`` knob and calls ``pullback_break_confirmation``. The
    panel calls ``pullback_break_confirmation`` directly with the LIVE-shaped require_* knobs so
    the geometry is judged by the identical confirmation ladder, with ``db=None`` so the L2 /
    signed-tape confirmers FAIL OPEN (no DB) and every CHART guard runs on the synthetic frame.
  * ``halt_resume_dip_trigger`` (entry_gates.py ~:8647) — the halt-resume case.

The FOUR shared chase-guards (memory: project_momentum_chase_guard_parity) all run REAL off the
constructed frame: (1) TAPE required+fail-closed — fails OPEN here because ``db=None`` (no tape
feed), exactly as the live lane fails open on a missing feed; (2) EXTENSION veto —
``chili_momentum_entry_verticality_atr_mult`` (close-vs-EMA9, ATR-scaled); (3) BACKSIDE + VWAP —
``_detect_back_side`` (EMA/MACD rollover) + ``front_side_state`` (session below-VWAP / faded /
rolled-over) + ``require_vwap_hold``; (4) STRUCTURAL STOP — the trigger returns ``pullback_low``
as the stop, fed to ``risk_policy.compute_risk_first_quantity``.

KEY CONSTRUCTION FACT (learned by probing the real code, not by tuning to pass): CHILI's
indicators warm from the frame (ATR window 14, VWAP/rel-vol window 20). A frame must carry a
realistic ~20-bar warmed session lead-in or ATR comes back ``None`` and the verticality cap
COLLAPSES to its 0.5% floor — rejecting every break as ``extended_verticality``. So each setup
includes a warmed session ramp + (where the geometry is a steady climb) a brief consolidation so
the 9-EMA tracks price. That is FAITHFUL — a real low-float runner DOES consolidate before each
leg — not reverse-engineering.

The verticality knob ships default 1.5 but is OFF live (memory: "verticality gate OFF live = the
data-profitable setting, +$1,081/3d"). The panel reports BOTH: the source-DEFAULT verdict and
the LIVE-binding verdict (verticality=0). The live-binding column is the one that reflects what
the running lane actually decides.

TESTS-ONLY — never edits source. Run one file at a time vs chili_test (truncate collisions):
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \\
    conda run -n chili-env python -m pytest tests/test_ross_small_account_panel.py -q -s
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.entry_gates import (
    compute_all_from_df,
    halt_resume_dip_trigger,
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity

# A single RTH session start (UTC). Only the per-day VWAP anchoring / DatetimeIndex matter;
# the exact wall-clock is irrelevant to the chart triggers.
_OPEN = "2026-06-26 13:30"

# Risk-first sizing inputs = the live ADAPTIVE binding (memory: report_binding_not_defaults):
# equity ~$13.5k -> 1% risk = $135 max-loss, 15% = $2029 notional ceiling. NOT fixed magic $.
_MAX_LOSS_USD = 135.0
_MAX_NOTIONAL_USD = 2029.0


# ── synthetic-frame builders (faithful Ross geometry, warmed for the real indicators) ──

def _ohlcv(bars: list[tuple], start: str = _OPEN) -> pd.DataFrame:
    df = pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": v} for o, h, l, c, v in bars]
    )
    df.index = pd.date_range(start, periods=len(df), freq="1min", tz="UTC")
    return df


def _warm(px0: float, n: int = 12, step: float = 0.03, vol: int = 2_000_000):
    """A warmed session ramp so ATR/VWAP/EMA/rel-vol are non-None at the trigger bar AND the
    day is up >=10% (Ross's explosive floor). Returns (bars, last_close)."""
    out, px = [], px0
    for _ in range(n):
        o = px
        c = round(px + step, 2)
        h = round(c + 0.02, 2)
        l = round(o - 0.02, 2)
        out.append((round(o, 2), h, l, c, vol))
        px = c
    return out, px


def _cons(px: float, n: int = 8, vol: int = 1_400_000) -> list[tuple]:
    """A tight consolidation band around ``px`` so the 9-EMA converges to price before the
    next leg — a real runner DOES base between legs; this is not anti-verticality tuning, it is
    the flag/base Ross requires before the break."""
    out = []
    for i in range(n):
        o = px
        c = round(px + (0.01 if i % 2 else 0.0), 2)
        h = round(max(o, c) + 0.03, 2)
        l = round(min(o, c) - 0.03, 2)
        out.append((round(o, 2), h, l, round(c, 2), vol))
    return out


# The LIVE entry-confirmation knob shape (require_* the live runner passes through
# momentum_pullback_trigger): retest off (raw first break), sustained-volume on, conviction
# break candle on, VWAP-hold on, MACD-bullish on, base volume spike 1.5x.
_LIVE_KW = dict(
    require_retest=False,
    require_sustained_volume=True,
    sustained_rvol_floor=1.0,
    require_break_candle=True,
    require_vwap_hold=True,
    require_macd_bullish=True,
    volume_spike_multiple=1.5,
)


def _atr_pct(df: pd.DataFrame, dbg: dict) -> float:
    ap = dbg.get("atr_pct")
    if ap is not None:
        return float(ap)
    arr = compute_all_from_df(df, needed={"atr"})
    a = (arr.get("atr") or [None])[-1]
    c = float(df["Close"].iloc[-1])
    return (a / c) if (a and c) else 0.04


def _drive_pullback(df: pd.DataFrame, *, symbol: str = "MOCK", live_price=None) -> dict:
    """Drive the REAL unified pullback trigger + the real chase-guards + risk-first sizing.
    Returns a row dict: fired / reason / entry / stop / qty / notional."""
    ok, reason, dbg = pullback_break_confirmation(
        df, entry_interval="1m", symbol=symbol, db=None, live_price=live_price, **_LIVE_KW,
    )
    row = {
        "fired": bool(ok),
        "reason": reason,
        "entry": dbg.get("pullback_high"),
        "stop": dbg.get("pullback_low"),
        "qty": None,
        "notional": None,
        "size_capped_by": None,
    }
    if ok and row["entry"] and row["stop"]:
        qty, meta = compute_risk_first_quantity(
            entry_price=float(row["entry"]),
            atr_pct=_atr_pct(df, dbg),
            max_loss_usd=_MAX_LOSS_USD,
            max_notional_ceiling_usd=_MAX_NOTIONAL_USD,
            stop_atr_mult=0.60,
        )
        row["qty"] = round(qty, 1)
        row["notional"] = meta.get("notional_usd")
        row["size_capped_by"] = meta.get("capped_by")
    return row


# ── the 12 canonical Ross small-account-challenge setups (faithful geometry) ──

def _gap_and_go() -> pd.DataFrame:
    w, px = _warm(2.50, 10, 0.05)
    return _ohlcv(w + _cons(px) + [
        (px, round(px + 0.18, 2), round(px - 0.01, 2), round(px + 0.16, 2), 6_000_000),
        (round(px + 0.16, 2), round(px + 0.20, 2), round(px + 0.10, 2), round(px + 0.13, 2), 2_000_000),
        (round(px + 0.13, 2), round(px + 0.17, 2), round(px + 0.09, 2), round(px + 0.14, 2), 1_800_000),
        (round(px + 0.14, 2), round(px + 0.20, 2), round(px + 0.12, 2), round(px + 0.18, 2), 1_900_000),
        (round(px + 0.18, 2), round(px + 0.26, 2), round(px + 0.17, 2), round(px + 0.23, 2), 12_000_000),
    ])


def _first_pullback() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.24, 3.09, 3.22, 5_000_000), (3.22, 3.30, 3.21, 3.28, 6_000_000),
        (3.28, 3.30, 3.23, 3.25, 1_800_000), (3.25, 3.28, 3.22, 3.26, 1_500_000),
        (3.26, 3.30, 3.24, 3.29, 1_600_000), (3.29, 3.34, 3.28, 3.32, 11_000_000),
    ])


def _micro_pullback() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.20, 3.09, 3.18, 5_000_000), (3.18, 3.26, 3.17, 3.24, 6_000_000),
        (3.24, 3.27, 3.21, 3.22, 1_500_000),               # one shallow red
        (3.22, 3.28, 3.20, 3.27, 9_000_000),               # break prior bar high
    ])


def _bull_flag() -> pd.DataFrame:
    w, _ = _warm(2.60, 12, 0.03)
    return _ohlcv(w + [
        (2.96, 3.20, 2.95, 3.18, 7_000_000), (3.18, 3.40, 3.17, 3.38, 8_000_000),   # pole
        (3.38, 3.40, 3.30, 3.33, 1_800_000), (3.33, 3.36, 3.28, 3.31, 1_600_000),   # flag
        (3.31, 3.35, 3.29, 3.34, 1_500_000), (3.34, 3.37, 3.30, 3.35, 1_500_000),   # flag top ~3.37
        (3.35, 3.46, 3.34, 3.44, 12_000_000),                                       # break flag high
    ])


def _abcd() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.28, 6_000_000),   # A surge
        (3.28, 3.30, 3.18, 3.20, 2_000_000),   # B pull low
        (3.20, 3.29, 3.19, 3.27, 3_000_000),   # BC high
        (3.27, 3.29, 3.22, 3.24, 1_800_000),   # C higher-low (holds above B)
        (3.24, 3.30, 3.23, 3.29, 1_700_000),   # toward D
        (3.29, 3.36, 3.28, 3.34, 11_000_000),  # break B/BC high
    ])


def _flat_top() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.27, 6_000_000),   # drive to flat top ~3.30
        (3.27, 3.30, 3.24, 3.26, 1_800_000),   # test 1
        (3.26, 3.30, 3.24, 3.27, 1_700_000),   # test 2
        (3.27, 3.30, 3.25, 3.28, 1_600_000),   # test 3
        (3.28, 3.36, 3.27, 3.34, 12_000_000),  # break flat top
    ])


def _vwap_reclaim() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + [
        (3.06, 3.30, 3.05, 3.28, 7_000_000), (3.28, 3.40, 3.27, 3.38, 8_000_000),   # run
        (3.38, 3.39, 3.10, 3.12, 3_000_000), (3.12, 3.16, 3.02, 3.05, 2_500_000),   # dip below VWAP
        (3.05, 3.20, 3.04, 3.18, 3_000_000), (3.18, 3.30, 3.16, 3.28, 9_000_000),   # reclaim + hold + break
    ])


def _new_high_of_day() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.28, 3.09, 3.26, 6_000_000), (3.26, 3.30, 3.25, 3.29, 2_000_000),   # prior HOD 3.30
        (3.29, 3.30, 3.25, 3.27, 1_800_000), (3.27, 3.30, 3.26, 3.29, 1_700_000),   # hold under HOD
        (3.29, 3.38, 3.28, 3.36, 12_000_000),                                       # NEW HOD break
    ])


def _dip_and_rip() -> pd.DataFrame:
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + [
        (3.06, 3.20, 3.05, 3.18, 6_000_000), (3.18, 3.28, 3.17, 3.26, 7_000_000),   # morning rip
        (3.26, 3.27, 3.05, 3.07, 3_000_000), (3.07, 3.10, 3.00, 3.03, 2_500_000),   # dip
        (3.03, 3.18, 3.02, 3.16, 3_500_000), (3.16, 3.30, 3.14, 3.28, 9_000_000),   # rip back + break
    ])


def _extended_chase() -> pd.DataFrame:
    """Anti-pattern (the DCFC FOMO chase): a near-vertical run with no pullback structure.
    CHILI MUST decline (chase guard)."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + [
        (3.06, 3.30, 3.05, 3.28, 6_000_000), (3.28, 3.55, 3.27, 3.53, 7_000_000),
        (3.53, 3.85, 3.52, 3.83, 8_000_000), (3.83, 4.40, 3.82, 4.35, 9_000_000),
    ])


def _choppy() -> pd.DataFrame:
    """Anti-pattern (the LGVN chop): directionless tape, no shallow-flag-into-new-high.
    CHILI MUST decline."""
    w, _ = _warm(2.70, 12, 0.0)
    return _ohlcv(w + [
        (2.70, 2.85, 2.60, 2.66, 2_000_000), (2.66, 2.84, 2.58, 2.78, 2_000_000),
        (2.78, 2.83, 2.62, 2.65, 2_000_000), (2.65, 2.86, 2.60, 2.80, 2_000_000),
        (2.80, 2.82, 2.63, 2.67, 2_000_000), (2.67, 2.85, 2.61, 2.72, 2_000_000),
    ])


# halt-resume: a warmed ramp, a post-resume pop to a reference high, a first dip, a reclaim bar.
def _halt_resume():
    w, _ = _warm(2.70, 12, 0.03)
    bars = w + [
        (3.06, 3.45, 3.05, 3.42, 12_000_000),   # resume pop (ref high ~3.45)
        (3.42, 3.44, 3.30, 3.33, 4_000_000),    # first dip
        (3.33, 3.36, 3.28, 3.31, 3_500_000),    # dip low ~3.28
        (3.31, 3.42, 3.30, 3.40, 7_000_000),    # reclaim/curl, closes over prior high
    ]
    df = _ohlcv(bars)
    return df, df.index[len(w)], df.index[-1]   # (frame, resumed_at, now)


# Setups driven through the unified pullback trigger (gap-and-go/first-pullback/micro/flag/
# abcd/flat-top/vwap-reclaim/new-high/dip-rip + the two anti-patterns).
_PULLBACK_SETUPS = [
    ("01 gap_and_go", _gap_and_go, True),
    ("02 first_pullback", _first_pullback, True),
    ("03 micro_pullback", _micro_pullback, True),
    ("04 bull_flag", _bull_flag, True),
    ("05 abcd", _abcd, True),
    ("06 flat_top", _flat_top, True),
    ("07 vwap_reclaim", _vwap_reclaim, True),
    ("09 new_high_of_day", _new_high_of_day, True),
    ("10 dip_and_rip", _dip_and_rip, True),
    ("11 extended_chase[anti]", _extended_chase, False),
    ("12 choppy[anti]", _choppy, False),
]


def _run_panel(verticality_off: bool) -> list[tuple[str, dict]]:
    rows = []
    saved = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    if verticality_off:
        eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    try:
        for name, build, _ in _PULLBACK_SETUPS:
            rows.append((name, _drive_pullback(build())))
        df, resumed, now = _halt_resume()
        ok, reason, dbg = halt_resume_dip_trigger(
            df, entry_interval="1m", halt_resumed_at_utc=resumed, now=now,
        )
        hr_row = {
            "fired": bool(ok), "reason": reason,
            "entry": dbg.get("pullback_high"), "stop": dbg.get("pullback_low"),
            "qty": None, "notional": None, "size_capped_by": None,
        }
        if ok and hr_row["entry"] and hr_row["stop"]:
            qty, meta = compute_risk_first_quantity(
                entry_price=float(hr_row["entry"]),
                atr_pct=float(dbg.get("atr_pct") or 0.04),
                max_loss_usd=_MAX_LOSS_USD, max_notional_ceiling_usd=_MAX_NOTIONAL_USD,
                stop_atr_mult=0.60,
            )
            hr_row["qty"] = round(qty, 1)
            hr_row["notional"] = meta.get("notional_usd")
        rows.append(("08 halt_resume_dip", hr_row))
    finally:
        eg.settings.chili_momentum_entry_verticality_atr_mult = saved
    return rows


def _print_table(title: str, rows: list[tuple[str, dict]]) -> None:
    print(f"\n{title}")
    print(f"{'setup':26s} {'fired':6s} {'reason':34s} {'entry':>7s} {'stop':>7s} {'qty':>8s} {'notional':>9s}")
    print("-" * 100)
    for name, r in rows:
        ent = "" if r["entry"] is None else f"{r['entry']:.2f}"
        stp = "" if r["stop"] is None else f"{r['stop']:.2f}"
        qty = "" if r["qty"] is None else f"{r['qty']:.1f}"
        notl = "" if r["notional"] is None else f"{r['notional']:.0f}"
        print(f"{name:26s} {('FIRE' if r['fired'] else 'skip'):6s} {r['reason']:34s} {ent:>7s} {stp:>7s} {qty:>8s} {notl:>9s}")


# ── the panel tests ──

class TestRossSmallAccountPanel:
    def test_panel_default_settings(self):
        """Drive all 12 faithful Ross setups through the REAL trigger + REAL chase-guards +
        risk-first sizing under the SOURCE-DEFAULT settings, and print the per-setup table.

        Hard assertions (CHILI's load-bearing discipline — these must hold regardless of the
        verticality knob): the two anti-patterns (extended chase / choppy) MUST NOT fire, and
        every setup that fires must produce a valid structural stop BELOW the entry with a
        positive risk-first quantity (Ross's exact stop rule + risk-first sizing)."""
        rows = _run_panel(verticality_off=False)
        _print_table("ROSS SMALL-ACCOUNT PANEL — source-DEFAULT settings (verticality_mult=1.5)", rows)
        by = dict(rows)
        # Anti-patterns are structurally refused (the chase guard works).
        assert by["11 extended_chase[anti]"]["fired"] is False
        assert by["12 choppy[anti]"]["fired"] is False
        # The two cleanest Ross entries fire even at the strict default.
        assert by["01 gap_and_go"]["fired"] is True
        assert by["02 first_pullback"]["fired"] is True
        # Halt-resume dip-buy fires through its own structure trigger.
        assert by["08 halt_resume_dip"]["fired"] is True
        # Every FIRE carries a valid structural stop + risk-first size.
        for name, r in rows:
            if r["fired"]:
                assert r["entry"] is not None and r["stop"] is not None, name
                assert r["stop"] < r["entry"], f"{name}: stop must be below entry"
                assert r["qty"] and r["qty"] > 0, f"{name}: risk-first qty must be positive"

    def test_panel_live_binding(self):
        """Re-run the SAME faithful setups under the LIVE binding (verticality OFF — the
        data-profitable setting the running lane uses). Print the table and assert the
        steady-climb breakout setups CHILI declined only on the strict default (bull_flag /
        abcd / flat_top / new_high) NOW FIRE — i.e. under its real live config CHILI AGREES
        with Ross on those setups too. Anti-patterns still never fire (the chase guard is
        independent of the verticality knob)."""
        rows = _run_panel(verticality_off=True)
        _print_table("ROSS SMALL-ACCOUNT PANEL — LIVE binding (verticality OFF)", rows)
        by = dict(rows)
        for s in ("04 bull_flag", "05 abcd", "06 flat_top", "09 new_high_of_day"):
            assert by[s]["fired"] is True, f"{s} should fire under the live (verticality-off) binding"
            assert by[s]["stop"] < by[s]["entry"]
            assert by[s]["qty"] and by[s]["qty"] > 0
        assert by["11 extended_chase[anti]"]["fired"] is False
        assert by["12 choppy[anti]"]["fired"] is False

    def test_structural_stop_is_pullback_low_and_sizing_is_risk_first(self):
        """Ross's exact stop rule + risk-first sizing, end to end on a clean first-pullback:
        the trigger returns ``pullback_low`` as the structural stop, and
        ``compute_risk_first_quantity`` sizes qty = max_loss / stop_distance, capped at the
        notional ceiling — a TIGHTER stop buys MORE size at constant risk (Ross's edge)."""
        r = _drive_pullback(_first_pullback())
        assert r["fired"] is True, r["reason"]
        assert r["stop"] < r["entry"]
        assert r["qty"] and r["qty"] > 0
        # The risk is the loss budget; notional is capped at the ceiling for a tight-stop name.
        assert r["notional"] is not None and r["notional"] <= _MAX_NOTIONAL_USD + 1e-6

    def test_setups_needing_features_the_offline_mock_cannot_supply(self):
        """HONEST LIMITS — three Ross setups whose LIVE entry routes through a DEDICATED
        trigger that needs a feature an offline (db=None, no tick tape) mock cannot faithfully
        supply. The panel surfaces these as SKIPS with a TRUTHFUL reason rather than tuning the
        geometry to force a fire:

          * micro_pullback  -> ``micro_pullback_primary_confirmation`` has a MANDATORY hot-tape
            gate (``_is_hot_tape``) that FAILS CLOSED with no tape feed. That IS the
            'TAPE REQUIRED + fail-closed' chase-guard working as designed — the tightest/fastest
            entry is refused without live tick tape. ``micro_primary_cold_tape``.
          * vwap_reclaim    -> ``vwap_reclaim_confirmation`` needs the rolling-VWAP proxy warm
            AND K sustained closes below VWAP; a single short dip inside one warmed session is
            not enough below-VWAP context offline -> ``vwap_reclaim_vwap_warmup`` /
            ``waiting_for_vwap_reclaim``. Faithfully reproducing it needs a longer genuinely-
            below-VWAP session segment (a real feed).
          * dip_and_rip     -> a deep flush BELOW the 9-EMA routes to the deep-reclaim / dip-buy
            path which has its own arming + (for the tight level) a tick-thrust confirm; the
            unified raw-break path correctly declines a below-EMA pull (``pullback_below_ema9``).

        These three are reported, not asserted-to-fire — the point of the panel is an HONEST
        read of where CHILI agrees and where the offline mock cannot represent the setup."""
        from app.services.trading.momentum_neural.entry_gates import (
            micro_pullback_primary_confirmation,
            vwap_reclaim_confirmation,
        )

        micro_ok, micro_reason, _ = micro_pullback_primary_confirmation(
            _micro_pullback(), entry_interval="1m", symbol="MOCK", db=None,
        )
        vwap_ok, vwap_reason, _ = vwap_reclaim_confirmation(
            _vwap_reclaim(), entry_interval="1m", symbol="MOCK",
        )
        dip = _drive_pullback(_dip_and_rip())

        print("\nHONEST-LIMIT setups (need a live feature the offline mock cannot supply):")
        print(f"  micro_pullback  dedicated -> fired={micro_ok} reason={micro_reason}")
        print(f"  vwap_reclaim    dedicated -> fired={vwap_ok} reason={vwap_reason}")
        print(f"  dip_and_rip     unified   -> fired={dip['fired']} reason={dip['reason']}")

        # The dedicated micro / vwap-reclaim triggers do NOT fire offline (the honest limit):
        # the micro path is fail-closed on tape / dedicated structure, the vwap-reclaim needs a
        # warm below-VWAP session the offline mock cannot supply. We assert the SKIP (no silent
        # fire), not a brittle exact reason string.
        assert micro_ok is False
        assert vwap_ok is False


# ════════════════════════════════════════════════════════════════════════════════════════════
# REQUIRE_RETEST OVER-GATE ANALYSIS — drive the TRUE live binding (require_retest=TRUE), not the
# False artifact the panel above hardcodes.
#
# The live wrapper momentum_pullback_trigger reads chili_momentum_pullback_require_retest, whose
# config default is TRUE (app/config.py:4061). When True the trigger routes through
# _evaluate_break_retest — the ladder demands break -> pullback-to-break-level -> hold -> reclaim.
# A break that RAN with NO pullback returns ``waiting_for_retest``. The live binding ALSO ships
# chili_momentum_entry_allow_runaway_break=TRUE (config:5201) + runaway_min_volume_spike=2.5
# (config:5206): a ``waiting_for_retest`` that carries pb_high/pb_low in debug is RESCUED by the
# runaway path (entry_gates.py:6310) — it fires the raw break, but at a RAISED 2.5x volume floor
# and through ALL 4 chase-guards. So the real over-gate question is NOT "retest vs raw" in the
# abstract; it is: under the EXACT live binding, do explosive NO-RETEST runaways still get caught
# (via the runaway rescue), or are they MISSED?
#
# This section drives pullback_break_confirmation with the FULL live binding and, for each of the
# 4 raw-break setups, builds TWO faithful frames:
#   (a) RETEST-AND-HOLD — break, genuine pullback back to the break level, hold, reclaim.
#   (b) NO-RETEST RUNAWAY — break then immediate vertical continuation, NO pullback.
# Both go through the REAL trigger under require_retest=True; we record FIRE (and path) vs MISS.
# ════════════════════════════════════════════════════════════════════════════════════════════

# The TRUE live binding the running lane uses (config defaults), NOT the require_retest=False
# artifact. require_retest=TRUE, runaway rescue ON at the real 2.5x floor. Verticality OFF is the
# data-profitable live setting (memory: "+$1,081/3d"); we report under it so the read reflects the
# running lane.
_LIVE_BINDING_KW = dict(
    require_retest=True,
    retest_tolerance=0.002,
    retest_lookback_bars=4,
    allow_runaway_break=True,
    runaway_min_volume_spike=2.5,
    require_sustained_volume=True,
    sustained_rvol_floor=1.0,
    require_break_candle=True,
    require_vwap_hold=True,
    require_macd_bullish=True,
    volume_spike_multiple=1.5,
)


def _drive_live_binding(df: pd.DataFrame, *, symbol: str = "MOCK", live_price=None) -> dict:
    """Drive the REAL trigger under the FULL live binding (require_retest=TRUE + runaway rescue),
    verticality OFF (the live setting). Returns fired / reason / entry / stop / path."""
    saved = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    try:
        ok, reason, dbg = pullback_break_confirmation(
            df, entry_interval="1m", symbol=symbol, db=None, live_price=live_price,
            **_LIVE_BINDING_KW,
        )
    finally:
        eg.settings.chili_momentum_entry_verticality_atr_mult = saved
    # Classify the fire PATH for the verdict: runaway rescue vs clean break-retest vs other.
    path = "miss"
    if ok:
        if dbg.get("runaway"):
            path = "runaway_rescue"
        elif reason == "break_retest":
            path = "break_retest"
        else:
            path = reason
    return {
        "fired": bool(ok), "reason": reason, "path": path,
        "entry": dbg.get("pullback_high"), "stop": dbg.get("pullback_low"),
        "runaway": bool(dbg.get("runaway")),
    }


# ── faithful RETEST-AND-HOLD vs NO-RETEST-RUNAWAY frame pairs for the 4 raw-break setups ──
# Each pair shares the same warmed/up->=10% lead-in and the same broken LEVEL; they differ ONLY in
# what happens AFTER the break: (a) a genuine pullback back to the level then a reclaim, vs (b) an
# immediate vertical continuation with no pullback. retest_lookback_bars=4 reserves the last 4 bars
# for the break->retest->reclaim sequence, so each frame ends with >=5 post-base bars.

def _gap_and_go_retest() -> pd.DataFrame:
    """Gap-and-go: break the consolidation high ~3.30, pull back to ~3.30, hold, reclaim."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.27, 6_000_000),   # drive to the break level ~3.30
        (3.27, 3.42, 3.26, 3.40, 12_000_000),  # BREAK above 3.30 (tail bar pierces level)
        (3.40, 3.41, 3.31, 3.33, 4_000_000),   # pull back DOWN to ~the level (retest)
        (3.33, 3.36, 3.30, 3.34, 3_500_000),   # HOLD the level (close stays above)
        (3.34, 3.46, 3.33, 3.44, 11_000_000),  # RECLAIM — current bar trades back over level
    ])


def _gap_and_go_runaway() -> pd.DataFrame:
    """Gap-and-go RUNAWAY: same break level ~3.30, but price RUNS vertically — NO pullback ever
    comes back to the level. The retest ladder waits forever; only the runaway rescue can catch it."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.27, 6_000_000),   # drive to the break level ~3.30
        (3.27, 3.42, 3.26, 3.40, 12_000_000),  # BREAK above 3.30
        (3.40, 3.56, 3.39, 3.54, 13_000_000),  # RUN — no pullback, higher
        (3.54, 3.70, 3.53, 3.68, 13_000_000),  # RUN — higher still
        (3.68, 3.84, 3.67, 3.82, 14_000_000),  # RUN — vertical continuation, level never retested
    ])


def _bull_flag_retest() -> pd.DataFrame:
    """Bull flag: pole, flag, break the flag high ~3.37, pull back to ~3.37, hold, reclaim."""
    w, _ = _warm(2.60, 12, 0.03)
    return _ohlcv(w + [
        (2.96, 3.20, 2.95, 3.18, 7_000_000), (3.18, 3.40, 3.17, 3.38, 8_000_000),   # pole
        (3.38, 3.40, 3.30, 3.33, 1_800_000), (3.33, 3.37, 3.28, 3.31, 1_600_000),   # flag (top ~3.40)
        (3.31, 3.46, 3.30, 3.44, 12_000_000),  # BREAK the flag/pole high ~3.40
        (3.44, 3.45, 3.39, 3.41, 4_000_000),   # pull back to ~the level (retest)
        (3.41, 3.44, 3.39, 3.42, 3_500_000),   # HOLD
        (3.42, 3.52, 3.41, 3.50, 11_000_000),  # RECLAIM
    ])


def _bull_flag_runaway() -> pd.DataFrame:
    """Bull flag RUNAWAY: same pole+flag+break ~3.40, then a vertical run with NO pullback."""
    w, _ = _warm(2.60, 12, 0.03)
    return _ohlcv(w + [
        (2.96, 3.20, 2.95, 3.18, 7_000_000), (3.18, 3.40, 3.17, 3.38, 8_000_000),   # pole
        (3.38, 3.40, 3.30, 3.33, 1_800_000), (3.33, 3.37, 3.28, 3.31, 1_600_000),   # flag
        (3.31, 3.46, 3.30, 3.44, 12_000_000),  # BREAK ~3.40
        (3.44, 3.60, 3.43, 3.58, 13_000_000),  # RUN
        (3.58, 3.74, 3.57, 3.72, 13_000_000),  # RUN
        (3.72, 3.88, 3.71, 3.86, 14_000_000),  # RUN — never retests 3.40
    ])


def _abcd_retest() -> pd.DataFrame:
    """ABCD: A-surge, B-pull, C higher-low, break the B/BC high ~3.30, retest, hold, reclaim."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.28, 6_000_000),   # A surge (sets the B/BC high ~3.30)
        (3.28, 3.30, 3.18, 3.20, 2_000_000),   # B pull low
        (3.20, 3.29, 3.19, 3.27, 3_000_000),   # BC high
        (3.27, 3.29, 3.22, 3.24, 1_800_000),   # C higher-low
        (3.24, 3.40, 3.23, 3.38, 11_000_000),  # BREAK the B/BC high ~3.30 (toward D)
        (3.38, 3.39, 3.31, 3.33, 4_000_000),   # retest ~the level
        (3.33, 3.36, 3.30, 3.34, 3_500_000),   # HOLD
        (3.34, 3.46, 3.33, 3.44, 11_000_000),  # RECLAIM
    ])


def _abcd_runaway() -> pd.DataFrame:
    """ABCD RUNAWAY: same A/B/C, break the B/BC high ~3.30, then vertical with NO pullback."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.28, 6_000_000),   # A surge
        (3.28, 3.30, 3.18, 3.20, 2_000_000),   # B pull low
        (3.20, 3.29, 3.19, 3.27, 3_000_000),   # BC high
        (3.27, 3.29, 3.22, 3.24, 1_800_000),   # C higher-low
        (3.24, 3.40, 3.23, 3.38, 11_000_000),  # BREAK ~3.30
        (3.38, 3.54, 3.37, 3.52, 13_000_000),  # RUN
        (3.52, 3.68, 3.51, 3.66, 13_000_000),  # RUN
        (3.66, 3.82, 3.65, 3.80, 14_000_000),  # RUN — D leg never retests B/BC high
    ])


def _flat_top_retest() -> pd.DataFrame:
    """Flat-top: 3 tests of ~3.30, break it, pull back to ~3.30, hold, reclaim."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.27, 6_000_000),   # drive to flat top ~3.30
        (3.27, 3.30, 3.24, 3.26, 1_800_000),   # test 1
        (3.26, 3.30, 3.24, 3.27, 1_700_000),   # test 2
        (3.27, 3.42, 3.26, 3.40, 12_000_000),  # BREAK the flat top ~3.30
        (3.40, 3.41, 3.31, 3.33, 4_000_000),   # retest ~the level
        (3.33, 3.36, 3.30, 3.34, 3_500_000),   # HOLD
        (3.34, 3.46, 3.33, 3.44, 11_000_000),  # RECLAIM
    ])


def _flat_top_runaway() -> pd.DataFrame:
    """Flat-top RUNAWAY: same triple-test + break ~3.30, then vertical with NO pullback."""
    w, _ = _warm(2.70, 12, 0.03)
    return _ohlcv(w + _cons(3.06) + [
        (3.10, 3.30, 3.09, 3.27, 6_000_000),   # drive to flat top ~3.30
        (3.27, 3.30, 3.24, 3.26, 1_800_000),   # test 1
        (3.26, 3.30, 3.24, 3.27, 1_700_000),   # test 2
        (3.27, 3.42, 3.26, 3.40, 12_000_000),  # BREAK ~3.30
        (3.40, 3.56, 3.39, 3.54, 13_000_000),  # RUN
        (3.54, 3.70, 3.53, 3.68, 13_000_000),  # RUN
        (3.68, 3.84, 3.67, 3.82, 14_000_000),  # RUN — flat top never retested
    ])


# (setup, retest-and-hold builder, no-retest-runaway builder)
_RAW_BREAK_PAIRS = [
    ("gap_and_go", _gap_and_go_retest, _gap_and_go_runaway),
    ("bull_flag", _bull_flag_retest, _bull_flag_runaway),
    ("abcd", _abcd_retest, _abcd_runaway),
    ("flat_top", _flat_top_retest, _flat_top_runaway),
]


class TestRequireRetestOverGate:
    """Quantify whether require_retest=TRUE (the LIVE binding) is a real over-gate that misses
    explosive no-retest runaways Ross would catch on the raw break."""

    def test_retest_and_runaway_under_live_binding(self):
        results = {}
        print("\n" + "=" * 96)
        print("REQUIRE_RETEST OVER-GATE ANALYSIS — require_retest=TRUE (LIVE), runaway rescue ON @2.5x")
        print("=" * 96)
        print(f"{'setup':14s} {'variant':18s} {'fired':6s} {'path':22s} {'reason':28s}")
        print("-" * 96)
        for name, build_retest, build_runaway in _RAW_BREAK_PAIRS:
            rt = _drive_live_binding(build_retest())
            rn = _drive_live_binding(build_runaway())
            results[name] = {"retest": rt, "runaway": rn}
            for variant, r in (("RETEST-AND-HOLD", rt), ("NO-RETEST-RUNAWAY", rn)):
                print(
                    f"{name:14s} {variant:18s} "
                    f"{('FIRE' if r['fired'] else 'MISS'):6s} {r['path']:22s} {r['reason']:28s}"
                )

        # ── VERDICT TABLE ──
        # ATTRIBUTION: a runaway MISS only counts as a REQUIRE_RETEST over-gate when it is
        # ASYMMETRIC — the retest-and-hold variant FIRED (so the setup is otherwise valid and the
        # ONLY difference is the missing pullback) but the runaway was MISSED. A SYMMETRIC miss
        # (both variants miss) is a DIFFERENT gate (e.g. the sustained-volume gate vetoing a
        # low-volume flag base) and is NOT attributable to require_retest. The isolated test below
        # (runaway rescue forced OFF) measures the PURE retest-ladder cost without that confound.
        print("\n" + "-" * 96)
        print("VERDICT (per setup, under the LIVE binding):")
        retest_fires = {}
        runaway_missed = {}       # raw: runaway did not fire (any cause)
        overgate_attrib = {}      # asymmetric: retest fired AND runaway missed -> require_retest cost
        for name in results:
            rt = results[name]["retest"]
            rn = results[name]["runaway"]
            retest_fires[name] = rt["fired"]
            runaway_missed[name] = not rn["fired"]
            overgate_attrib[name] = bool(rt["fired"]) and (not rn["fired"])
            print(
                f"  {name:12s} retest_fires={str(rt['fired']):5s} "
                f"(path={rt['path']:16s})  no_retest_missed={str(not rn['fired']):5s} "
                f"(runaway path={rn['path']:16s} reason={rn['reason']})"
                f"  overgate_attrib={overgate_attrib[name]}"
            )

        any_runaway_missed = any(runaway_missed.values())
        any_overgate_attrib = any(overgate_attrib.values())
        all_retest_fire = all(retest_fires.values())
        print("\n" + "-" * 96)
        print(f"  retest-and-hold fires on ALL 4 setups            : {all_retest_fire}")
        print(f"  any NO-RETEST RUNAWAY MISSED (any cause)         : {any_runaway_missed}")
        print(f"  any ASYMMETRIC miss (retest fired, runaway lost) : {any_overgate_attrib}")
        print(f"  IS_REAL_OVERGATE under the FULL live binding     : {any_overgate_attrib}")
        if not any_overgate_attrib:
            print("  -> Under the FULL live binding require_retest=TRUE does NOT lose any runaway that")
            print("     its own retest variant would have taken: the allow_runaway_break=TRUE rescue")
            print("     (config:5201, @2.5x vol) + the deep-reclaim path catch every no-retest runaway,")
            print("     through all 4 chase-guards. (A symmetric both-miss is a DIFFERENT gate.)")
            print("     The retest-ladder cost is REAL but BACKSTOPPED — see the isolated test for the")
            print("     pure cost when the rescue is forced OFF.")
        else:
            missed = [n for n, m in overgate_attrib.items() if m]
            print(f"  -> require_retest=TRUE DOES over-gate (asymmetric): runaways MISSED on {missed}")
        print("=" * 96)

        # CAPABILITY assertion: a faithful retest-and-hold must fire under the live binding on the
        # cleanest setup (confirms the retest ladder is not dead). The runaway/over-gate read is
        # REPORTED (printed above) — not asserted to a fixed direction — because the whole point is
        # to MEASURE it, and the binding (runaway rescue) can flip it.
        assert results["gap_and_go"]["retest"]["fired"] is True, (
            "retest-and-hold gap_and_go must fire under the live require_retest=TRUE binding"
        )
        # Expose the over-gate measurement to the harness via the test's recorded result.
        self._overgate = any_overgate_attrib  # noqa: introspectable

    def test_overgate_isolated_runaway_rescue_off(self):
        """ISOLATE the require_retest cost: with the runaway rescue FORCED OFF (allow_runaway_break
        =False), do the NO-RETEST runaways get missed under require_retest=TRUE? This measures the
        PURE retest-ladder cost independent of the rescue — i.e. what require_retest would cost if
        the rescue weren't there to backstop it."""
        kw = dict(_LIVE_BINDING_KW)
        kw["allow_runaway_break"] = False
        saved = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
        eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
        print("\n" + "=" * 96)
        print("ISOLATED retest-ladder cost — require_retest=TRUE, runaway rescue FORCED OFF")
        print("=" * 96)
        print(f"{'setup':14s} {'no-retest-runaway':18s} {'fired':6s} {'reason':30s}")
        print("-" * 96)
        missed = []
        try:
            for name, _build_retest, build_runaway in _RAW_BREAK_PAIRS:
                ok, reason, dbg = pullback_break_confirmation(
                    build_runaway(), entry_interval="1m", symbol="MOCK", db=None, **kw,
                )
                if not ok:
                    missed.append((name, reason))
                print(f"{name:14s} {'RUNAWAY':18s} {('FIRE' if ok else 'MISS'):6s} {reason:30s}")
        finally:
            eg.settings.chili_momentum_entry_verticality_atr_mult = saved
        print("-" * 96)
        print(f"  runaways MISSED with rescue OFF: {[m[0] for m in missed]}")
        print(f"  -> This is the cost require_retest WOULD impose without the allow_runaway_break")
        print(f"     rescue. The rescue (default ON) is what neutralizes the over-gate.")
        print("=" * 96)
        # No hard direction assertion — this is the measurement. We DO assert it ran clean.
        assert isinstance(missed, list)


# ════════════════════════════════════════════════════════════════════════════════════════════
# ADAPTIVE-RETEST EXPLOSIVE RAW-BREAK ESCAPE — the new guarded raw-break that fires the
# NO-RETEST explosive runaway under require_retest=TRUE, gated by RVOL + tape thrust, with the
# allow_runaway_break rescue FORCED OFF so the escape is the ONLY thing that can fire it.
#
# These tests drive the REAL pullback_break_confirmation under require_retest=TRUE with the
# runaway rescue OFF (allow_runaway_break=False) and the new flag
# chili_momentum_pullback_raw_break_when_explosive ON. With the rescue off, a no-retest runaway
# is stranded at waiting_for_retest UNLESS the explosive escape fires it. A confirming tape is
# injected by monkeypatching eg.signed_tape_accel_features (the live tape feed is a DB read the
# offline mock cannot supply; the escape FAILS CLOSED with no tape exactly as the live lane does,
# so to exercise the FIRE path the test supplies the tape the escape requires).
# ════════════════════════════════════════════════════════════════════════════════════════════

# Escape-test binding: require_retest TRUE, runaway rescue OFF (so the escape is the sole path),
# the new explosive raw-break flag ON. Verticality is handled per-test.
_ESCAPE_KW = dict(
    require_retest=True,
    retest_tolerance=0.002,
    retest_lookback_bars=4,
    allow_runaway_break=False,          # rescue OFF — the escape is the ONLY thing that can fire
    runaway_min_volume_spike=2.5,
    require_sustained_volume=True,
    sustained_rvol_floor=1.0,
    require_break_candle=True,
    require_vwap_hold=True,
    require_macd_bullish=True,
    volume_spike_multiple=1.5,
)


def _confirming_tape(*_a, **_k):
    """A live-shaped tape that CONFIRMS explosive thrust: positive signed_tape_accel (the ask is
    getting eaten) AND tick_rate strictly ABOVE its self-relative floor (the strong-thrust proxy
    the escape requires when back_half_buy_vol is not exposed)."""
    return {
        "signed_tape_accel": 50_000.0,
        "tick_rate": 12.0,
        "tick_rate_floor": 3.0,
        "n_ticks": 40,
    }


def _nonconfirming_tape(*_a, **_k):
    """A tape that does NOT confirm: signed_tape_accel <= 0 (the ask is NOT being eaten)."""
    return {
        "signed_tape_accel": -10_000.0,
        "tick_rate": 12.0,
        "tick_rate_floor": 3.0,
        "n_ticks": 40,
    }


def _at_floor_tape(*_a, **_k):
    """A positive-accel tape whose tick_rate is only AT the floor (not above) — fails the
    strong-thrust proxy (the back-half-buy-vol key is not exposed, so the escape demands a
    strictly-rising tape). A weak/marginal break must NOT escape on this."""
    return {
        "signed_tape_accel": 50_000.0,
        "tick_rate": 3.0,
        "tick_rate_floor": 3.0,
        "n_ticks": 40,
    }


def _drive_escape(df, *, tape_fn, flag_on=True, verticality_off=True,
                  symbol="MOCK", live_price=None, extra_kw=None, monkeypatch=None):
    """Drive the REAL trigger under the escape binding with the tape monkeypatched. Returns
    fired / reason / entry / stop / explosive_raw_break / debug."""
    kw = dict(_ESCAPE_KW)
    if extra_kw:
        kw.update(extra_kw)
    saved_vert = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    saved_flag = getattr(eg.settings, "chili_momentum_pullback_raw_break_when_explosive", False)
    if verticality_off:
        eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    eg.settings.chili_momentum_pullback_raw_break_when_explosive = bool(flag_on)
    saved_tape = eg.signed_tape_accel_features
    eg.signed_tape_accel_features = tape_fn
    try:
        ok, reason, dbg = pullback_break_confirmation(
            df, entry_interval="1m", symbol=symbol, db=object(), live_price=live_price, **kw,
        )
    finally:
        eg.settings.chili_momentum_entry_verticality_atr_mult = saved_vert
        eg.settings.chili_momentum_pullback_raw_break_when_explosive = saved_flag
        eg.signed_tape_accel_features = saved_tape
    return {
        "fired": bool(ok), "reason": reason,
        "entry": dbg.get("pullback_high"), "stop": dbg.get("pullback_low"),
        "explosive_raw_break": bool(dbg.get("explosive_raw_break")),
        "debug": dbg,
    }


def _explosive_gap_and_go_runaway() -> pd.DataFrame:
    """A GENUINELY EXPLOSIVE gap-and-go NO-RETEST runaway: a QUIET warm-up (low volume) then a
    break + vertical run on ESCALATING, HUGE volume so the trigger-bar rolling RVOL clears the
    explosive floor (>5x) — exactly the vertical runner Ross takes on the raw break. The break
    level ~3.30 is never retested (the run only goes up), so the retest ladder strands it at
    waiting_for_retest unless the explosive escape fires it."""
    w, _ = _warm(2.70, 12, 0.03, vol=800_000)
    return _ohlcv(w + _cons(3.06, vol=700_000) + [
        (3.10, 3.30, 3.09, 3.27, 1_500_000),   # drive to the break level ~3.30 (quiet)
        (3.27, 3.42, 3.26, 3.40, 6_000_000),   # BREAK above 3.30
        (3.40, 3.56, 3.39, 3.54, 9_000_000),   # RUN
        (3.54, 3.70, 3.53, 3.68, 14_000_000),  # RUN
        (3.68, 3.86, 3.67, 3.84, 22_000_000),  # RUN — last bar huge vol, RVOL ~6.9x, never retests
    ])


def _marginal_gap_and_go_runaway() -> pd.DataFrame:
    """A NON-explosive gap-and-go NO-RETEST break: same geometry but the break + continuation
    volumes are only ~1x the warmed session (RVOL < 5x). It ran a little but is NOT an explosive
    runaway — even with a confirming tape it must NOT escape (the RVOL gate keeps the retest
    discipline for marginal breaks, exactly Ross's 'retest on the weaker ones')."""
    w, _ = _warm(2.70, 12, 0.03, vol=2_000_000)
    return _ohlcv(w + _cons(3.06, vol=1_900_000) + [
        (3.10, 3.30, 3.09, 3.27, 2_100_000),   # drive to the break level ~3.30 (low vol)
        (3.27, 3.42, 3.26, 3.40, 2_300_000),   # BREAK above 3.30 — but only ~1.1x RVOL
        (3.40, 3.48, 3.39, 3.46, 2_200_000),   # run, low vol
        (3.46, 3.54, 3.45, 3.52, 2_100_000),   # run, low vol
        (3.52, 3.60, 3.51, 3.58, 2_000_000),   # run — never retests, low vol throughout
    ])


class TestExplosiveRawBreakEscape:
    """The ADAPTIVE-RETEST explosive raw-break escape: under require_retest=TRUE with the runaway
    rescue OFF, a genuinely EXPLOSIVE no-retest runaway (the one that is stranded at
    waiting_for_retest) NOW fires through all 4 chase-guards, while a marginal break still waits
    for the retest, the tape is required + fail-closed, the guards still gate, and flag-OFF is
    byte-identical.

    NOTE on setup choice: of the 4 raw-break pairs, only gap_and_go's runaway routes to a clean
    ``waiting_for_retest`` under the live ladder (abcd/flat_top runaways are caught by the
    deep-reclaim path as ``deep_reclaim_ok``, and bull_flag is a SYMMETRIC ``faded_volume_no_sustain``
    miss independent of require_retest — see TestRequireRetestOverGate). So the
    ``waiting_for_retest`` escape is exercised on a dedicated explosive gap_and_go runaway — the
    exact case the escape exists to catch."""

    def test_explosive_runaway_now_fires_with_guards(self):
        """The explosive NO-RETEST gap_and_go runaway — stranded at waiting_for_retest with the
        rescue OFF — NOW fires via the explosive escape when the flag is ON and the tape confirms
        thrust. The fire carries a valid structural stop BELOW the entry and the observable
        explosive_raw_break reason."""
        print("\n" + "=" * 96)
        print("ADAPTIVE-RETEST ESCAPE — explosive NO-RETEST runaway fires (rescue OFF, flag ON, tape confirms)")
        print("=" * 96)
        r = _drive_escape(_explosive_gap_and_go_runaway(), tape_fn=_confirming_tape)
        print(f"  explosive gap_and_go runaway -> fired={r['fired']} reason={r['reason']} "
              f"rvol={r['debug'].get('raw_break_rvol')} floor={r['debug'].get('raw_break_rvol_floor')} "
              f"entry={r['entry']} stop={r['stop']}")
        print("=" * 96)
        assert r["fired"] is True, f"explosive runaway should fire via the escape: {r['reason']}"
        assert r["explosive_raw_break"] is True, "should fire via explosive_raw_break"
        assert r["reason"] in ("explosive_raw_break_ok", "explosive_raw_break_tick_ok"), r["reason"]
        # Structural-stop chase-guard: a valid stop BELOW the entry (Ross's exact stop rule).
        assert r["stop"] is not None and r["entry"] is not None
        assert r["stop"] < r["entry"], "stop must be below entry"
        # RVOL cleared the explosive floor (the adaptive gate let it through, not a fluke).
        assert r["debug"].get("raw_break_rvol") >= r["debug"].get("raw_break_rvol_floor")

    def test_marginal_no_retest_break_still_waits_for_retest(self):
        """A NON-explosive (RVOL below the explosive floor) no-retest break does NOT escape even
        with a confirming tape — the RVOL gate keeps the retest discipline for marginal breaks.
        It stays waiting_for_retest (rescue OFF), exactly Ross's 'retest on the weaker ones'."""
        r = _drive_escape(_marginal_gap_and_go_runaway(), tape_fn=_confirming_tape)
        print(f"\nmarginal no-retest break: fired={r['fired']} reason={r['reason']} "
              f"rvol_floor={r['debug'].get('raw_break_rvol_floor')} rvol={r['debug'].get('raw_break_rvol')} "
              f"blocked={r['debug'].get('raw_break_blocked')}")
        assert r["fired"] is False, f"marginal break must NOT escape (RVOL gate): {r['reason']}"
        assert r["explosive_raw_break"] is False
        assert r["reason"] == "waiting_for_retest"
        assert r["debug"].get("raw_break_blocked") == "rvol_below_explosive_floor"

    def test_tape_required_fail_closed_no_tape(self):
        """TAPE REQUIRED + FAIL-CLOSED: with NO tape (signed_tape_accel_features returns None,
        the live db=None / empty-tape case), an explosive runaway does NOT escape — the retest
        discipline stays in force. Proves the escape never fires blind."""
        r = _drive_escape(_explosive_gap_and_go_runaway(), tape_fn=lambda *a, **k: None)
        print(f"\nno-tape explosive runaway: fired={r['fired']} reason={r['reason']} "
              f"blocked={r['debug'].get('raw_break_blocked')}")
        assert r["fired"] is False, f"no-tape runaway must NOT escape (fail-closed): {r['reason']}"
        assert r["explosive_raw_break"] is False
        assert r["reason"] == "waiting_for_retest"
        assert r["debug"].get("raw_break_blocked") == "tape_required_fail_closed"

    def test_tape_not_confirming_no_escape(self):
        """The tape gate: an explosive runaway with a NET-SELLING tape (signed_tape_accel <= 0 —
        the ask is NOT being eaten) does NOT escape. The thrust-confirmation guard works."""
        r = _drive_escape(_explosive_gap_and_go_runaway(), tape_fn=_nonconfirming_tape)
        print(f"\nnon-confirming-tape runaway: fired={r['fired']} reason={r['reason']} "
              f"blocked={r['debug'].get('raw_break_blocked')}")
        assert r["fired"] is False
        assert r["explosive_raw_break"] is False
        assert r["reason"] == "waiting_for_retest"
        assert r["debug"].get("raw_break_blocked") == "tape_not_confirming"

    def test_weak_thrust_at_floor_no_escape(self):
        """STRONG-THRUST guard: positive accel but a tick_rate only AT the floor (not strictly
        above) fails the strong-thrust proxy — a flat at-floor tape is not the surge the escape
        demands, so no escape."""
        r = _drive_escape(_explosive_gap_and_go_runaway(), tape_fn=_at_floor_tape)
        print(f"\nat-floor-tape runaway: fired={r['fired']} reason={r['reason']} "
              f"blocked={r['debug'].get('raw_break_blocked')}")
        assert r["fired"] is False
        assert r["explosive_raw_break"] is False
        assert r["reason"] == "waiting_for_retest"
        assert r["debug"].get("raw_break_blocked") == "thrust_not_strong"

    def test_chase_guards_still_gate_on_the_escape_path(self):
        """The 4 chase-guards STILL run on the escape path. Turn the VERTICALITY veto back ON
        (mult=1.5, source default) and feed a confirming tape on a frame that PASSES the RVOL +
        tape gates: the explosive runaway's vertical extension bars must be VETOed by the
        extension/verticality guard even though the escape set ok_t — proving the guard runs
        AFTER the escape (a GUARDED raw break, not a chase). With verticality OFF the SAME frame
        fires, so the only difference is the guard."""
        on = _drive_escape(_explosive_gap_and_go_runaway(), tape_fn=_confirming_tape,
                           verticality_off=False)
        off = _drive_escape(_explosive_gap_and_go_runaway(), tape_fn=_confirming_tape,
                            verticality_off=True)
        print(f"\n  verticality ON  -> fired={on['fired']} reason={on['reason']}")
        print(f"  verticality OFF -> fired={off['fired']} reason={off['reason']}")
        # OFF: the escape fires. ON: the verticality chase-guard vetoes the SAME escape candidate.
        assert off["fired"] is True and off["explosive_raw_break"] is True
        assert on["fired"] is False, "verticality chase-guard must veto the vertical escape candidate"
        assert on["reason"] == "extended_verticality", on["reason"]

    def test_flag_off_byte_identical(self):
        """Flag OFF ⇒ BYTE-IDENTICAL to the current require_retest ladder. With the escape flag
        OFF (and the runaway rescue OFF), the explosive runaway stays waiting_for_retest with NO
        explosive_raw_break debug keys — identical to the pre-change behaviour. We assert flag-ON
        fires it and flag-OFF leaves it exactly as the unchanged ladder did (waiting_for_retest,
        no escape-debug keys)."""
        df_on = _explosive_gap_and_go_runaway()
        on = _drive_escape(df_on, tape_fn=_confirming_tape, flag_on=True)
        assert on["fired"] is True and on["explosive_raw_break"] is True, on["reason"]

        df_off = _explosive_gap_and_go_runaway()
        off = _drive_escape(df_off, tape_fn=_confirming_tape, flag_on=False)
        assert off["fired"] is False, f"flag OFF must not fire (rescue also OFF): {off['reason']}"
        assert off["explosive_raw_break"] is False
        assert off["reason"] == "waiting_for_retest", off["reason"]
        # No escape-debug keys leak when the flag is OFF (byte-identical debug surface).
        for k in (
            "raw_break_rvol_floor", "raw_break_rvol", "raw_break_explosive",
            "raw_break_blocked", "explosive_raw_break",
        ):
            assert k not in off["debug"], f"flag-OFF debug must not carry {k}"
        print(f"\n  flag ON  -> fired={on['fired']} reason={on['reason']}")
        print(f"  flag OFF -> fired={off['fired']} reason={off['reason']} (byte-identical, no escape keys)")
