"""Pytest plugin that makes focused PAPER probes zero-egress by construction.

The plugin is loaded only by the fixed preactivation subprocess commands.  It
blocks socket connection attempts and Alpaca order submissions, records every
attempt in one create-new canonical report, and fails the test session when an
attempt or live-cash credential is observed.
"""

from __future__ import annotations

import hashlib
import functools
import json
import os
from pathlib import Path
import socket
from typing import Any


_REPORT_ENV = "CHILI_CAPTURED_PAPER_SIDE_EFFECT_REPORT"
_COUNTS = {
    "real_network": 0,
    "live_cash": 0,
    "broker_post": 0,
    "fake_transport": 0,
}
_ORIGINALS: dict[str, Any] = {}
_LIVE_KEYS = {
    "CHILI_ALPACA_LIVE_API_KEY",
    "CHILI_ALPACA_LIVE_API_SECRET",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "APCA_API_BASE_URL",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _is_loopback(address: Any) -> bool:
    if isinstance(address, tuple) and address:
        host = str(address[0]).strip().lower()
        return host in {"127.0.0.1", "::1", "localhost"}
    return False


def _guarded_connect(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
    if _is_loopback(address):
        return _ORIGINALS["socket.connect"](sock, address, *args, **kwargs)
    _COUNTS["real_network"] += 1
    raise AssertionError("captured PAPER focused probe forbids real network I/O")


def _guarded_connect_ex(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
    if _is_loopback(address):
        return _ORIGINALS["socket.connect_ex"](sock, address, *args, **kwargs)
    _COUNTS["real_network"] += 1
    raise AssertionError("captured PAPER focused probe forbids real network I/O")


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    if _is_loopback(address):
        return _ORIGINALS["socket.create_connection"](address, *args, **kwargs)
    _COUNTS["real_network"] += 1
    raise AssertionError("captured PAPER focused probe forbids real network I/O")


def _blocked_broker_post(*_args: Any, **_kwargs: Any) -> Any:
    _COUNTS["broker_post"] += 1
    raise AssertionError("captured PAPER focused probe forbids broker POST")


def _wrap_fixed_fake_transport(items: Any) -> None:
    """Count the fixed shard's executable fake broker calls.

    The lifecycle report must be based on an observed call boundary, not a
    shaped constant emitted after pytest exits.  Collection has imported the
    fixed test module, so wrap only its in-memory ``_Broker.post_limit_buy``
    fake.  No application/real-adapter method is touched.
    """

    broker_type = None
    for item in items:
        if not str(getattr(item, "nodeid", "")).startswith(
            "tests/test_captured_paper_transport_coordinator.py::"
        ):
            continue
        candidate = getattr(getattr(item, "module", None), "_Broker", None)
        if candidate is not None:
            broker_type = candidate
            break
    if broker_type is None or "fake_transport.post_limit_buy" in _ORIGINALS:
        return
    original = broker_type.post_limit_buy

    @functools.wraps(original)
    def observed_fake_post(self: Any, *args: Any, **kwargs: Any) -> Any:
        _COUNTS["fake_transport"] += 1
        return original(self, *args, **kwargs)

    _ORIGINALS["fake_transport.post_limit_buy"] = (broker_type, original)
    broker_type.post_limit_buy = observed_fake_post


def pytest_collection_modifyitems(
    session: Any, config: Any, items: Any
) -> None:
    del session, config
    _wrap_fixed_fake_transport(items)


def pytest_configure(config: Any) -> None:
    report = str(os.environ.get(_REPORT_ENV) or "").strip()
    path = Path(report)
    if not path.is_absolute() or path.exists():
        raise RuntimeError("captured PAPER side-effect report path is not fresh/absolute")
    _COUNTS["live_cash"] = sum(
        1 for key in _LIVE_KEYS if str(os.environ.get(key) or "").strip()
    )
    _ORIGINALS["socket.connect"] = socket.socket.connect
    _ORIGINALS["socket.connect_ex"] = socket.socket.connect_ex
    _ORIGINALS["socket.create_connection"] = socket.create_connection
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
    socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
    try:
        from alpaca.trading.client import TradingClient

        _ORIGINALS["alpaca.submit_order"] = TradingClient.submit_order
        TradingClient.submit_order = _blocked_broker_post  # type: ignore[assignment]
    except ImportError:
        # The fixed shard can run without alpaca-py installed, but any eventual
        # network call remains blocked at the socket boundary.
        pass
    config._captured_paper_side_effect_report = path


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    del exitstatus
    path = session.config._captured_paper_side_effect_report
    body = {
        "schema_version": "chili.captured-paper-pytest-side-effect-census.v1",
        "events": [
            {"event_type": name, "count": int(_COUNTS[name])}
            for name in ("fake_transport", "real_network", "live_cash", "broker_post")
        ],
    }
    body["report_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    raw = _canonical(body)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if any(_COUNTS[name] for name in ("real_network", "live_cash", "broker_post")):
        session.exitstatus = 1


def pytest_unconfigure(config: Any) -> None:
    del config
    if "socket.connect" in _ORIGINALS:
        socket.socket.connect = _ORIGINALS["socket.connect"]
        socket.socket.connect_ex = _ORIGINALS["socket.connect_ex"]
        socket.create_connection = _ORIGINALS["socket.create_connection"]
    if "alpaca.submit_order" in _ORIGINALS:
        from alpaca.trading.client import TradingClient

        TradingClient.submit_order = _ORIGINALS["alpaca.submit_order"]
    fake_transport = _ORIGINALS.get("fake_transport.post_limit_buy")
    if fake_transport is not None:
        broker_type, original = fake_transport
        broker_type.post_limit_buy = original
