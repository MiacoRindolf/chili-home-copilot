"""PostgreSQL connection helpers for CLI scripts (Windows socket / localhost quirks)."""
from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def prefer_ipv4_localhost(url: str) -> str:
    """If URL uses ``localhost``, return a copy using ``127.0.0.1`` (single substitution)."""
    if not url or "localhost" not in url.lower():
        return url
    # Avoid changing passwords that contain the substring "localhost"
    for pattern, repl in (
        (r"@localhost:", "@127.0.0.1:"),
        (r"@localhost/", "@127.0.0.1/"),
        (r"://localhost:", "://127.0.0.1:"),
        (r"://localhost/", "://127.0.0.1/"),
    ):
        if re.search(pattern, url, flags=re.IGNORECASE):
            return re.sub(pattern, repl, url, count=1, flags=re.IGNORECASE)
    return url


def _is_buffer_space(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "10055" in str(exc) or "buffer space" in msg


def print_windows_socket_exhaustion_help() -> None:
    print(
        "\nCould not open a TCP connection to PostgreSQL (Windows often reports this as "
        '"No buffer space available" / Winsock 10055). This is usually **OS socket pool '
        "exhaustion**, not a bad password or wrong script.\n\n"
        "Try, in order:\n"
        "  1. Use **127.0.0.1** in DATABASE_URL instead of **localhost** (reduces dual-stack tries).\n"
        "  2. Close heavy apps, **restart Docker Desktop**, or **reboot**.\n"
        "  3. Run the script **inside Docker** on the Compose network (see docs/DATABASE_POSTGRES.md).\n",
        file=sys.stderr,
    )


def create_postgres_engine_connected(url: str) -> "Engine":
    """Create a SQLAlchemy engine and verify connectivity.

    On Windows buffer-space errors, retries once with ``127.0.0.1`` if the URL used ``localhost``.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    candidates = [url]
    alt = prefer_ipv4_localhost(url)
    if alt != url:
        candidates.append(alt)

    for i, u in enumerate(candidates):
        eng = create_engine(u, pool_pre_ping=True)
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            return eng
        except OperationalError as e:
            orig = getattr(e, "orig", e)
            if i == 0 and len(candidates) > 1 and _is_buffer_space(orig):
                print(
                    "localhost failed (socket exhaustion?). Retrying with 127.0.0.1 …",
                    file=sys.stderr,
                )
                continue
            if _is_buffer_space(orig):
                print_windows_socket_exhaustion_help()
            raise
