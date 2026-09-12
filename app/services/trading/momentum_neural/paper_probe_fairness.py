"""Fair operational service of the complete PAPER candidate set.

Service ordinals are scheduling state, never a market indicator or ranking.
Queued/cancelled work does not count as observed. In-flight work retains its
slot across caller deadlines, preventing a new pool from exceeding capacity.
Late results belong to their original pass and are not traded by a later pass.
"""
from __future__ import annotations

import threading


class ProbeCapacityDeferred(RuntimeError):
    pass


class PaperProbeService:
    def __init__(self):
        self._lock = threading.Lock()
        self._ordinal = 0
        self._started = {}
        self._inflight = set()

    def order(self, candidates):
        with self._lock:
            return sorted(candidates, key=lambda c: (self._started.get(c.symbol, 0), c.symbol))

    def call(self, symbol, *, capacity, probe):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('positive_probe_resource_capacity_required')
        with self._lock:
            if symbol in self._inflight or len(self._inflight) >= capacity:
                raise ProbeCapacityDeferred('paper_probe_capacity_or_symbol_busy')
            self._inflight.add(symbol)
            self._ordinal += 1
            self._started[symbol] = self._ordinal
        try:
            return probe()
        finally:
            with self._lock:
                self._inflight.remove(symbol)


PAPER_PROBE_SERVICE = PaperProbeService()
