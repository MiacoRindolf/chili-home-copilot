"""Fail-closed effective-DSN checks for local diagnostic replay tools."""
from __future__ import annotations

import os
import hashlib
import json
from dataclasses import dataclass
from psycopg2.extensions import parse_dsn
from urllib.parse import urlsplit

_TICKS_QUERY = """
SELECT id, observed_at, price, size, bid, ask
FROM public.replay_golden_ticks
WHERE symbol = %s AND observed_at >= %s AND observed_at < %s AND price > 0
ORDER BY observed_at ASC, id ASC
"""

_NBBO_QUERY = """
SELECT id, observed_at, bid, ask, mid, spread_bps, day_volume, source
FROM public.replay_golden_nbbo
WHERE symbol = %s AND observed_at >= %s AND observed_at < %s
ORDER BY observed_at ASC, id ASC
"""

_QUERY_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "ticks": " ".join(_TICKS_QUERY.split()),
            "nbbo": " ".join(_NBBO_QUERY.split()),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class DatabaseIdentity:
    host: str
    port: int
    dbname: str
    user: str

    @property
    def server_key(self) -> tuple[str, int, str]:
        return self.host, self.port, self.dbname

    def public_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
        }


def guarded_database_identity(url: str, *, sink: bool) -> DatabaseIdentity:
    try:
        split = urlsplit(url)
    except ValueError as exc:
        raise ValueError("explicit PostgreSQL URL required") from exc
    if (
        split.scheme not in {"postgres", "postgresql"}
        or not split.netloc
        or split.query
        or split.fragment
    ):
        raise ValueError(
            "canonical PostgreSQL URL without query/fragment overrides required"
        )
    try:
        parsed = parse_dsn(url)
    except Exception as exc:
        raise ValueError("explicit PostgreSQL DSN required") from exc
    allowed_dsn_keys = {"dbname", "host", "password", "port", "user"}
    unexpected_dsn_keys = {
        key for key, value in parsed.items()
        if value not in {None, ""} and key not in allowed_dsn_keys
    }
    if unexpected_dsn_keys:
        raise ValueError(
            "only explicit dbname/host/port/user/password DSN fields are allowed"
        )
    inherited_pg_keys = sorted(
        key for key, value in os.environ.items()
        if key.upper().startswith("PG") and value
    )
    if inherited_pg_keys:
        raise ValueError(
            "all ambient libpq PG* environment inheritance is forbidden"
        )
    host = str(parsed.get("host") or "").strip().lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("diagnostic replay database host must be explicit loopback")
    if "," in host:
        raise ValueError("multi-host database DSNs are forbidden")
    dbname = str(parsed.get("dbname") or "").strip()
    if not dbname or "/" in dbname or "\\" in dbname:
        raise ValueError("explicit database name required")
    if sink and not dbname.endswith("_test"):
        raise ValueError("diagnostic replay sink database name must end in _test")
    try:
        port = int(parsed.get("port") or 5432)
    except (TypeError, ValueError) as exc:
        raise ValueError("explicit numeric database port required") from exc
    if not (1 <= port <= 65535):
        raise ValueError("database port is out of range")
    user = str(parsed.get("user") or "").strip()
    password = str(parsed.get("password") or "")
    if not user or not password:
        raise ValueError("explicit database user and password are required")
    return DatabaseIdentity(
        host="loopback",
        port=port,
        dbname=dbname,
        user=user,
    )


def verify_connected_endpoint(conn, expected: DatabaseIdentity) -> None:
    params = conn.get_dsn_parameters()
    host = str(params.get("host") or "").strip().lower()
    try:
        port = int(params.get("port") or 5432)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("connected client port is invalid") from exc
    user = str(params.get("user") or "")
    if (
        host not in {"localhost", "127.0.0.1", "::1"}
        or port != expected.port
        or user != expected.user
    ):
        raise RuntimeError("connected client endpoint mismatch")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_database(), current_user, current_schema(), "
            "current_setting('search_path')"
        )
        dbname, current_user, current_schema, search_path = cur.fetchone()
    if str(dbname) != expected.dbname:
        raise RuntimeError("connected database identity mismatch")
    if str(current_user) != expected.user:
        raise RuntimeError("connected database user mismatch")
    if str(current_schema) != "public" or str(search_path).strip() not in {
        '"$user", public',
        "public",
    }:
        raise RuntimeError("connected database schema/search_path mismatch")


class _HashWriter:
    def __init__(self):
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, value):
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        self._hash.update(raw)
        self.size += len(raw)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _copy_query_receipt(
    conn,
    query: str,
    params: tuple,
    *,
    before_query=None,
) -> dict:
    if before_query is not None:
        before_query()
    writer = _HashWriter()
    with conn.cursor() as cur:
        rendered = cur.mogrify(query, params).decode("utf-8")
        cur.copy_expert(
            f"COPY ({rendered}) TO STDOUT WITH (FORMAT CSV, NULL '\\N')",
            writer,
        )
    return {"bytes": writer.size, "sha256": writer.hexdigest()}


def golden_window_content_receipt(
    conn,
    *,
    symbol: str,
    start: str,
    end: str,
    before_query=None,
) -> dict:
    params = (symbol, start, end)
    return {
        "schema": "chili.golden-window-content-receipt.v2",
        "query_contract_sha256": _QUERY_CONTRACT_SHA256,
        "symbol": symbol,
        "start": start,
        "end": end,
        "ticks": _copy_query_receipt(
            conn,
            _TICKS_QUERY,
            params,
            before_query=before_query,
        ),
        "nbbo": _copy_query_receipt(
            conn,
            _NBBO_QUERY,
            params,
            before_query=before_query,
        ),
    }


def content_receipt_sha256(receipt: dict) -> str:
    raw = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def query_contract_sha256() -> str:
    return _QUERY_CONTRACT_SHA256
