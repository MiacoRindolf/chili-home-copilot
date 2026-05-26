"""Shared socket-pressure guardrails for outbound HTTP clients."""
from __future__ import annotations

import errno
from collections.abc import Iterable
from typing import Any

import requests
from requests.adapters import HTTPAdapter

SOCKET_EXHAUSTION_WINERROR = 10055
SOCKET_EXHAUSTION_ERRNOS = frozenset({
    SOCKET_EXHAUSTION_WINERROR,
    errno.ENOBUFS,
})
SOCKET_EXHAUSTION_TEXT_MARKERS = (
    "no buffer space available",
    "winerror 10055",
    "errno 10055",
)
DEFAULT_HTTP_POOL_CONNECTIONS = 16
DEFAULT_HTTP_POOL_MAXSIZE = 32


def _iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        ident = id(cur)
        if ident in seen:
            continue
        seen.add(ident)
        yield cur
        cause = getattr(cur, "__cause__", None)
        context = getattr(cur, "__context__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        if isinstance(context, BaseException):
            stack.append(context)
        for arg in getattr(cur, "args", ()) or ():
            if isinstance(arg, BaseException):
                stack.append(arg)


def is_socket_exhaustion_error(exc: BaseException) -> bool:
    """Detect Windows 10055 / ENOBUFS across nested requests exceptions."""
    for cur in _iter_exception_chain(exc):
        winerror = getattr(cur, "winerror", None)
        if winerror in SOCKET_EXHAUSTION_ERRNOS:
            return True
        err_no = getattr(cur, "errno", None)
        if err_no in SOCKET_EXHAUSTION_ERRNOS:
            return True
        for arg in getattr(cur, "args", ()) or ():
            if isinstance(arg, int) and arg in SOCKET_EXHAUSTION_ERRNOS:
                return True
        text = str(cur).lower()
        if any(marker in text for marker in SOCKET_EXHAUSTION_TEXT_MARKERS):
            return True
    return False


def mount_bounded_http_adapters(
    session: requests.Session,
    *,
    pool_connections: int = DEFAULT_HTTP_POOL_CONNECTIONS,
    pool_maxsize: int = DEFAULT_HTTP_POOL_MAXSIZE,
    pool_block: bool = True,
) -> None:
    """Mount blocking urllib3 pools so outage bursts reuse sockets.

    ``pool_block=True`` is the critical part: without it urllib3 can create
    throwaway connections beyond ``pool_maxsize``, which turns provider outages
    into TIME_WAIT storms and eventually WinError 10055 on Windows.
    """
    pc = max(1, int(pool_connections))
    pm = max(pc, int(pool_maxsize))
    adapter = HTTPAdapter(
        pool_connections=pc,
        pool_maxsize=pm,
        pool_block=bool(pool_block),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def socket_pressure_snapshot() -> dict[str, Any]:
    """Best-effort local TCP snapshot for diagnostics; never raises."""
    try:
        import psutil  # type: ignore
    except Exception:
        return {"available": False, "reason": "psutil_unavailable"}

    try:
        counts: dict[str, int] = {}
        for conn in psutil.net_connections(kind="tcp"):
            status = str(getattr(conn, "status", "") or "UNKNOWN")
            counts[status] = counts.get(status, 0) + 1
        return {"available": True, "tcp_states": counts}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__}
