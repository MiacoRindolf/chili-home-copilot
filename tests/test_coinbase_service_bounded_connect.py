"""[64] (2026-09-11): every Coinbase REST call is BOUNDED — with the bound that fits it.

Ang insidente: ang ws-ignition_1 (ang ignition→arm bridge) ay naka-``ssl.read``
nang 13,744 s sa loob ng ``coinbase_service.connect()`` → ``get_accounts`` —
isang keep-alive socket papunta sa api.coinbase.com na binuksan 03:04 PT at
ginamit ulit 04:04:56 PT (half-open) ang tumanggap ng request at hindi na
sumagot. Ang RESTClient ay ginawa nang WALANG ``timeout`` (SDK default None =
walang hangganan).

Review fix (DALAWANG CLIENT, DALAWANG HANGGANAN):
  - PROBE client (connect / is_connected / can_trade) = ONE arm cadence
    (``chili_momentum_auto_arm_live_scheduler_interval_seconds``; lane 10 s).
  - TRADING client (orders / cancels / reads — the CoinbaseSpotAdapter) = the SLOWEST
    recorded order ack (``trading_order_state_log`` coinbase, n=2,879, max 11.849 s).
    The first revision bound order POSTs by the arm cadence too, which would cut 2 of
    the 2,879 recorded acks (a POST Coinbase accepted, answered after 10 s = an
    unbooked fill).
  - connect() probes on EVERY call (the TTL cache hid a failure for up to 600 s).

Ang mga huling test ay gumagamit ng TUNAY na RESTClient laban sa lokal na server —
kasama ang eksaktong klase ng insidente (ang IKALAWANG request sa PAREHONG keep-alive
TLS socket na hindi na sinasagot) at ang order POST na mabagal pero MALUSOG.

Runnable: pytest tests/test_coinbase_service_bounded_connect.py -v
"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
import os
import socket
import ssl
import threading
import time
import warnings

import pytest
import requests

from app.services import broker_manager
from app.services import coinbase_service as cbs

_BINDING = "chili_momentum_auto_arm_live_scheduler_interval_seconds"
_ORDER_BINDING = "coinbase_order_ack_span_max_s"
_FAKE_KEY = "organizations/test-org/apiKeys/test-key"


def _fake_ed25519_secret() -> str:
    # A syntactically valid Ed25519 raw key (32 bytes, base64) so the SDK can sign
    # its JWT. Random per test; never a real credential.
    return base64.b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture
def fresh_state(monkeypatch):
    """Isolate the module-level connection state for each test."""
    monkeypatch.setattr(cbs, "_cb_available", True)
    monkeypatch.setattr(cbs, "_client", None)
    monkeypatch.setattr(cbs, "_probe_client", None)
    monkeypatch.setattr(cbs, "_client_source", "")
    monkeypatch.setattr(cbs, "_connected", False)
    monkeypatch.setattr(cbs, "_last_check", 0.0)
    cbs._can_trade_cache.update({"value": None, "ts": 0.0})
    yield monkeypatch
    cbs._can_trade_cache.update({"value": None, "ts": 0.0})


class _CountingClient:
    def __init__(self, *, raises: BaseException | None = None, timeout=None):
        self.calls: list[str] = []
        self._raises = raises
        self.timeout = timeout

    def get_accounts(self, limit=None):
        self.calls.append("get_accounts")
        if self._raises is not None:
            raise self._raises
        return {"accounts": []}

    def get_api_key_permissions(self):
        self.calls.append("get_api_key_permissions")
        if self._raises is not None:
            raise self._raises
        return {"can_view": True, "can_trade": True}


# ───────────────────────── construction sites: two clients, two bounds ─────────────────────────


def _record_constructions(monkeypatch) -> list[dict]:
    built: list[dict] = []

    class _FakeCB:
        def __init__(self, **kwargs):
            built.append(dict(kwargs))
            self.timeout = kwargs.get("timeout")

        def get_accounts(self, limit=None):
            return {"accounts": []}

    import coinbase.rest as _cb_rest

    monkeypatch.setattr(_cb_rest, "RESTClient", _FakeCB)
    return built


def test_trading_sites_use_order_ack_bound_and_probe_sites_use_cadence(fresh_state):
    """Trading constructions (_get_client, _get_env_client, connect_with_credentials'
    trading client) carry the ORDER-ack bound; probe constructions carry the cadence."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    monkeypatch.setattr(cbs.settings, "coinbase_api_key", "k-test", raising=False)
    monkeypatch.setattr(cbs.settings, "coinbase_api_secret", "s-test", raising=False)
    built = _record_constructions(monkeypatch)

    assert cbs._get_client() is not None  # trading
    monkeypatch.setattr(cbs, "_client", None)
    assert cbs._get_env_client() is not None  # trading
    assert cbs._get_probe_client() is not None  # probe
    monkeypatch.setattr(cbs, "_probe_client", None)
    assert cbs.connect_with_credentials("k2-test", "s2-test")["status"] == "connected"  # probe + trading

    timeouts = [kw.get("timeout") for kw in built]
    assert timeouts == [11.849, 11.849, 10.0, 10.0, 11.849], timeouts
    assert cbs._client.timeout == 11.849 and cbs._probe_client.timeout == 10.0
    assert cbs.rest_timeout_receipt() == {
        "probe_timeout_s": 10.0,
        "probe_timeout_binding": _BINDING,
        "order_timeout_s": 11.849,
        "order_timeout_binding": _ORDER_BINDING,
        "order_timeout_derivation": cbs._ORDER_TIMEOUT_DERIVATION,
    }


def test_order_bound_is_not_coupled_to_the_arm_cadence(fresh_state):
    """Finding: an operator raising the arm cadence to 300 s must NOT let an exit POST
    wait 300 s. The probe bound follows the cadence; the trading bound does not."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, "coinbase_api_key", "k-test", raising=False)
    monkeypatch.setattr(cbs.settings, "coinbase_api_secret", "s-test", raising=False)
    built = _record_constructions(monkeypatch)
    monkeypatch.setattr(cbs.settings, _BINDING, 300, raising=False)
    cbs._get_client()
    cbs._get_probe_client()
    assert [kw["timeout"] for kw in built] == [11.849, 300.0]
    # ...and the order bound is the recorded max, >= every recorded ack span
    # (the arm-cadence lane bound of 10 s would have cut 2 of the 2,879).
    assert cbs._order_timeout_seconds() == cbs._COINBASE_ORDER_ACK_SPAN_MAX_S == 11.849
    assert "n=2879" in cbs._ORDER_TIMEOUT_DERIVATION and "max 11.849" in cbs._ORDER_TIMEOUT_DERIVATION


def test_probe_timeout_falls_back_to_field_default_not_a_literal(fresh_state):
    """An unreadable / non-positive setting uses the config FIELD default (30)."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 0, raising=False)
    field_default = float(type(cbs.settings).model_fields[_BINDING].default)
    assert cbs._probe_timeout_seconds() == field_default == 30.0


def test_session_request_receives_each_clients_bound(fresh_state):
    """REAL RESTClients pass their OWN bound to requests.Session.request(timeout=)."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    probe = cbs._new_probe_client(_FAKE_KEY, _fake_ed25519_secret())
    trading = cbs._new_trading_client(_FAKE_KEY, _fake_ed25519_secret())
    assert probe.session is not trading.session  # separate keep-alive pools
    seen: list[tuple[str, float]] = []

    def _fake_request_for(tag):
        def _fake_request(method, url, **kwargs):
            seen.append((tag, kwargs["timeout"]))
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b'{"accounts": []}'
            return resp

        return _fake_request

    monkeypatch.setattr(probe.session, "request", _fake_request_for("probe"))
    monkeypatch.setattr(trading.session, "request", _fake_request_for("trading"))
    probe.get_accounts(limit=1)
    trading.get_accounts(limit=1)
    assert seen == [("probe", 10.0), ("trading", 11.849)], seen


# ───────────────────────── connect(): probe client, every call ─────────────────────────


def test_connect_uses_probe_client_never_the_trading_client(fresh_state):
    monkeypatch = fresh_state
    probe, trading = _CountingClient(timeout=10.0), _CountingClient(timeout=11.849)
    monkeypatch.setattr(cbs, "_probe_client", probe)
    monkeypatch.setattr(cbs, "_client", trading)
    assert cbs.connect()["status"] == "connected"
    assert probe.calls == ["get_accounts"] and trading.calls == []


def test_connect_probes_every_call_so_a_failure_is_seen_at_once(fresh_state):
    """Finding: the TTL cache made a revocation/outage invisible for up to 600 s.
    connect() now probes every call; the failure clears ``connected`` immediately, so
    the readiness filter drops coinbase_spot candidates on the SAME pass."""
    monkeypatch = fresh_state
    fake = _CountingClient(timeout=10.0)
    monkeypatch.setattr(cbs, "_probe_client", fake)
    assert cbs.connect()["status"] == "connected"
    assert cbs.get_connection_status()["connected"] is True
    assert cbs.connect()["status"] == "connected"
    assert fake.calls == ["get_accounts", "get_accounts"], "no cache: every connect probes"
    fake._raises = RuntimeError("401 Unauthorized")  # the key was revoked
    out = cbs.connect()
    assert out["status"] == "error", out
    assert cbs._connected is False
    assert cbs.get_connection_status()["connected"] is False
    assert len(fake.calls) == 3


def test_operator_ui_connect_probes(fresh_state):
    """broker_manager.connect_broker('coinbase') is the operator's explicit verify."""
    monkeypatch = fresh_state
    fake = _CountingClient(timeout=10.0)
    monkeypatch.setattr(cbs, "_probe_client", fake)
    monkeypatch.setattr(cbs, "_connected", True)
    monkeypatch.setattr(cbs, "_last_check", time.time())
    out = broker_manager.connect_broker("coinbase")
    assert out["status"] == "connected"
    assert fake.calls == ["get_accounts"]


def test_can_trade_probes_on_the_probe_client(fresh_state):
    monkeypatch = fresh_state
    probe, trading = _CountingClient(timeout=10.0), _CountingClient(timeout=11.849)
    monkeypatch.setattr(cbs, "_probe_client", probe)
    monkeypatch.setattr(cbs, "_client", trading)
    monkeypatch.setattr(cbs, "_connected", True)
    monkeypatch.setattr(cbs, "_last_check", time.time())
    assert cbs.can_trade() is True
    assert probe.calls == ["get_api_key_permissions"] and trading.calls == []


def test_clear_cache_drops_both_clients(fresh_state):
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs, "_probe_client", _CountingClient())
    monkeypatch.setattr(cbs, "_client", _CountingClient())
    monkeypatch.setattr(cbs, "_client_source", "explicit")
    cbs.clear_cache()
    assert cbs._client is None and cbs._probe_client is None and cbs._client_source == ""


# ───────────────────────── timeout receipts ─────────────────────────


def test_connect_timeout_receipt_reports_the_bound_the_client_carries(fresh_state, caplog):
    """Finding: the receipt must carry the value that DECIDED — the bound fixed on the
    client at construction — not a fresh settings read."""
    monkeypatch = fresh_state
    fake = _CountingClient(raises=requests.exceptions.ReadTimeout("Read timed out."), timeout=10.0)
    monkeypatch.setattr(cbs, "_probe_client", fake)
    monkeypatch.setattr(cbs.settings, _BINDING, 45, raising=False)  # changed AFTER construction
    with caplog.at_level(logging.WARNING, logger=cbs.logger.name):
        out = cbs.connect()
    assert out["status"] == "error"
    assert out.get("timed_out") is True
    assert out.get("probe_timeout_s") == 10.0, out
    assert out.get("probe_timeout_binding") == _BINDING
    assert cbs._connected is False
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "bound=10.000s" in m and f"binding={_BINDING}" in m and "call=get_accounts" in m
        for m in msgs
    ), msgs


def test_connect_success_receipt_carries_the_bound(fresh_state):
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs, "_probe_client", _CountingClient(timeout=10.0))
    out = cbs.connect()
    assert out["status"] == "connected"
    assert out["probe_timeout_s"] == 10.0 and out["probe_timeout_binding"] == _BINDING
    assert "cached" not in out


def test_is_connected_timeout_logs_bound_receipt(fresh_state, caplog):
    monkeypatch = fresh_state
    fake = _CountingClient(raises=requests.exceptions.ReadTimeout("Read timed out."), timeout=10.0)
    monkeypatch.setattr(cbs, "_probe_client", fake)
    monkeypatch.setattr(cbs, "_connected", True)
    monkeypatch.setattr(cbs, "_last_check", time.time() - cbs._CHECK_TTL - 1)
    monkeypatch.setattr(cbs.settings, "coinbase_api_key", "k-test", raising=False)
    monkeypatch.setattr(cbs.settings, "coinbase_api_secret", "s-test", raising=False)
    with caplog.at_level(logging.WARNING, logger=cbs.logger.name):
        assert cbs.is_connected() is False
    assert cbs._connected is False
    assert any(f"binding={_BINDING}" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_connection_status_reports_both_bindings(fresh_state):
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    st = cbs.get_connection_status()
    assert st["probe_timeout_s"] == 10.0 and st["probe_timeout_binding"] == _BINDING
    assert st["order_timeout_s"] == 11.849 and st["order_timeout_binding"] == _ORDER_BINDING


# ───────────────────────── real sockets ─────────────────────────


class _SilentTcpServer:
    """Accepts TCP connections and never sends a byte (TLS handshake read hangs)."""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        self._stop = threading.Event()
        self.sock.settimeout(0.2)
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                self.held.append(conn)
            except (socket.timeout, OSError):
                continue

    def close(self):
        self._stop.set()
        for c in self.held:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


def test_hanging_server_connect_returns_within_bound(fresh_state):
    """Real RESTClient, server accepts and never answers: connect() returns in ~bound."""
    monkeypatch = fresh_state
    bound = 1
    monkeypatch.setattr(cbs.settings, _BINDING, bound, raising=False)
    srv = _SilentTcpServer()
    try:
        client = cbs._new_probe_client(_FAKE_KEY, _fake_ed25519_secret())
        client.base_url = f"127.0.0.1:{srv.port}"
        monkeypatch.setattr(cbs, "_probe_client", client)
        t0 = time.monotonic()
        out = cbs.connect()
        elapsed = time.monotonic() - t0
    finally:
        srv.close()
    assert out["status"] == "error", out
    assert out.get("timed_out") is True, out
    assert out.get("probe_timeout_s") == float(bound), out
    assert elapsed < 3 * bound, elapsed


def _self_signed_cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def _read_http_request(tls) -> bool:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = tls.recv(4096)
        if not chunk:
            return False
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        chunk = tls.recv(4096)
        if not chunk:
            return False
        rest += chunk
    return True


def _http_200(body: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Connection: keep-alive\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


class _AnswerOnceTlsServer:
    """TLS keep-alive server: answers the FIRST request, then reads the second
    request on the SAME connection and never answers it (the 09-11 half-open reuse)."""

    _BODY = b'{"accounts": []}'

    def __init__(self, cert_path: str, key_path: str):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert_path, key_path)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.requests_seen = 0
        self._held: list = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            tls = self.ctx.wrap_socket(conn, server_side=True)
            self._held.append(tls)
            if not _read_http_request(tls):
                return
            self.requests_seen += 1
            tls.sendall(_http_200(self._BODY))
            if _read_http_request(tls):
                self.requests_seen += 1
            # Half-open: the request was received; never answer it.
            self._stop.wait(30)
        except (OSError, ssl.SSLError):
            return

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self._stop.set()
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


def test_half_open_keepalive_reuse_returns_within_bound(fresh_state, tmp_path):
    """The literal incident class: connect #1 succeeds and leaves the socket in the
    keep-alive pool; connect #2 REUSES that socket, the server receives the request
    and never answers. Unbounded, this was 13,744 s. Now it returns within ~bound."""
    monkeypatch = fresh_state
    bound = 1
    monkeypatch.setattr(cbs.settings, _BINDING, bound, raising=False)
    cert_path, key_path = _self_signed_cert(tmp_path)
    srv = _AnswerOnceTlsServer(cert_path, key_path)
    try:
        client = cbs._new_probe_client(_FAKE_KEY, _fake_ed25519_secret())
        client.base_url = f"127.0.0.1:{srv.port}"
        client.session.verify = False  # self-signed test cert
        monkeypatch.setattr(cbs, "_probe_client", client)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            first = cbs.connect()
            assert first["status"] == "connected", first
            t0 = time.monotonic()
            second = cbs.connect()
            elapsed = time.monotonic() - t0
    finally:
        srv.close()
    assert second["status"] == "error", second
    assert second.get("timed_out") is True, second
    assert elapsed < 3 * bound, elapsed
    # Same TCP connection carried both requests = keep-alive reuse, as on 09-11.
    assert srv.connections == 1, srv.connections
    assert srv.requests_seen == 2, srv.requests_seen


class _DelayedTlsServer:
    """TLS server that answers EVERY request with ``body`` after ``delay_s`` — a slow
    but HEALTHY venue (an order ACK that takes longer than one arm cadence)."""

    def __init__(self, cert_path: str, key_path: str, *, delay_s: float, body: bytes):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert_path, key_path)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.delay_s = delay_s
        self.body = body
        self.requests_seen = 0
        self._held: list = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            tls = self.ctx.wrap_socket(conn, server_side=True)
            self._held.append(tls)
            while not self._stop.is_set() and _read_http_request(tls):
                self.requests_seen += 1
                if self._stop.wait(self.delay_s):
                    return
                tls.sendall(_http_200(self.body))
        except (OSError, ssl.SSLError):
            return

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self._stop.set()
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


def test_slow_healthy_order_post_is_not_cut_by_the_probe_bound(fresh_state, tmp_path):
    """Finding (major): the cadence bound also governed live ORDER POSTs, so a POST
    Coinbase accepted but answered after one cadence became a terminal reject and its
    fill went unbooked. Now: the SAME slow answer (longer than the probe bound) is
    CUT on the probe client and WAITED FOR on the trading client, which carries the
    order-ack bound. Scaled down (probe 1 s, order bound 4 s, answer after 1.5 s)."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 1, raising=False)
    monkeypatch.setattr(cbs, "_COINBASE_ORDER_ACK_SPAN_MAX_S", 4.0)
    body = (
        b'{"success": true, "success_response": {"order_id": "oid-slow-1", '
        b'"product_id": "BTC-USD", "side": "SELL", "client_order_id": "cid-slow-1"}, '
        b'"accounts": []}'
    )
    cert_path, key_path = _self_signed_cert(tmp_path)
    srv = _DelayedTlsServer(cert_path, key_path, delay_s=1.5, body=body)
    try:
        trading = cbs._new_trading_client(_FAKE_KEY, _fake_ed25519_secret())
        probe = cbs._new_probe_client(_FAKE_KEY, _fake_ed25519_secret())
        for c in (trading, probe):
            c.base_url = f"127.0.0.1:{srv.port}"
            c.session.verify = False  # self-signed test cert
        assert (trading.timeout, probe.timeout) == (4.0, 1.0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t0 = time.monotonic()
            resp = trading.market_order_sell(
                client_order_id="cid-slow-1", product_id="BTC-USD", base_size="0.001"
            )
            order_elapsed = time.monotonic() - t0
            # Negative control, same server, same delay: the probe bound cuts it.
            with pytest.raises(requests.exceptions.Timeout):
                probe.get_accounts(limit=1)
    finally:
        srv.close()
    d = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
    assert d.get("success") is True, d
    assert (d.get("success_response") or {}).get("order_id") == "oid-slow-1", d
    assert 1.5 <= order_elapsed < 4.0, order_elapsed
