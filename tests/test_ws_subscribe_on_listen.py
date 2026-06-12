"""WS subscribe-on-listen (2026-06-12 IPO morning stale_bbo incident).

A tick listener without a WS subscription never fires: newly armed equities
sat behind stale_bbo forever (RYAM's freshest quote was 43 min old) because
only boot-time symbols were subscribed.
"""

from app.services import massive_client as mc


class _FakeWs:
    def __init__(self):
        self.subscribed = []
        self._thread = object()  # truthy = running

    def subscribe(self, tickers):
        self.subscribed.extend(tickers)


def test_register_tick_listener_subscribes_running_ws(monkeypatch):
    fake = _FakeWs()
    monkeypatch.setattr(mc, "_ws_client", fake)
    cb = lambda sym, snap: None
    mc.register_tick_listener("ryam", cb)
    try:
        assert fake.subscribed == ["RYAM"]
    finally:
        mc.unregister_tick_listener("ryam", cb)


def test_register_tick_listener_safe_without_ws(monkeypatch):
    monkeypatch.setattr(mc, "_ws_client", None)
    cb = lambda sym, snap: None
    mc.register_tick_listener("bbd", cb)  # must not raise
    mc.unregister_tick_listener("bbd", cb)
