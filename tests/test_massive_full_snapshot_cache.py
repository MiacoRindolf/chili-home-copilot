"""Freshness invariants for the shared Massive full-market snapshot cache."""

from __future__ import annotations

import time

import pytest

from app.services import massive_client


def test_full_snapshot_cache_rejects_future_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(
        massive_client,
        "_snapshot_cache",
        (time.time() + 300.0, [{"ticker": "FUTURE_STALE"}]),
    )
    calls = []

    def _fresh_response(url, params):
        calls.append((url, params))
        return {"tickers": [{"ticker": "FRESH"}]}

    monkeypatch.setattr(massive_client, "_get", _fresh_response)

    rows = massive_client.get_full_market_snapshot(max_age_seconds=300.0)

    assert rows == [{"ticker": "FRESH"}]
    assert len(calls) == 1


def test_full_snapshot_capture_sink_observes_exact_cached_read_and_resets(
    monkeypatch,
) -> None:
    rows = [{"ticker": "VEEE", "updated": 1_784_070_000_000_000_000}]
    monkeypatch.setattr(
        massive_client,
        "_snapshot_cache",
        (time.time() - 2.0, rows),
    )
    monkeypatch.setattr(
        massive_client,
        "_get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("warm snapshot unexpectedly reached the network")
        ),
    )

    class Sink:
        def __init__(self, accepted: bool) -> None:
            self.accepted = accepted
            self.calls = []

        def on_massive_full_snapshot(self, **kwargs):
            self.calls.append(kwargs)
            return self.accepted

    accepted = Sink(True)
    with massive_client.massive_full_snapshot_capture_sink(accepted):
        assert massive_client.get_full_market_snapshot(
            include_otc=False,
            max_age_seconds=300.0,
        ) is rows

    assert len(accepted.calls) == 1
    call = accepted.calls[0]
    assert call["rows"] is rows
    assert call["cache_hit"] is True
    assert 0.0 <= call["cache_age_seconds"] < 300.0
    assert call["provider_cache_ttl_seconds"] == 300.0
    assert call["requested_at"].tzinfo is not None
    assert call["returned_at"] >= call["requested_at"]

    rejected = Sink(False)
    with massive_client.massive_full_snapshot_capture_sink(rejected):
        with pytest.raises(
            massive_client.MassiveFullSnapshotCaptureError,
            match="did not durably accept",
        ):
            massive_client.get_full_market_snapshot(max_age_seconds=300.0)

    # ContextVar reset: the ordinary cached read remains byte-for-byte data
    # compatible and does not reuse either capture sink.
    assert massive_client.get_full_market_snapshot(max_age_seconds=300.0) is rows
    assert len(rejected.calls) == 1
