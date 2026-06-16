"""Ross gap #4: sympathy/theme cluster (videos 06/09/12). The day's movers cluster by SIC
sector; a sector whose LEADER is a big % gainer drags its peers (the "hot potato" sympathy
run that produces STI/ASTC-class moves). A sympathy peer — same sector as a strong leader,
itself in play but less extended — gets an additive viability tilt; the leader is excluded
(already ranked on its own move). Pure-logic tests (the SIC sector fetch is network).
"""
from __future__ import annotations

from app.services.trading.momentum_neural.catalyst import (
    _catalyst_tilt,
    sympathy_peer_symbols,
    sympathy_viability_delta,
)


# ── sympathy_peer_symbols ────────────────────────────────────────────────────

def test_strong_cluster_tilts_peers_not_leader():
    movers = {"LEAD": 40.0, "PEER1": 12.0, "PEER2": 8.0, "OTHER": 30.0}
    sectors = {"LEAD": "Biotech", "PEER1": "Biotech", "PEER2": "Biotech", "OTHER": "Energy"}
    # Biotech cluster (3 members, leader +40%) -> the two non-leaders are sympathy peers;
    # Energy has only OTHER alone -> no cluster.
    assert sympathy_peer_symbols(movers, sectors) == {"PEER1", "PEER2"}


def test_no_strong_leader_no_peers():
    movers = {"A": 8.0, "B": 6.0}   # leader +8% is below the floor -> not a hot cluster
    sectors = {"A": "Biotech", "B": "Biotech"}
    assert sympathy_peer_symbols(movers, sectors) == set()


def test_singleton_sectors_no_cluster():
    movers = {"A": 40.0, "B": 35.0}
    sectors = {"A": "Biotech", "B": "Energy"}   # each alone in its sector
    assert sympathy_peer_symbols(movers, sectors) == set()


def test_missing_sector_is_skipped():
    movers = {"A": 40.0, "B": 20.0}
    sectors = {"A": "Biotech", "B": None}   # B has no SIC sector -> not clustered
    assert sympathy_peer_symbols(movers, sectors) == set()   # A alone in Biotech -> no cluster


def test_empty_inputs():
    assert sympathy_peer_symbols({}, {}) == set()
    assert sympathy_peer_symbols({"A": 40.0}, {}) == set()


# ── sympathy_viability_delta ─────────────────────────────────────────────────

def test_sympathy_delta_peer_vs_non_peer():
    half = _catalyst_tilt() * 0.5
    assert sympathy_viability_delta("PEER1", {"PEER1"}) == half
    assert sympathy_viability_delta("PEER1", {"OTHER"}) == 0.0
    assert sympathy_viability_delta("PEER1", None) == 0.0


def test_sympathy_delta_crypto_zero():
    assert sympathy_viability_delta("BTC-USD", {"BTC"}) == 0.0
