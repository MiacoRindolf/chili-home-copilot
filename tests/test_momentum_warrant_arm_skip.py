"""ITEM A — conservative warrant / non-common-stock ARM-SKIP (live_error noise fix, 2026-06-29).

67/72 of the momentum lane's recurring live_error were PRE-entry no_bbo on thin non-common-
stock names — WARRANTS especially (the "W" 5th-letter class suffix, e.g. RVMDW, appeared 5×).
The arm-skip drops structurally-non-common names so the slot goes to a FILLABLE common mover.

⚠️⚠️ THE OVER-VETO GUARD is the load-bearing property: the filter must NEVER skip a legitimate
THIN-BUT-QUOTED COMMON premarket mover (the UPC-class +500% low-float names). It skips ONLY
truly-non-common (warrant/right/unit) tickers; a wide spread / low float is NOT a skip reason;
anything uncertain FAILS OPEN (does not skip).
"""
from __future__ import annotations

import app.services.trading.momentum_neural.auto_arm as aa
from app.config import settings


# ── Warrant / right / unit tickers ARE matched (skipped) ─────────────────────────────


def test_warrant_class_tickers_are_matched():
    # The dominant observed case: the 5-letter all-alpha NASDAQ "W" 5th-letter warrant class.
    assert aa._looks_like_non_common_stock("RVMDW") is True   # the literal noise driver
    assert aa._looks_like_non_common_stock("GXAIW") is True
    # Explicit punctuation class suffixes (warrant / unit / right).
    for s in ("ABCD.WS", "XYZ-WT", "FOO.W", "BAR-W", "BAZ.U", "QUX-U", "ZIP.R", "ZAP-R"):
        assert aa._looks_like_non_common_stock(s) is True, s
    # The ``=`` warrant marker some feeds use.
    assert aa._looks_like_non_common_stock("RVMD=") is True


# ── ⚠️ OVER-VETO GUARD: thin-but-quoted COMMON premarket movers are NOT matched ──────


def test_thin_common_premarket_movers_are_not_matched():
    # The UPC-class low-float +500% premarket movers MUST still arm — normal 1–4 letter roots.
    for s in ("UPC", "NXTS", "FCUV", "ILLR", "SMCX", "BATL", "KAIO", "CRVO", "SPCX"):
        assert aa._looks_like_non_common_stock(s) is False, s
    # Ordinary commons, incl. 4-letter and legit 5-letter roots (GOOGL), and a 4-letter
    # root that happens to end in W (SNDW) — only a *5*-letter all-alpha W-root is a warrant.
    for s in ("AAPL", "TSLA", "AMC", "GOOGL", "CSCO", "SNDW", "W", "SW"):
        assert aa._looks_like_non_common_stock(s) is False, s


def test_crypto_pairs_are_exempt():
    for s in ("BTC-USD", "ETH-USD", "DOGE-USD", "SOL-USDC"):
        assert aa._looks_like_non_common_stock(s) is False, s


def test_uncertain_or_empty_fails_open():
    # Empty / unparseable => do NOT skip (fail-open).
    assert aa._looks_like_non_common_stock("") is False
    assert aa._looks_like_non_common_stock(None) is False
    assert aa._looks_like_non_common_stock("   ") is False


# ── Flag gating (no-dark-flags: default ON; OFF == byte-identical no-skip) ────────────


def test_arm_skip_default_on_skips_warrant(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_asset_type_arm_skip_enabled", True)
    assert aa._asset_type_blocks_arm("RVMDW") is True
    assert aa._asset_type_blocks_arm("UPC") is False  # common mover still arms


def test_flag_off_is_byte_identical(monkeypatch):
    # Flag OFF => the skip NEVER fires, even for an unambiguous warrant.
    monkeypatch.setattr(settings, "chili_momentum_asset_type_arm_skip_enabled", False)
    assert aa._asset_type_blocks_arm("RVMDW") is False
    assert aa._asset_type_blocks_arm("GXAIW") is False


def test_blocks_arm_fails_open_on_error(monkeypatch):
    # If the matcher raises, the gate fails OPEN (lets the name arm) — never starves the lane.
    monkeypatch.setattr(settings, "chili_momentum_asset_type_arm_skip_enabled", True)
    def _boom(_sym):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(aa, "_looks_like_non_common_stock", _boom)
    assert aa._asset_type_blocks_arm("RVMDW") is False
