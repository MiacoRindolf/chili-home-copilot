from __future__ import annotations

import errno

import requests

from app.services.socket_budget import (
    DEFAULT_HTTP_POOL_CONNECTIONS,
    DEFAULT_HTTP_POOL_MAXSIZE,
    SOCKET_EXHAUSTION_WINERROR,
    is_socket_exhaustion_error,
    mount_bounded_http_adapters,
)


def test_detects_windows_socket_exhaustion_wrapped_by_requests() -> None:
    root = OSError(SOCKET_EXHAUSTION_WINERROR, "No buffer space available")
    exc = requests.ConnectionError(root)

    assert is_socket_exhaustion_error(exc) is True


def test_detects_enobufs_errno() -> None:
    exc = OSError(errno.ENOBUFS, "No buffer space available")

    assert is_socket_exhaustion_error(exc) is True


def test_ignores_plain_timeout_without_socket_budget_markers() -> None:
    exc = requests.Timeout("read timed out")

    assert is_socket_exhaustion_error(exc) is False


def test_mount_bounded_http_adapters_uses_blocking_pools() -> None:
    session = requests.Session()

    mount_bounded_http_adapters(
        session,
        pool_connections=DEFAULT_HTTP_POOL_CONNECTIONS,
        pool_maxsize=DEFAULT_HTTP_POOL_MAXSIZE,
    )

    https_adapter = session.get_adapter("https://")
    http_adapter = session.get_adapter("http://")
    assert https_adapter._pool_connections == DEFAULT_HTTP_POOL_CONNECTIONS
    assert https_adapter._pool_maxsize == DEFAULT_HTTP_POOL_MAXSIZE
    assert https_adapter._pool_block is True
    assert http_adapter._pool_block is True
