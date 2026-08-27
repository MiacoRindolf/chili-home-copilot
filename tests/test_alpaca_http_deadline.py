"""Bawat alpaca HTTP call ay may deadline na (2026-08-27, lock storm C-c1).

ANG UGAT: ang alpaca-py RESTClient ay WALANG timeout parameter; ang requests
na walang timeout ay naghihintay nang walang hanggan sa nakabiting koneksyon.
Isang straggler tick_live_session ang nahuli nang may hawak na session row
lock nang minuto (648s historical max) habang ang HELD-position exit
management ay nakatira sa tick na iyon — BDRX-class na panganib.

Runnable: pytest tests/test_alpaca_http_deadline.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

from app.config import settings
from app.services.trading.venue import alpaca_spot as AS


class _FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=200)


def _client_with_session():
    sess = _FakeSession()
    return SimpleNamespace(_session=sess), sess


def test_the_wrap_injects_a_bounded_timeout():
    """ANG PANGUNAHING KASO: walang timeout sa call ⇒ may (connect, read) na."""
    client, sess = _client_with_session()
    AS._bound_client_http_deadline(client)
    client._session.request("GET", "https://paper-api.alpaca.markets/v2/clock")
    (_, _, kwargs) = sess.calls[0]
    assert kwargs.get("timeout") is not None
    connect, read = kwargs["timeout"]
    assert connect == 5.0
    assert read == float(settings.chili_alpaca_http_timeout_seconds)


def test_an_explicit_timeout_is_never_overridden():
    client, sess = _client_with_session()
    AS._bound_client_http_deadline(client)
    client._session.request("GET", "https://x", timeout=3.0)
    (_, _, kwargs) = sess.calls[0]
    assert kwargs["timeout"] == 3.0


def test_the_wrap_is_idempotent():
    """⚠️ Dobleng balot = dobleng indirection na walang saysay — ang pangalawang
    tawag ay dapat walang gawin."""
    client, sess = _client_with_session()
    AS._bound_client_http_deadline(client)
    wrapped_once = client._session.request
    AS._bound_client_http_deadline(client)
    assert client._session.request is wrapped_once


def test_zero_restores_legacy_unbounded(monkeypatch):
    monkeypatch.setattr(
        AS.settings, "chili_alpaca_http_timeout_seconds", 0.0, raising=False)
    client, sess = _client_with_session()
    AS._bound_client_http_deadline(client)
    client._session.request("GET", "https://x")
    (_, _, kwargs) = sess.calls[0]
    assert kwargs.get("timeout") is None, "0 => walang balot (legacy)"


def test_a_client_without_a_session_is_left_alone():
    AS._bound_client_http_deadline(SimpleNamespace())  # walang _session — walang sabog


def test_all_three_client_builders_apply_the_deadline():
    import inspect

    src = inspect.getsource(AS)
    i_raw = src.index("def _raw_trading_client")
    i_data = src.index("def _data_client")
    i_crypto = src.index("def _crypto_data_client")
    for name, start in (("trading", i_raw), ("data", i_data), ("crypto", i_crypto)):
        region = src[start:start + 1600]
        assert "_bound_client_http_deadline" in region, (
            f"ang {name} client builder ay dapat nagba-balot ng deadline"
        )


def test_the_flag_ships_at_10s_with_the_incident_recorded():
    assert float(settings.chili_alpaca_http_timeout_seconds) == 10.0
    desc = str(type(settings).model_fields[
        "chili_alpaca_http_timeout_seconds"].description or "")
    assert "648s" in desc and "2026-08-27" in desc
