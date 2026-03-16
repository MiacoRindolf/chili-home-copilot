"""Top Picks: generation, ranking, smart pick, freshness tracking.

This module re-exports top picks functions from scanner.py to establish a
cleaner module boundary. Functions will be migrated here incrementally.
"""
from __future__ import annotations

# Re-export top picks functions from scanner for clean import paths.
from .scanner import (
    generate_top_picks,
    get_top_picks_freshness,
    recheck_pick,
    smart_pick,
    smart_pick_context,
)

__all__ = [
    "generate_top_picks",
    "get_top_picks_freshness",
    "recheck_pick",
    "smart_pick",
    "smart_pick_context",
]
