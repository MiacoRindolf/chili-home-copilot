"""WS watchdog — auto-reconnect a silently-dead Coinbase L2 feed (2026-06-16).

The Coinbase SDK swallows a network socket-flap drop without firing on_close (its
internal retry exhausts after ~19min and clears subscriptions), so the L2 feed dies
silently until a manual restart — proven live (a 65-min fast_orderbook gap, zero
reconnect log lines). The drain job polls watchdog_check(): when no l2 message has
arrived for `stale_s`, it force-reconnects (rate-limited), re-subscribing the saved
products.
"""

import json
import time

from app.services.trading.venue import coinbase_spot as cs


def _seam(monkeypatch, *, enabled=True, stale_s=45.0, min_interval=30.0):
    monkeypatch.setattr(cs.settings, "chili_coinbase_ws_watchdog_enabled", enabled, raising=False)
    monkeypatch.setattr(cs.settings, "chili_coinbase_ws_watchdog_stale_s", stale_s, raising=False)
    monkeypatch.setattr(cs.settings, "chili_coinbase_ws_watchdog_min_reconnect_interval_s", min_interval, raising=False)
    s = cs.CoinbaseWebSocketSeam()
    s.enabled = True
    s._running = True
    s._subscribed = {"BTC-USD", "ETH-USD"}
    calls = {"stop": 0, "start": []}
    s.stop = lambda: calls.__setitem__("stop", calls["stop"] + 1)
    s.start = lambda product_ids=None: calls["start"].append(product_ids)
    return s, calls


def test_fresh_feed_no_reconnect(monkeypatch):
    s, calls = _seam(monkeypatch)
    s._last_l2_monotonic = time.monotonic()       # just received a message
    r = s.watchdog_check()
    assert r["stale"] is False
    assert calls["stop"] == 0 and calls["start"] == []


def test_stale_feed_force_reconnects_saved_products(monkeypatch):
    s, calls = _seam(monkeypatch)
    s._last_l2_monotonic = time.monotonic() - 120  # 120s > 45 stale threshold
    r = s.watchdog_check()
    assert r["reconnect"] == "done"
    assert calls["stop"] == 1
    assert calls["start"] == [["BTC-USD", "ETH-USD"]]   # saved set re-subscribed (sorted)


def test_disabled_kill_switch_no_check(monkeypatch):
    s, calls = _seam(monkeypatch, enabled=False)
    s._last_l2_monotonic = time.monotonic() - 120
    r = s.watchdog_check()
    assert r["checked"] is False
    assert calls["stop"] == 0


def test_rate_limited_no_storm(monkeypatch):
    s, calls = _seam(monkeypatch)
    s._last_l2_monotonic = time.monotonic() - 120          # stale
    s._last_watchdog_reconnect_monotonic = time.monotonic()  # but just reconnected
    r = s.watchdog_check()
    assert r["reconnect"] == "rate_limited"
    assert calls["stop"] == 0                               # anti-storm: no reconnect


def test_no_data_yet_no_reconnect(monkeypatch):
    s, calls = _seam(monkeypatch)
    s._last_l2_monotonic = None                            # never received a message
    r = s.watchdog_check()
    assert r["stale"] is False and r.get("reason") == "no_data_yet"
    assert calls["stop"] == 0


def test_not_running_no_check(monkeypatch):
    s, calls = _seam(monkeypatch)
    s._running = False
    s._last_l2_monotonic = time.monotonic() - 120
    r = s.watchdog_check()
    assert r["checked"] is False and r.get("reason") == "not_running"


def test_heartbeat_stamped_on_l2_message(monkeypatch):
    s, _calls = _seam(monkeypatch)
    s._last_l2_monotonic = None
    s._handle_l2 = lambda events, ts=None: None            # isolate the stamp
    s._on_message(json.dumps({"channel": "l2_data", "events": [], "timestamp": "x"}))
    assert s._last_l2_monotonic is not None                # liveness heartbeat recorded
