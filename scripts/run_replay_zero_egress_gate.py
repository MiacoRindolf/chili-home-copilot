"""Run the ReplayV3 FSM regression inside an OS-isolated network namespace.

This entry point is intentionally small.  The companion Compose file gives the
process ``network_mode: none`` and exposes PostgreSQL only through a shared Unix
domain socket.  The namespace canary below must prove that no non-loopback
interface or IP route exists before pytest is imported.

Passing this gate proves that the selected synthetic FSM regression can execute
without IP egress.  It does *not* by itself certify a recorded live decision;
that stronger claim additionally requires a caller-pinned sealed capture and
the exact ReplayV3 capture-driver binding.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


TEST_NODE = (
    "tests/test_replay_v3_p1.py::"
    "test_replay_v3_p1_drives_one_session_end_to_end"
)


def _non_loopback_interfaces() -> tuple[str, ...]:
    return tuple(
        sorted(name for _index, name in socket.if_nameindex() if name != "lo")
    )


def _has_non_loopback_route(path: Path) -> bool:
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows = lines if path.name == "ipv6_route" else lines[1:]
    for line in rows:
        fields = line.split()
        if not fields:
            continue
        # Linux exposes the interface first in ``/proc/net/route`` but last
        # in ``/proc/net/ipv6_route``.  Treating the IPv6 destination as the
        # interface creates a false positive even in a loopback-only namespace.
        interface = fields[-1] if path.name == "ipv6_route" else fields[0]
        if interface != "lo":
            return True
    return False


def assert_os_zero_egress() -> dict[str, object]:
    if os.environ.get("CHILI_REPLAY_OS_ZERO_EGRESS") != "1":
        raise RuntimeError("zero-egress gate requires its explicit container marker")

    interfaces = _non_loopback_interfaces()
    if interfaces:
        raise RuntimeError(
            "zero-egress namespace exposes non-loopback interfaces: "
            + ",".join(interfaces)
        )
    route_files = (Path("/proc/net/route"), Path("/proc/net/ipv6_route"))
    routed = tuple(str(path) for path in route_files if _has_non_loopback_route(path))
    if routed:
        raise RuntimeError("zero-egress namespace exposes an IP route: " + ",".join(routed))

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        result = probe.connect_ex(("198.51.100.1", 9))
    finally:
        probe.close()
    if result == 0:
        raise RuntimeError("OS zero-egress canary unexpectedly connected")

    return {
        "network_namespace": "none",
        "non_loopback_interfaces": list(interfaces),
        "non_loopback_routes": list(routed),
        "blocked_connect_ex": int(result),
        "database_transport": "unix_domain_socket",
    }


def assert_database_unix_transport() -> dict[str, object]:
    """Verify the actual PostgreSQL connection is a Unix-domain connection."""

    database_url = str(os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("zero-egress gate requires TEST_DATABASE_URL")
    from sqlalchemy import create_engine, text as sql_text

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            server_ip = connection.execute(
                sql_text("SELECT inet_server_addr()::text")
            ).scalar_one_or_none()
            socket_directories = connection.execute(
                sql_text("SHOW unix_socket_directories")
            ).scalar_one()
    finally:
        engine.dispose()
    if server_ip is not None:
        raise RuntimeError(
            "zero-egress replay database is using an IP transport: "
            + str(server_ip)
        )
    return {
        "verified_transport": "unix_domain_socket",
        "inet_server_addr": None,
        "server_unix_socket_directories": str(socket_directories),
    }


def main() -> int:
    namespace_evidence = assert_os_zero_egress()
    database_evidence = assert_database_unix_transport()
    import pytest

    pytest_code = int(
        pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                TEST_NODE,
            ]
        )
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "gate": "replay_v3_os_zero_egress",
                "namespace": namespace_evidence,
                "database": database_evidence,
                "test_node": TEST_NODE,
                "pytest_exit_code": pytest_code,
                "claim_scope": "synthetic_fsm_regression_only",
                "exact_run_binding": None,
                "exact_os_zero_egress_attestation": None,
                "exact_claim_blocker": (
                    "canonical_recorded_live_replay_entrypoint_not_implemented"
                ),
            },
            sort_keys=True,
        )
    )
    return pytest_code


if __name__ == "__main__":
    sys.exit(main())
