"""Regression tests for the AGENTIC-TRADABILITY PRE-FILTER (learn-from-401, 2026-06-29).

The Robinhood AGENTIC MCP rail rejects SOME instruments at entry-place with a 401
Unauthorized ("instrument not available for agentic trading on the isolated CASH
account" — CTNT 2026-06-29). The pre-filter LEARNS the untradeable names from the real
place 401s and SKIPS them at ARM so the single slot never loops arm->break->401 on a name
the rail will never fill. Properties under test:

  (a) a recorded 401 symbol is skipped at arm (the throughput win),
  (b) TTL expiry re-admits the symbol (self-healing — tradability can change),
  (c) flag-off => no skip (byte-identical),
  (d) a NON-agentic family (crypto/alpaca/robinhood_spot) is unaffected,
  (e) the bounded store has a hard-max-size eviction,
plus the 401-matcher signature (per-instrument vs whole-rail auth excluded).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import app.services.trading.momentum_neural.auto_arm as aa
from app.config import settings
from app.services.trading.momentum_neural.auto_arm import (
    _AGENTIC_NON_TRADEABLE,
    _AGENTIC_NON_TRADEABLE_MAX,
    _agentic_non_tradeable_active,
    _agentic_tradability_blocks_arm,
    _record_agentic_non_tradeable,
    is_agentic_unauthorized_reject,
)

_T0 = datetime(2026, 6, 29, 21, 0, 0)


def _enable(monkeypatch, ttl: float = 86400.0) -> None:
    monkeypatch.setattr(settings, "chili_momentum_agentic_tradability_prefilter_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_agentic_non_tradeable_ttl_sec", ttl)


def _force_agentic(monkeypatch, agentic: bool = True) -> None:
    """Pin the family-resolution so the test does not depend on live rail config/token."""
    monkeypatch.setattr(aa, "_symbol_routes_to_agentic", lambda sym: agentic)


# ── 401-matcher signature ──────────────────────────────────────────────────────────


def test_matcher_recognizes_per_instrument_401():
    # The real place isError content path (same shape as the observed 409, status 401).
    assert is_agentic_unauthorized_reject(
        "MCP tool 'place_equity_order' returned isError: API error 401: "
        '{"detail":"instrument not available for agentic trading"}'
    ) is True
    assert is_agentic_unauthorized_reject("HTTP 401 unauthorized agentic instrument") is True
    assert is_agentic_unauthorized_reject("instrument is not available for agentic trading") is True


def test_matcher_excludes_whole_rail_auth_and_benign_rejects():
    # A token-revoked / needs-reauth 401 is a WHOLE-RAIL auth failure, not a per-symbol
    # property — must NOT be learned (would falsely starve the entire equity lane).
    assert is_agentic_unauthorized_reject("needs_reauth") is False
    assert is_agentic_unauthorized_reject("grant_revoked") is False
    assert is_agentic_unauthorized_reject(
        "unauthorized — Robinhood Agentic token missing/expired; re-auth via OAuth"
    ) is False
    # Benign / handled-elsewhere rejects.
    assert is_agentic_unauthorized_reject("API error 409: Reference ID must be unique.") is False
    assert is_agentic_unauthorized_reject("EQUITY_SUITABILITY blocked") is False
    assert is_agentic_unauthorized_reject("error 401") is False  # bare 401, no instrument marker
    assert is_agentic_unauthorized_reject("") is False
    assert is_agentic_unauthorized_reject(None) is False


# ── (a) recorded 401 symbol is skipped at arm ───────────────────────────────────────


def test_recorded_401_symbol_is_skipped_at_arm(monkeypatch):
    _enable(monkeypatch)
    _force_agentic(monkeypatch, True)
    _AGENTIC_NON_TRADEABLE.clear()
    _record_agentic_non_tradeable("CTNT", _T0)
    assert _agentic_non_tradeable_active("CTNT", _T0 + timedelta(seconds=60)) is True
    assert _agentic_tradability_blocks_arm("CTNT", _T0 + timedelta(seconds=60)) is True
    # An unrecorded name is never blocked (the pre-filter is an optimization, not a gate).
    assert _agentic_tradability_blocks_arm("AAPL", _T0 + timedelta(seconds=60)) is False


# ── (b) TTL expiry re-admits ─────────────────────────────────────────────────────────


def test_ttl_expiry_readmits_the_symbol(monkeypatch):
    _enable(monkeypatch, ttl=900.0)
    _force_agentic(monkeypatch, True)
    _AGENTIC_NON_TRADEABLE.clear()
    _record_agentic_non_tradeable("CTNT", _T0)
    assert _agentic_tradability_blocks_arm("CTNT", _T0 + timedelta(seconds=899)) is True
    # SELF-HEALING: past the TTL the entry is dropped and the name re-admitted.
    assert _agentic_tradability_blocks_arm("CTNT", _T0 + timedelta(seconds=901)) is False
    assert "CTNT" not in _AGENTIC_NON_TRADEABLE  # expired entry was pruned on read


# ── (c) flag-off => no skip (byte-identical) ─────────────────────────────────────────


def test_flag_off_is_byte_identical(monkeypatch):
    _force_agentic(monkeypatch, True)
    _AGENTIC_NON_TRADEABLE.clear()
    # Flag OFF: recording is a no-op AND the skip never fires.
    monkeypatch.setattr(settings, "chili_momentum_agentic_tradability_prefilter_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_agentic_non_tradeable_ttl_sec", 86400.0)
    _record_agentic_non_tradeable("CTNT", _T0)
    assert "CTNT" not in _AGENTIC_NON_TRADEABLE  # nothing recorded
    assert _agentic_tradability_blocks_arm("CTNT", _T0 + timedelta(seconds=60)) is False


def test_zero_ttl_disables_like_kill_switch(monkeypatch):
    _force_agentic(monkeypatch, True)
    _AGENTIC_NON_TRADEABLE.clear()
    # TTL 0 == instant kill-switch even with the flag on.
    monkeypatch.setattr(settings, "chili_momentum_agentic_tradability_prefilter_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_agentic_non_tradeable_ttl_sec", 0.0)
    _record_agentic_non_tradeable("CTNT", _T0)
    assert "CTNT" not in _AGENTIC_NON_TRADEABLE
    assert _agentic_tradability_blocks_arm("CTNT", _T0 + timedelta(seconds=1)) is False


# ── (d) a non-agentic family is unaffected ───────────────────────────────────────────


def test_non_agentic_family_is_unaffected(monkeypatch):
    _enable(monkeypatch)
    _AGENTIC_NON_TRADEABLE.clear()
    # Even if the name were somehow recorded, a name that routes to a NON-agentic family
    # (crypto/alpaca/robinhood_spot) must never be skipped — those have different tradable
    # universes. The negative-cache membership is still active...
    _force_agentic(monkeypatch, True)
    _record_agentic_non_tradeable("BTC-USD", _T0)
    assert _agentic_non_tradeable_active("BTC-USD", _T0 + timedelta(seconds=60)) is True
    # ...but with the family resolving to NON-agentic, the arm gate does NOT block it.
    _force_agentic(monkeypatch, False)
    assert _agentic_tradability_blocks_arm("BTC-USD", _T0 + timedelta(seconds=60)) is False


def test_real_resolver_scopes_crypto_out(monkeypatch):
    # Exercise the REAL family resolver (not the monkeypatched one): a crypto pair routes to
    # coinbase_spot, so even when recorded it is never blocked by the agentic pre-filter.
    _enable(monkeypatch)
    _AGENTIC_NON_TRADEABLE.clear()
    _record_agentic_non_tradeable("ETH-USD", _T0)
    assert aa._symbol_routes_to_agentic("ETH-USD") is False
    assert _agentic_tradability_blocks_arm("ETH-USD", _T0 + timedelta(seconds=60)) is False


# ── (e) hard-max-size eviction ───────────────────────────────────────────────────────


def test_hard_max_size_eviction(monkeypatch):
    _enable(monkeypatch, ttl=86400.0)
    _AGENTIC_NON_TRADEABLE.clear()
    # Insert well over the cap, all FRESH (within TTL) so the stale-prune cannot reclaim
    # them — the oldest-eviction backstop must still hold the hard ceiling.
    n = _AGENTIC_NON_TRADEABLE_MAX + 50
    for i in range(n):
        _record_agentic_non_tradeable(f"SYM{i:04d}", _T0 + timedelta(seconds=i))
    assert len(_AGENTIC_NON_TRADEABLE) <= _AGENTIC_NON_TRADEABLE_MAX
    # The most-recent insert survives; the oldest were evicted first.
    assert f"SYM{n - 1:04d}" in _AGENTIC_NON_TRADEABLE
    assert "SYM0000" not in _AGENTIC_NON_TRADEABLE


def test_empty_symbol_is_never_recorded_or_blocked(monkeypatch):
    _enable(monkeypatch)
    _force_agentic(monkeypatch, True)
    _AGENTIC_NON_TRADEABLE.clear()
    _record_agentic_non_tradeable("", _T0)
    assert "" not in _AGENTIC_NON_TRADEABLE
    assert _agentic_tradability_blocks_arm("", _T0) is False
    assert _agentic_tradability_blocks_arm(None, _T0) is False
