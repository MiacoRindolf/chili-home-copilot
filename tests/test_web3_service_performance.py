from __future__ import annotations

from collections import OrderedDict

from app.services.trading import web3_service


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("token-cache pruning should not snapshot all keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("token-cache pruning should walk oldest entries directly")


def test_token_cache_get_removes_stale_entry() -> None:
    original_ttl = web3_service._TOKEN_CACHE_TTL
    web3_service._TOKEN_CACHE_TTL = 10
    web3_service._token_cache.clear()
    try:
        web3_service._token_cache[1] = (900.0, [{"symbol": "OLD"}])

        assert web3_service._token_cache_get(1, now=1_000.0) is None
        assert 1 not in web3_service._token_cache
    finally:
        web3_service._TOKEN_CACHE_TTL = original_ttl
        web3_service._token_cache.clear()


def test_token_cache_set_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(web3_service, "_TOKEN_CACHE_TTL", 10)
    monkeypatch.setattr(web3_service, "_TOKEN_CACHE_MAX", 5)
    cache = _NoSnapshotOrderedDict(
        [
            (100, (900.0, [{"symbol": "OLD1"}])),
            (101, (901.0, [{"symbol": "OLD2"}])),
            (1, (995.0, [{"symbol": "ETH"}])),
        ]
    )
    monkeypatch.setattr(web3_service, "_token_cache", cache)

    web3_service._token_cache_set(137, [{"symbol": "POL"}], now=1_000.0)

    assert list(web3_service._token_cache) == [1, 137]


def test_token_cache_set_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(web3_service, "_TOKEN_CACHE_TTL", 100)
    monkeypatch.setattr(web3_service, "_TOKEN_CACHE_MAX", 3)
    web3_service._token_cache.clear()
    try:
        for idx, chain_id in enumerate([1, 137, 56]):
            web3_service._token_cache[chain_id] = (990.0 + idx, [{"symbol": str(chain_id)}])

        web3_service._token_cache_set(42161, [{"symbol": "ARB"}], now=1_000.0)

        assert list(web3_service._token_cache) == [137, 56, 42161]
    finally:
        web3_service._token_cache.clear()


def test_token_cache_get_moves_hit_to_newest() -> None:
    web3_service._token_cache.clear()
    try:
        web3_service._token_cache[1] = (1_000.0, [{"symbol": "ETH"}])
        web3_service._token_cache[137] = (1_000.0, [{"symbol": "POL"}])

        assert web3_service._token_cache_get(1, now=1_001.0) == [{"symbol": "ETH"}]
        assert list(web3_service._token_cache) == [137, 1]
    finally:
        web3_service._token_cache.clear()


def test_get_token_list_uses_bounded_cache_for_unsupported_chains(monkeypatch) -> None:
    monkeypatch.setattr(web3_service, "_TOKEN_CACHE_MAX", 3)
    monkeypatch.setattr(web3_service.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(web3_service, "_fetch_zerox_tokens", lambda _chain_id: [])
    web3_service._token_cache.clear()
    try:
        for chain_id in [10_000, 10_001, 10_002, 10_003]:
            assert web3_service.get_token_list(chain_id) == []

        assert list(web3_service._token_cache) == [10_001, 10_002, 10_003]
    finally:
        web3_service._token_cache.clear()
