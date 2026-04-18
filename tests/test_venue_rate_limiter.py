"""Venue order rate limiter — token bucket per venue.

Why this exists
---------------
Robinhood (~60/min) and Coinbase Advanced Trade (~30/s) throttle private
REST endpoints hard. A retry storm from the bracket reconciler — or any
accidental tight loop around ``place_market_order`` — can 429-lock the
account for the rest of the session. The rate limiter is a cheap in-process
guard that fires BEFORE the HTTP call so we never burn the server-side
budget on our own misbehavior.

Headline guarantees verified here
---------------------------------
* Exhausting the bucket returns a structured ``rate_limited`` response
  without calling the underlying venue SDK — so a reconciler treats it as
  backoff, not as "broker said no".
* Two venues are independent — Robinhood exhaustion does not affect
  Coinbase's bucket and vice versa.
* Refill is time-based: after waiting ``1 / rate_per_sec`` seconds at least
  one more token is available.
* ``chili_venue_rate_limit_enabled = False`` is a hard bypass.
* Wiring: the venue adapter short-circuits with ``error == "rate_limited"``
  and does NOT invoke ``market_order_buy`` / ``place_buy_order`` when
  exhausted — the SDK mock's call count stays at 0.
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock

import pytest

from app.services.trading.venue import idempotency_store, rate_limiter
from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter


@pytest.fixture(autouse=True)
def _reset_buckets():
    rate_limiter.reset_for_tests()
    # Also clear the in-RAM idempotency front-cache so fresh client_order_ids
    # inside each test don't collide with cache lines from a neighbor test.
    idempotency_store.reset_for_tests()
    yield
    rate_limiter.reset_for_tests()
    idempotency_store.reset_for_tests()


def _unique_cid(prefix: str) -> str:
    """Generate a client_order_id that can't collide with a prior test run's
    DB-backed idempotency row (rate-limiter tests are adapter-level and share
    the ``venue_order_idempotency`` table with every other test run)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── Core bucket semantics ───────────────────────────────────────────────


def test_try_acquire_respects_burst_and_exhausts(monkeypatch):
    """Allows up to ``burst`` fast calls, then refuses with a positive retry."""
    from app.services.trading.venue import rate_limiter as rl

    # Pin a tiny, predictable config for Coinbase: 3/sec, burst 3.
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 3.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 3, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    # First 3 calls allowed (burst).
    for i in range(3):
        allowed, retry = rl.try_acquire("coinbase")
        assert allowed is True, f"burst call #{i} should be allowed"
        assert retry == 0.0

    # 4th call exhausts — must return retry_after > 0.
    allowed, retry = rl.try_acquire("coinbase")
    assert allowed is False
    assert retry > 0.0
    # At 3 tokens/sec, one token regenerates in ~1/3 sec; allow generous headroom.
    assert retry <= 1.0


def test_refill_after_wait_restores_at_least_one_token(monkeypatch):
    """After waiting long enough for one token, next call is allowed."""
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 10.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 2, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    # Drain bucket.
    assert rl.try_acquire("coinbase")[0] is True
    assert rl.try_acquire("coinbase")[0] is True
    assert rl.try_acquire("coinbase")[0] is False

    # Sleep a hair longer than 1/rate so at least 1 token has regenerated.
    time.sleep(0.15)  # 10/s → 0.1s per token; 0.15 is safe.
    allowed, retry = rl.try_acquire("coinbase")
    assert allowed is True
    assert retry == 0.0


def test_per_venue_buckets_are_independent(monkeypatch):
    """Exhausting Coinbase's bucket does not affect Robinhood's and vice versa."""
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 1.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_rh_orders_per_min", 60.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_rh_burst", 2, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    # Drain Coinbase.
    assert rl.try_acquire("coinbase")[0] is True
    assert rl.try_acquire("coinbase")[0] is False

    # Robinhood still has its full burst.
    assert rl.try_acquire("robinhood")[0] is True
    assert rl.try_acquire("robinhood")[0] is True
    assert rl.try_acquire("robinhood")[0] is False

    # Coinbase is still exhausted — neither bucket leaked.
    assert rl.try_acquire("coinbase")[0] is False


def test_disabled_setting_is_hard_bypass(monkeypatch):
    """Global disable flag skips the bucket entirely — unlimited calls allowed."""
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 1.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", False, raising=False)
    rl.reset_for_tests()

    # Would be exhausted after 1 call if enabled; here all 20 go through.
    for _ in range(20):
        allowed, retry = rl.try_acquire("coinbase")
        assert allowed is True
        assert retry == 0.0


def test_rate_limited_response_shape():
    """Canonical payload — callers rely on these exact keys for backoff logic."""
    resp = rate_limiter.rate_limited_response(
        "coinbase", 0.456, client_order_id="cid-42"
    )
    assert resp["ok"] is False
    assert resp["error"] == "rate_limited"
    assert resp["venue"] == "coinbase"
    assert resp["retry_after_s"] == 0.456
    assert resp["client_order_id"] == "cid-42"

    # Without a cid, the field is simply absent (not empty-string).
    resp2 = rate_limiter.rate_limited_response("robinhood", 1.0)
    assert "client_order_id" not in resp2
    assert resp2["venue"] == "robinhood"


# ── Adapter-level wiring checks ─────────────────────────────────────────


def test_coinbase_adapter_returns_rate_limited_without_hitting_sdk(monkeypatch):
    """Headline wiring test: when the bucket is exhausted, ``place_market_order``
    short-circuits with ``error == rate_limited`` and never calls the SDK.

    This is the property that actually protects the account — a tight loop
    over ``place_market_order`` must NOT reach the HTTP layer.
    """
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    # Use a very slow regen (0.1/sec) and burst=1 so the bucket takes ~10s
    # to recover a token. The place_market_order path hits the DB for
    # idempotency; on a slow runner the two calls can be ~1s apart, which
    # at 1/sec would be enough to regenerate a token. 0.1/sec keeps the
    # test insensitive to that timing.
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 0.1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    mock = MagicMock()
    mock.market_order_buy.return_value = {
        "success": True,
        "success_response": {"order_id": "oid-rl-1"},
    }
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    cid1 = _unique_cid("cb")
    cid2 = _unique_cid("cb")

    # First call consumes the single token.
    r1 = ad.place_market_order(
        product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid1
    )
    assert r1["ok"] is True
    assert mock.market_order_buy.call_count == 1

    # Second call exhausts → rate_limited, no additional SDK call.
    r2 = ad.place_market_order(
        product_id="BTC-USD", side="buy", base_size="0.001", client_order_id=cid2
    )
    assert r2["ok"] is False
    assert r2["error"] == "rate_limited"
    assert r2["client_order_id"] == cid2
    assert r2["retry_after_s"] > 0.0
    assert mock.market_order_buy.call_count == 1  # did NOT increase


def test_robinhood_adapter_returns_rate_limited_without_hitting_broker(monkeypatch):
    """Same wiring guarantee on the RH side — ``place_buy_order`` is not
    reached when the bucket is exhausted."""
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    # 6/min = 0.1/sec — slow enough that DB I/O between calls can't regenerate
    # a token (see sister test for the timing-sensitivity rationale).
    monkeypatch.setattr(settings, "chili_venue_rate_limit_rh_orders_per_min", 6.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_rh_burst", 1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    calls: list[tuple] = []

    def _fake_buy(*a, **kw):
        calls.append(("buy", a, kw))
        return {"ok": True, "order_id": "rh-oid-1", "raw": {}}

    def _fake_sell(*a, **kw):
        calls.append(("sell", a, kw))
        return {"ok": True, "order_id": "rh-oid-2", "raw": {}}

    monkeypatch.setattr(
        "app.services.broker_service.place_buy_order", _fake_buy, raising=False
    )
    monkeypatch.setattr(
        "app.services.broker_service.place_sell_order", _fake_sell, raising=False
    )

    ad = RobinhoodSpotAdapter()
    cid1 = _unique_cid("rh")
    cid2 = _unique_cid("rh")

    # First call consumes the single token → reaches broker_service.
    r1 = ad.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid1
    )
    assert r1["ok"] is True
    assert len(calls) == 1

    # Second call exhausts → rate_limited, broker_service never called again.
    r2 = ad.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid2
    )
    assert r2["ok"] is False
    assert r2["error"] == "rate_limited"
    assert r2["retry_after_s"] > 0.0
    assert len(calls) == 1  # broker path was skipped


def test_coinbase_cancel_order_is_also_rate_limited(monkeypatch):
    """Cancel counts against the same bucket — a retry storm of cancels is
    just as capable of tripping a 429 as a retry storm of placements."""
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 0.1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    mock = MagicMock()
    mock.cancel_orders.return_value = {"results": []}
    ad = CoinbaseSpotAdapter(client_factory=lambda: mock)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: mock  # type: ignore[method-assign]

    # First cancel consumes the token.
    assert ad.cancel_order("ord-1")["ok"] is True
    assert mock.cancel_orders.call_count == 1

    # Second cancel → rate_limited; SDK is NOT called again.
    r2 = ad.cancel_order("ord-2")
    assert r2["ok"] is False
    assert r2["error"] == "rate_limited"
    assert r2["retry_after_s"] > 0.0
    assert mock.cancel_orders.call_count == 1


def test_settings_change_is_picked_up_without_reset(monkeypatch):
    """A monkeypatched setting change takes effect on the next acquire, so
    ops can tune the limiter at runtime without process restart."""
    from app.services.trading.venue import rate_limiter as rl
    from app.config import settings

    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_orders_per_sec", 1.0, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 1, raising=False)
    monkeypatch.setattr(settings, "chili_venue_rate_limit_enabled", True, raising=False)
    rl.reset_for_tests()

    # Drain.
    assert rl.try_acquire("coinbase")[0] is True
    assert rl.try_acquire("coinbase")[0] is False

    # Lower the cap further — still exhausted. Raising burst clamps tokens
    # down (we don't award free tokens), but once tokens regenerate the new
    # capacity applies.
    monkeypatch.setattr(settings, "chili_venue_rate_limit_cb_burst", 10, raising=False)
    snap = rl.peek("coinbase")
    assert snap["capacity"] == 10.0
    # Tokens remain whatever they drifted to under the old config — never
    # negative, never above the new capacity.
    assert 0.0 <= snap["tokens"] <= 10.0
