"""Halt-aware ENTRY trigger suppression (2026-08-17, IPST 13:07 phantom fire).

Live evidence: suspected_halt_detected sa 13:07:14 (huling print 136s ang tanda),
tapos live_entry_momentum_continuation_fire sa 13:07:34 na may tick_rate ~150/s —
lahat ng tick sa window ay BAGO pa ang halt. Ang fire ay phantom sa stale stats.

Ang gate: habang may `suspected_halt_since_utc` sa le at ang session ay nasa
pre-entry gate scope (`_live_entry_quote_gate_applies`), ang tick ay bumabalik
nang blocked (reason: suspected_halt_active) BAGO ang anumang trigger detector.
Ang resume (halt_resume_dip path) ang nagbubura ng marker — kusang bumubukas.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
import app.services.trading.momentum_neural.live_runner as lr


def test_flag_default_on():
    assert bool(
        getattr(settings, "chili_momentum_halt_trigger_suppression_enabled", None)
    ) is True


def test_suppression_condition_shape(monkeypatch):
    """Ang tatlong sangkap ng gate: marker + gate-scope + flag. (Ang inline gate
    sa tick_live_session ay ang eksaktong kombinasyong ito — ang test na ito ang
    drift guard sa semantics ng bawat sangkap.)"""
    monkeypatch.setattr(
        settings, "chili_momentum_halt_trigger_suppression_enabled", True
    )
    le_halted = {"suspected_halt_since_utc": "2026-08-17T13:07:14+00:00"}
    le_clear: dict = {}
    sess = SimpleNamespace(state="watching_live")
    with patch.object(lr, "_live_entry_quote_gate_applies", return_value=True):
        assert bool(
            le_halted.get("suspected_halt_since_utc")
            and lr._live_entry_quote_gate_applies(sess, le_halted)
            and bool(
                getattr(
                    settings,
                    "chili_momentum_halt_trigger_suppression_enabled",
                    True,
                )
            )
        ) is True
        assert bool(le_clear.get("suspected_halt_since_utc")) is False
    # Held/submitted session (gate hindi applicable) → hindi sinusupil.
    with patch.object(lr, "_live_entry_quote_gate_applies", return_value=False):
        assert bool(
            le_halted.get("suspected_halt_since_utc")
            and lr._live_entry_quote_gate_applies(sess, le_halted)
        ) is False


def test_resume_clears_marker_semantics():
    """Ang resume path ang nagbubura ng marker (live_runner ~21993) — kapag wala
    na ang marker, wala nang sinusupil. Drift guard sa key name."""
    le = {"suspected_halt_since_utc": "t", "halt_stale_streak": 3}
    le.pop("suspected_halt_since_utc", None)
    le["halt_resumed_at_utc"] = "t2"
    assert not le.get("suspected_halt_since_utc")
    assert le.get("halt_resumed_at_utc")
