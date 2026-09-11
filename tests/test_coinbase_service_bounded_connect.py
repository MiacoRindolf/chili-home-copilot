"""[64] (2026-09-11): every Coinbase REST call is BOUNDED, and connect() is cached.

Ang insidente: ang ws-ignition_1 (ang ignition→arm bridge) ay naka-``ssl.read``
nang 13,744 s sa loob ng ``coinbase_service.connect()`` → ``get_accounts`` —
isang keep-alive socket papunta sa api.coinbase.com na binuksan 03:04 PT at
ginamit ulit 04:04:56 PT (half-open) ang tumanggap ng request at hindi na
sumagot. Ang RESTClient ay ginawa nang WALANG ``timeout`` (SDK default None =
walang hangganan). Ang bound ngayon = ang arm cadence
(``chili_momentum_auto_arm_live_scheduler_interval_seconds``; lane 10 s).

Ang dalawang huling test ay gumagamit ng TUNAY na RESTClient laban sa isang
lokal na server na hindi sumasagot — kasama ang eksaktong klase ng insidente:
ang IKALAWANG request sa PAREHONG keep-alive TLS socket na hindi na sinasagot.

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
    monkeypatch.setattr(cbs, "_client_source", "")
    monkeypatch.setattr(cbs, "_connected", False)
    monkeypatch.setattr(cbs, "_last_check", 0.0)
    return monkeypatch


class _CountingClient:
    def __init__(self, *, raises: BaseException | None = None):
        self.calls = 0
        self._raises = raises

    def get_accounts(self, limit=None):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {"accounts": []}


# ───────────────────────── construction sites ─────────────────────────


def test_rest_client_constructed_with_cadence_timeout(fresh_state):
    """All 3 construction sites build the client with timeout == the arm cadence."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    monkeypatch.setattr(cbs.settings, "coinbase_api_key", "k-test", raising=False)
    monkeypatch.setattr(cbs.settings, "coinbase_api_secret", "s-test", raising=False)
    built: list[dict] = []

    class _FakeCB:
        def __init__(self, **kwargs):
            built.append(dict(kwargs))

        def get_accounts(self, limit=None):
            return {"accounts": []}

    import coinbase.rest as _cb_rest

    monkeypatch.setattr(_cb_rest, "RESTClient", _FakeCB)

    assert cbs._get_client() is not None
    monkeypatch.setattr(cbs, "_client", None)
    assert cbs._get_env_client() is not None
    assert cbs.connect_with_credentials("k2-test", "s2-test")["status"] == "connected"

    assert len(built) == 3, built
    expected = float(getattr(cbs.settings, _BINDING))
    assert expected == 10.0
    for kw in built:
        assert kw.get("timeout") == expected, kw
    assert cbs.rest_timeout_receipt() == {
        "rest_timeout_s": 10.0,
        "rest_timeout_binding": _BINDING,
    }


def test_rest_timeout_falls_back_to_field_default_not_a_literal(fresh_state):
    """An unreadable / non-positive setting uses the config FIELD default (30)."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 0, raising=False)
    field_default = float(type(cbs.settings).model_fields[_BINDING].default)
    assert cbs._rest_timeout_seconds() == field_default == 30.0


def test_session_request_receives_timeout(fresh_state):
    """A REAL RESTClient passes the bound to requests.Session.request(timeout=)."""
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    client = cbs._new_rest_client(_FAKE_KEY, _fake_ed25519_secret())
    seen: list[dict] = []

    def _fake_request(method, url, **kwargs):
        seen.append(dict(kwargs))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"accounts": []}'
        return resp

    monkeypatch.setattr(client.session, "request", _fake_request)
    client.get_accounts(limit=1)
    assert seen and seen[0]["timeout"] == 10.0, seen


# ───────────────────────── connect() cache ─────────────────────────


def test_connect_is_cached_within_check_ttl(fresh_state):
    monkeypatch = fresh_state
    fake = _CountingClient()
    monkeypatch.setattr(cbs, "_client", fake)

    first = cbs.connect()
    assert first["status"] == "connected" and first.get("cached") is False
    assert fake.calls == 1

    second = cbs.connect()
    assert second["status"] == "connected"
    assert second.get("cached") is True, second
    assert second.get("ttl_s") == cbs._CHECK_TTL
    assert fake.calls == 1, "a fresh connect() must not touch the network"

    forced = cbs.connect(force=True)
    assert forced["status"] == "connected" and forced.get("cached") is False
    assert fake.calls == 2

    # Past the TTL -> re-probe.
    monkeypatch.setattr(cbs, "_last_check", time.time() - cbs._CHECK_TTL - 1)
    cbs.connect()
    assert fake.calls == 3


def test_connect_not_cached_when_last_probe_failed(fresh_state):
    monkeypatch = fresh_state
    fake = _CountingClient(raises=RuntimeError("boom"))
    monkeypatch.setattr(cbs, "_client", fake)
    assert cbs.connect()["status"] == "error"
    assert cbs.connect()["status"] == "error"
    assert fake.calls == 2, "a failed connect must re-probe (never cache a failure)"


def test_operator_ui_connect_forces_a_probe(fresh_state):
    """broker_manager.connect_broker('coinbase') is the operator's explicit verify."""
    monkeypatch = fresh_state
    fake = _CountingClient()
    monkeypatch.setattr(cbs, "_client", fake)
    monkeypatch.setattr(cbs, "_connected", True)
    monkeypatch.setattr(cbs, "_last_check", time.time())
    out = broker_manager.connect_broker("coinbase")
    assert out["status"] == "connected" and out.get("cached") is False
    assert fake.calls == 1


# ───────────────────────── timeout receipts ─────────────────────────


def test_connect_timeout_logs_bound_receipt(fresh_state, caplog):
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    fake = _CountingClient(raises=requests.exceptions.ReadTimeout("Read timed out."))
    monkeypatch.setattr(cbs, "_client", fake)
    with caplog.at_level(logging.WARNING, logger=cbs.logger.name):
        out = cbs.connect()
    assert out["status"] == "error"
    assert out.get("timed_out") is True
    assert out.get("rest_timeout_s") == 10.0
    assert out.get("rest_timeout_binding") == _BINDING
    assert cbs._connected is False
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "bound=10.0s" in m and f"binding={_BINDING}" in m and "call=get_accounts" in m
        for m in msgs
    ), msgs


def test_is_connected_timeout_logs_bound_receipt(fresh_state, caplog):
    monkeypatch = fresh_state
    fake = _CountingClient(raises=requests.exceptions.ReadTimeout("Read timed out."))
    monkeypatch.setattr(cbs, "_client", fake)
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


def test_connection_status_reports_the_binding(fresh_state):
    monkeypatch = fresh_state
    monkeypatch.setattr(cbs.settings, _BINDING, 10, raising=False)
    st = cbs.get_connection_status()
    assert st["rest_timeout_s"] == 10.0
    assert st["rest_timeout_binding"] == _BINDING


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
        client = cbs._new_rest_client(_FAKE_KEY, _fake_ed25519_secret())
        client.base_url = f"127.0.0.1:{srv.port}"
        monkeypatch.setattr(cbs, "_client", client)
        t0 = time.monotonic()
        out = cbs.connect()
        elapsed = time.monotonic() - t0
    finally:
        srv.close()
    assert out["status"] == "error", out
    assert out.get("timed_out") is True, out
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

    @staticmethod
    def _read_request(tls) -> bool:
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

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            tls = self.ctx.wrap_socket(conn, server_side=True)
            self._held.append(tls)
            if not self._read_request(tls):
                return
            self.requests_seen += 1
            tls.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Connection: keep-alive\r\nContent-Length: "
                + str(len(self._BODY)).encode()
                + b"\r\n\r\n"
                + self._BODY
            )
            if self._read_request(tls):
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
        client = cbs._new_rest_client(_FAKE_KEY, _fake_ed25519_secret())
        client.base_url = f"127.0.0.1:{srv.port}"
        client.session.verify = False  # self-signed test cert
        monkeypatch.setattr(cbs, "_client", client)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            first = cbs.connect(force=True)
            assert first["status"] == "connected", first
            t0 = time.monotonic()
            second = cbs.connect(force=True)
            elapsed = time.monotonic() - t0
    finally:
        srv.close()
    assert second["status"] == "error", second
    assert second.get("timed_out") is True, second
    assert elapsed < 3 * bound, elapsed
    # Same TCP connection carried both requests = keep-alive reuse, as on 09-11.
    assert srv.connections == 1, srv.connections
    assert srv.requests_seen == 2, srv.requests_seen
