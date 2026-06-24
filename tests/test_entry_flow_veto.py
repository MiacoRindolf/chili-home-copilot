"""Entry-TIME flow veto (the PLSM/RUN-flush fix): defer the BUY this tick when the
executed tape is selling. Distinct from the selection-time OFI tilt and from the
static-book L2 seller-veto. Two veto legs (OR):

  * AND-leg (both-bearish): OFI <= ofi_thr (-0.6) AND trade_flow <= tf_thr (-0.25) —
    both the resting-book pressure and the executed tape lean net-selling (PLSM flush).
  * STRONG-tape OR-leg: trade_flow <= tf_strong_thr (-0.5) ALONE, regardless of OFI —
    a STRONGLY-selling executed tape vetoes even when OFI looks mildly positive (the
    06-24 RUN case: ofi=+0.5 mild buy yet trade_flow=-0.63 strong executed selling,
    which the strict AND-leg missed).

These tests pin the pure decision helper `_entry_flow_veto`:

  (a) FIRES on the strict AND of two NEGATIVE-threshold breaches (PLSM case).
  (a2) FIRES on the STRONG-tape OR-leg: RUN-shape (mildly-positive OFI but strongly
       selling tape), and a strong-selling tape with OFI absent (None) or mildly +.
  (b) NO-OP when the kill-switch flag is OFF (byte-identical / parity).
  (c) NO-OP when either flow is None (absent L2 / absent tape -> never veto).
  (d) NO-OP when only ONE leg is mildly bearish (neither the AND nor the STRONG-leg);
      a normal MILD-negative tape (trade_flow=-0.2) does NOT over-veto.
  (e) FIRES for an extreme mover too: the veto is an ENTRY-TIMING gate and is NOT
      gated by _extreme_mover (ross>=0.8) — the helper has no selection escape hatch,
      so a max-selling tick is vetoed for explosives exactly like any other name.

Pure / no DB: mirrors tests/test_ofi_microprice_tilt.py (imports settings + helper).
"""

from __future__ import annotations

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import _entry_flow_veto


# ── (a) veto FIRES on the bearish AND (the PLSM flush) ────────────────────────

def test_veto_fires_on_bearish_and_plsm_case():
    # PLSM: OFI=-1.0, trade_flow=-0.51 — both past the -0.6 / -0.25 defaults.
    assert _entry_flow_veto(-1.0, -0.51, settings) is True


def test_veto_fires_exactly_at_thresholds():
    # at/below (<=) the defaults -0.6 / -0.25 -> veto.
    assert _entry_flow_veto(-0.6, -0.25, settings) is True


# ── (a2) veto FIRES on the STRONG-tape OR-leg (regardless of OFI) ─────────────

def test_veto_fires_on_run_shape_strong_tape_with_mild_positive_ofi():
    # 06-24 RUN: OFI=+0.5 (mildly BUYING resting book) but trade_flow=-0.63 (strongly
    # SELLING executed tape). The strict AND-leg fails (ofi is positive) yet the
    # STRONG-tape OR-leg (tf <= -0.5) fires -> veto. This is the bug the OR-leg fixes.
    assert _entry_flow_veto(0.5, -0.63, settings) is True


def test_veto_fires_on_strong_selling_tape_with_ofi_none():
    # Strong-selling tape ALONE (trade_flow=-0.7) vetoes even when OFI is absent (None):
    # the OR-leg keys only on trade_flow, so a missing book does NOT save the entry.
    assert _entry_flow_veto(None, -0.7, settings) is True


def test_veto_fires_on_strong_selling_tape_with_mild_positive_ofi():
    # Strong-selling tape (trade_flow=-0.7) with a mildly-POSITIVE OFI (+0.3) still
    # vetoes via the OR-leg — the executed tape is the most direct sellers-winning signal.
    assert _entry_flow_veto(0.3, -0.7, settings) is True


def test_veto_fires_exactly_at_strong_threshold():
    # at/below (<=) the strong default -0.5 -> OR-leg fires regardless of OFI.
    assert _entry_flow_veto(0.4, -0.5, settings) is True


# ── (b) NO-OP when the kill-switch flag is OFF (parity) ───────────────────────

def test_veto_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_entry_flow_veto_enabled", False)
    # even the strongest selling tick must NOT veto when disabled (byte-identical path).
    assert _entry_flow_veto(-1.0, -1.0, settings) is False


def test_veto_noop_when_flag_off_on_run_shape(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_entry_flow_veto_enabled", False)
    # the RUN-shape that fires the OR-leg when ON must be byte-identical (no veto) when OFF.
    assert _entry_flow_veto(0.5, -0.63, settings) is False


# ── (c) NO-OP / parity when either flow is absent (None) ──────────────────────

def test_veto_noop_when_ofi_none():
    # OFI absent: the AND-leg needs OFI, so it can't fire; and a MILD tape (-0.3, above the
    # strong -0.5 threshold) doesn't fire the OR-leg either -> no veto. (A strongly-selling
    # tape WOULD fire the OR-leg with OFI=None — see the (a2) strong-tape tests.)
    assert _entry_flow_veto(None, -0.3, settings) is False


def test_veto_noop_when_trade_flow_none():
    assert _entry_flow_veto(-1.0, None, settings) is False


def test_veto_noop_when_both_none():
    assert _entry_flow_veto(None, None, settings) is False


# ── (d) NO-OP when the AND-leg fails AND the tape is not STRONGLY selling ─────
# (The AND-leg needs BOTH legs bearish; the OR-leg needs a STRONG tape <= -0.5. So a single
# mildly-bearish leg with a tape ABOVE -0.5 fires neither — no over-veto.)

def test_veto_noop_when_only_ofi_bearish():
    # OFI past threshold but trade_flow bullish -> AND fails, OR-leg fails (tape positive) -> no veto.
    assert _entry_flow_veto(-1.0, 0.1, settings) is False


def test_veto_noop_when_only_ofi_bearish_mild_tape():
    # OFI strongly negative but the tape (-0.1) is ABOVE the AND tf_thr (-0.25) AND above the
    # strong -0.5: only the OFI leg is bearish -> AND fails, OR fails -> no veto.
    assert _entry_flow_veto(-1.0, -0.1, settings) is False


def test_veto_noop_when_ofi_above_threshold_mild_tape():
    # OFI=-0.5 is ABOVE the -0.6 AND floor (less negative) and tf=-0.3 is ABOVE the strong
    # -0.5 threshold -> AND fails (ofi not past) AND OR fails (tape not strong) -> no veto.
    assert _entry_flow_veto(-0.5, -0.3, settings) is False


def test_veto_noop_when_trade_flow_above_threshold():
    # trade_flow=-0.1 is ABOVE the -0.25 floor -> AND fails even though OFI is past.
    assert _entry_flow_veto(-0.7, -0.1, settings) is False


def test_veto_noop_on_bullish_flow():
    assert _entry_flow_veto(0.3, 0.3, settings) is False


def test_veto_noop_on_normal_mild_negative_tape():
    # A NORMAL healthy entry on a mildly-negative executed tape (trade_flow=-0.2) with a
    # positive OFI must NOT veto: -0.2 is ABOVE both the AND tf_thr (-0.25) and the STRONG
    # tf_strong_thr (-0.5), so neither leg fires. Guards against over-veto / chop-starving
    # the lane — the OR-leg only bites a STRONGLY selling tape, not ordinary tape noise.
    assert _entry_flow_veto(0.2, -0.2, settings) is False
    # mid-band: tf=-0.3 trips the AND tf_thr but OFI is positive (AND fails) and -0.3 is
    # above the STRONG -0.5 (OR fails) -> still no veto.
    assert _entry_flow_veto(0.2, -0.3, settings) is False


# ── (e) the veto STILL FIRES for an extreme mover (not skipped by _extreme_mover) ─

def test_veto_fires_for_extreme_mover():
    # The entry-timing veto is NOT gated by the selection-time ross>=0.8 tail rule:
    # the pure helper has no _extreme_mover escape hatch, so a max-selling tick is
    # vetoed for explosives exactly like any other name. (PLSM-shape flow on what
    # would be an extreme Ross mover.)
    assert _entry_flow_veto(-1.0, -0.51, settings) is True
    assert _entry_flow_veto(-0.95, -0.9, settings) is True
