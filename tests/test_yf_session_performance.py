from __future__ import annotations

from app.services import yf_session


def test_cache_set_prunes_fresh_overflow_in_batches(monkeypatch) -> None:
    monkeypatch.setattr(yf_session, "_MAX_CACHE_SIZE", 10)
    monkeypatch.setattr(yf_session.time, "time", lambda: 1_000.0)
    with yf_session._cache_lock:
        yf_session._cache.clear()
        for idx in range(12):
            yf_session._cache[f"old-{idx}"] = (900.0 + idx, idx)
    try:
        yf_session._cache_set("new-key", "new-value")

        with yf_session._cache_lock:
            assert len(yf_session._cache) <= 10
            assert "new-key" in yf_session._cache
            assert "old-0" not in yf_session._cache
            assert "old-1" not in yf_session._cache
    finally:
        with yf_session._cache_lock:
            yf_session._cache.clear()
