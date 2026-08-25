"""Cross-process authority fence for the dedicated captured-PAPER service.

The captured service holds one PostgreSQL *session* advisory lock on a
dedicated connection for its entire process-lifetime runtime.  Generic Alpaca
arm/promote flows acquire the conflicting non-blocking transaction lock before
they may mutate an arm generation.  This closes the otherwise unavoidable
first-session race where no durable captured-session owner row exists yet.

This module is deliberately inert on import.  It neither opens a database
connection nor contacts a broker/provider until an explicit ``acquire`` call.
"""

from __future__ import annotations

import re

from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import threading
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import text


_ACCOUNT_SCOPE = "alpaca:paper"

# Two positive signed-int32 keys give this subsystem a stable PostgreSQL
# advisory-lock namespace without relying on server-version-specific hashtext
# output.  The first key is ASCII "CPFS" (Captured Paper Fence Service); the
# second is the v1 account-scope slot.  These are operational identity values,
# not strategy/risk caps.
CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID = 0x43504653
CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID = 1

# Ang irreducible base para sa bounded wait, sa millisecond. Sapat para tumawid sa
# nasukat na 6s na arm transaction sa dalawang pagtatangka; malayo pa rin sa ilalim
# ng anumang wake cadence kaya walang pulse na natatagalan.
_DEFAULT_ARM_FENCE_WAIT_MS = 2500
_MAX_ARM_FENCE_WAIT_MS = 15000
_LOCK_TIMEOUT_TOKEN = re.compile(r"[0-9]+(us|ms|s|min|h|d)?")

_TRY_SESSION_LOCK_SQL = """
SELECT pg_try_advisory_lock(:class_id, :object_id)
"""
_TRY_TRANSACTION_LOCK_SQL = """
SELECT pg_try_advisory_xact_lock(:class_id, :object_id)
"""
# ANG BOUNDED WAIT (2026-08-25). Ang pg_try_* ay HINDI naghihintay: kung hawak ng
# KAPWA lane connection ang fence, agad na bumabagsak ang arm na ito. Hindi na
# teoretikal iyon -- NASUKAT sa buhay na premarket:
#
#   fence hawak sa 14/25 -> 22/25 na sample (56-88%), lahat mula 172.18.0.1
#   ang may hawak ay may xact na 6s ang tanda, nagpapatakbo ng arm-path query
#   [auto_arm] begin_live_arm blocked AIXI/RCON/SWVL/BDRX/WVVIP -> armed=0
#
# Isang mabagal na arm transaction ang humahawak sa pandaigdigang mutex nang ilang
# segundo, at ang bawat kasabay na arm ay namamatay agad. Sa 5 kandidato,
# garantisadong 4 ang mawawala -- kasama ang pinakamataas sa viability.
#
# ⚠️ ANG PAG-AARI NG FENCE AY HINDI NAGBABAGO. Ang layunin nito ay ihiwalay ang
# DEDIKADONG captured-paper SERVICE, at nananatili iyon: kung hawak ng service ang
# session lock nito, mag-e-expire ang hintay na ito at fail-closed pa rin. Ang
# ipinagkakaiba lang ay pumipila na ang KAPWA lane transaction sa halip na mamatay.
_WAIT_TRANSACTION_LOCK_SQL = """
SELECT pg_advisory_xact_lock(:class_id, :object_id)
"""
_READ_LOCK_TIMEOUT_SQL = """
SELECT current_setting('lock_timeout')
"""
_ASSERT_SESSION_LOCK_SQL = """
SELECT EXISTS (
    SELECT 1
      FROM pg_locks
     WHERE locktype = 'advisory'
       AND pid = pg_backend_pid()
       AND classid::bigint = :class_id
       AND objid::bigint = :object_id
       AND objsubid = 2
       AND mode = 'ExclusiveLock'
       AND granted IS TRUE
)
"""
_UNLOCK_SESSION_SQL = """
SELECT pg_advisory_unlock(:class_id, :object_id)
"""
_PRESTART_ADMISSION_INVENTORY_SQL = """
SELECT
    (
        SELECT count(*)
          FROM trading_automation_sessions
         WHERE mode = 'live'
           AND execution_family IN ('alpaca_spot', 'alpaca_short')
           AND state NOT IN (
               'cancelled', 'expired', 'error', 'archived', 'finished',
               'live_finished', 'live_cancelled', 'live_error',
               'live_arm_expired'
           )
    ) AS active_sessions,
    (
        SELECT count(*)
          FROM broker_symbol_action_claims
         WHERE account_scope = 'alpaca:paper'
           AND phase <> 'resolved'
    ) AS active_action_claims,
    (
        SELECT count(*)
          FROM adaptive_risk_reservations
         WHERE account_scope = 'alpaca:paper'
           AND state NOT IN ('released', 'closed')
    ) AS active_reservations,
    (
        SELECT count(*)
          FROM adaptive_risk_opportunity_claims
         WHERE account_scope = 'alpaca:paper'
           AND status = 'reserved'
    ) AS reserved_opportunities,
    (
        SELECT count(*)
          FROM captured_paper_post_commit_outbox
         WHERE account_scope = 'alpaca:paper'
           AND status <> 'completed'
    ) AS active_outbox_rows,
    (
        SELECT count(*)
          FROM captured_paper_completed_fill_watch
         WHERE state NOT IN ('terminal_zero_fill', 'fill_handoff_committed')
    ) AS active_fill_watches
"""

_PRESTART_COUNT_FIELDS = (
    "active_sessions",
    "active_action_claims",
    "active_reservations",
    "reserved_opportunities",
    "active_outbox_rows",
    "active_fill_watches",
)


class CapturedPaperServiceFenceError(RuntimeError):
    """The exclusive captured-PAPER process authority is unavailable/lost."""


def _postgresql_bind(value: Any) -> Any:
    """Return a PostgreSQL bind or fail closed before an advisory-lock query."""

    bind = value
    get_bind = getattr(value, "get_bind", None)
    if callable(get_bind):
        bind = get_bind()
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect != "postgresql":
        raise CapturedPaperServiceFenceError(
            "captured_paper_service_fence_requires_postgresql"
        )
    return bind


def _scalar_bool(result: Any) -> bool:
    scalar_one = getattr(result, "scalar_one", None)
    if callable(scalar_one):
        value = scalar_one()
    else:
        scalar = getattr(result, "scalar", None)
        if not callable(scalar):
            raise CapturedPaperServiceFenceError(
                "captured_paper_service_fence_result_invalid"
            )
        value = scalar()
    return value is True


def try_acquire_generic_alpaca_arm_fence(
    db: Any,
    *,
    account_scope: str,
) -> bool:
    """Try the generic Alpaca transaction side of the process fence.

    The caller owns the transaction.  A successful lock remains held until its
    commit/rollback, thereby preventing the dedicated service from starting in
    the middle of the generic arm mutation.  Absence, a non-PostgreSQL bind,
    query failure, malformed result, or a held service lock all fail closed.
    No commit/rollback is performed here.
    """

    if str(account_scope or "").strip().lower() != _ACCOUNT_SCOPE:
        return False
    params = {
        "class_id": CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID,
        "object_id": CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID,
    }
    try:
        _postgresql_bind(db)
        if _scalar_bool(db.execute(text(_TRY_TRANSACTION_LOCK_SQL), params)):
            return True
    except Exception:
        return False

    wait_ms = _generic_arm_fence_wait_ms()
    if wait_ms <= 0:
        return False
    return _wait_for_generic_alpaca_arm_fence(db, params, wait_ms)


def _generic_arm_fence_wait_ms() -> int:
    """ISANG dokumentadong knob: gaano katagal pipila ang isang arm sa KAPWA nito.

    Ang 0 ay nagpapanumbalik ng eksaktong lumang gawi (try-once, fail-closed)."""
    try:
        from app.config import settings as _settings

        raw = getattr(_settings, "chili_momentum_alpaca_arm_fence_wait_ms", _DEFAULT_ARM_FENCE_WAIT_MS)
        value = int(raw if raw is not None else _DEFAULT_ARM_FENCE_WAIT_MS)
    except Exception:
        return _DEFAULT_ARM_FENCE_WAIT_MS
    return max(0, min(_MAX_ARM_FENCE_WAIT_MS, value))


def _wait_for_generic_alpaca_arm_fence(db: Any, params: Mapping[str, Any], wait_ms: int) -> bool:
    """Bounded na hintay sa loob ng SAVEPOINT.

    ⚠️ ANG SAVEPOINT ANG BUONG PUNTO. Kapag nag-expire ang lock_timeout ay
    NAGTATAAS ng error ang PostgreSQL, at ang isang error na hindi nakakulong ay
    umaabort sa BUONG transaction ng tumawag -- ang eksaktong transaction-poison
    na klase kung saan ang isang fail-open na basa ay pumapatay sa lahat ng
    sumusunod dito. Ang subtransaction ay naglalaman ng abort na iyon, kaya ang
    isang na-expire na hintay ay nagbabalik lang ng False at nananatiling
    magagamit ang transaction ng tumawag.

    Ang isang lock na nakuha sa loob ng isang subtransaction na nag-commit ay
    hawak hanggang sa katapusan ng PANGUNAHING transaction, kaya hindi nagbabago
    ang buhay ng fence.

    ⚠️ IBINABALIK ANG lock_timeout sa daan ng TAGUMPAY. Ang SET LOCAL ay
    umuurong lamang kapag NAG-ABORT ang subtransaction; sa isang release ay
    tumatagos ito hanggang sa katapusan ng nakapaloob na transaction, at ang
    isang tumagas na 2.5s na timeout ay magpapabagsak sa mga susunod na
    pahayag ng tumawag sa unang tunay na paghihintay sa lock."""
    nested = None
    prior = None
    try:
        prior = _sanitized_lock_timeout(db.execute(text(_READ_LOCK_TIMEOUT_SQL)).scalar())
        nested = db.begin_nested()
        # Ang SET LOCAL ay hindi tumatanggap ng bind parameter; ang wait_ms ay isang
        # na-clamp na int at ang prior ay na-sanitize, kaya walang naipapasok na teksto.
        db.execute(text("SET LOCAL lock_timeout = '" + str(int(wait_ms)) + "ms'"))
        db.execute(text(_WAIT_TRANSACTION_LOCK_SQL), dict(params))
        nested.commit()
    except Exception:
        try:
            if nested is not None and nested.is_active:
                nested.rollback()
        except Exception:
            pass
        return False
    try:
        db.execute(text("SET LOCAL lock_timeout = '" + (prior or "0") + "'"))
    except Exception:
        pass
    return True


def _sanitized_lock_timeout(value: Any) -> str:
    """Payagan lamang ang isang PostgreSQL interval-ish na token ('0', '2s', '250ms')."""
    token = str(value or "0").strip().lower()
    if not token or len(token) > 16 or not _LOCK_TIMEOUT_TOKEN.fullmatch(token):
        return "0"
    return token


def read_captured_paper_prestart_admission_inventory(
    bind: Any,
) -> Mapping[str, Any]:
    """Atomically inventory every durable exposure-increasing PAPER seam.

    One SQL statement gives all subqueries the same PostgreSQL statement
    snapshot.  The process session fence must already be held by the caller;
    because generic arm paths need the conflicting xact lock, no compliant
    writer can commit between this snapshot and provider/runtime startup.
    """

    engine = _postgresql_bind(bind)
    if not callable(getattr(engine, "connect", None)):
        raise CapturedPaperServiceFenceError(
            "captured_paper_prestart_inventory_bind_invalid"
        )
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(_PRESTART_ADMISSION_INVENTORY_SQL)
            ).one()
        counts = {
            name: int(getattr(row, name))
            for name in _PRESTART_COUNT_FIELDS
        }
    except Exception as exc:
        raise CapturedPaperServiceFenceError(
            "captured_paper_prestart_inventory_unavailable"
        ) from exc
    if any(value < 0 for value in counts.values()):
        raise CapturedPaperServiceFenceError(
            "captured_paper_prestart_inventory_invalid"
        )
    body: dict[str, Any] = {
        "schema_version": "chili.captured-paper-prestart-admission-inventory.v1",
        "account_scope": _ACCOUNT_SCOPE,
        **counts,
        "active_total": sum(counts.values()),
        "empty": all(value == 0 for value in counts.values()),
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return MappingProxyType(
        {
            **body,
            "inventory_canonical_json": canonical,
            "inventory_sha256": hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest(),
        }
    )
@dataclass(frozen=True, slots=True)
class CapturedPaperServiceFenceReceipt:
    backend_pid: int

    def to_mapping(self, *, held: bool) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": "chili.captured-paper-service-fence.v1",
                "account_scope": _ACCOUNT_SCOPE,
                "class_id": CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID,
                "object_id": CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID,
                "backend_pid": int(self.backend_pid),
                "held": bool(held),
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
        )


class CapturedPaperServiceFence:
    """Own one dedicated PostgreSQL connection and its session advisory lock."""

    def __init__(self, bind: Any) -> None:
        self._bind = _postgresql_bind(bind)
        if not callable(getattr(self._bind, "connect", None)):
            raise CapturedPaperServiceFenceError(
                "captured_paper_service_fence_bind_invalid"
            )
        self._guard = threading.RLock()
        self._connection: Any | None = None
        self._receipt: CapturedPaperServiceFenceReceipt | None = None

    @staticmethod
    def _params() -> dict[str, int]:
        return {
            "class_id": CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID,
            "object_id": CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID,
        }

    @staticmethod
    def _invalidate_and_close(connection: Any) -> None:
        # A failed unlock must destroy (not return) the physical DB session;
        # PostgreSQL then releases every session advisory lock automatically.
        with suppress(Exception):
            connection.invalidate()
        with suppress(Exception):
            connection.close()

    @staticmethod
    def _commit(connection: Any) -> None:
        commit = getattr(connection, "commit", None)
        if not callable(commit):
            raise CapturedPaperServiceFenceError(
                "captured_paper_service_fence_connection_invalid"
            )
        commit()

    def acquire(self) -> Mapping[str, Any]:
        """Acquire once before any captured provider/runtime is started."""

        with self._guard:
            if self._connection is not None or self._receipt is not None:
                raise CapturedPaperServiceFenceError(
                    "captured_paper_service_fence_acquire_is_one_shot"
                )
            connection = self._bind.connect()
            try:
                acquired = _scalar_bool(
                    connection.execute(
                        text(_TRY_SESSION_LOCK_SQL), self._params()
                    )
                )
                # End SQLAlchemy's implicit transaction immediately.  The
                # session-level lock survives COMMIT while avoiding a process-
                # lifetime idle-in-transaction connection.
                self._commit(connection)
                if not acquired:
                    connection.close()
                    raise CapturedPaperServiceFenceError(
                        "captured_paper_service_fence_busy"
                    )
                backend_pid = int(
                    connection.execute(text("SELECT pg_backend_pid()"))
                    .scalar_one()
                )
                self._commit(connection)
                if backend_pid <= 0:
                    raise CapturedPaperServiceFenceError(
                        "captured_paper_service_fence_backend_invalid"
                    )
                self._connection = connection
                self._receipt = CapturedPaperServiceFenceReceipt(
                    backend_pid=backend_pid
                )
                self.assert_held()
                return self._receipt.to_mapping(held=True)
            except BaseException:
                if self._connection is connection:
                    self._connection = None
                    self._receipt = None
                self._invalidate_and_close(connection)
                raise

    def assert_held(self) -> None:
        """Prove that this exact backend still owns the granted lock."""

        with self._guard:
            connection = self._connection
            receipt = self._receipt
            if connection is None or receipt is None:
                raise CapturedPaperServiceFenceError(
                    "captured_paper_service_fence_not_held"
                )
            try:
                row = connection.execute(
                    text(
                        "SELECT pg_backend_pid() AS backend_pid, "
                        f"({_ASSERT_SESSION_LOCK_SQL.strip()}) AS held"
                    ),
                    self._params(),
                ).one()
                self._commit(connection)
                if int(row.backend_pid) != receipt.backend_pid or row.held is not True:
                    raise CapturedPaperServiceFenceError(
                        "captured_paper_service_fence_lost"
                    )
            except BaseException:
                self._connection = None
                self._receipt = None
                self._invalidate_and_close(connection)
                raise

    def release(self) -> Mapping[str, Any]:
        """Release only after every provider/runtime/order worker is quiescent."""

        with self._guard:
            connection = self._connection
            receipt = self._receipt
            if connection is None and receipt is None:
                return MappingProxyType(
                    {
                        "schema_version": "chili.captured-paper-service-fence.v1",
                        "account_scope": _ACCOUNT_SCOPE,
                        "held": False,
                        "live_cash_authorized": False,
                        "real_money_authorized": False,
                    }
                )
            if connection is None or receipt is None:
                raise CapturedPaperServiceFenceError(
                    "captured_paper_service_fence_state_invalid"
                )
            try:
                released = _scalar_bool(
                    connection.execute(
                        text(_UNLOCK_SESSION_SQL), self._params()
                    )
                )
                self._commit(connection)
                if not released:
                    raise CapturedPaperServiceFenceError(
                        "captured_paper_service_fence_unlock_unconfirmed"
                    )
                connection.close()
            except BaseException:
                self._connection = None
                self._receipt = None
                self._invalidate_and_close(connection)
                raise
            self._connection = None
            self._receipt = None
            return receipt.to_mapping(held=False)

    def health(self) -> Mapping[str, Any]:
        """Return local state; callers use ``assert_held`` for a DB proof."""

        with self._guard:
            receipt = self._receipt
            if receipt is None:
                return MappingProxyType(
                    {
                        "schema_version": "chili.captured-paper-service-fence.v1",
                        "account_scope": _ACCOUNT_SCOPE,
                        "held": False,
                        "live_cash_authorized": False,
                        "real_money_authorized": False,
                    }
                )
            return receipt.to_mapping(held=self._connection is not None)


__all__ = (
    "CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID",
    "CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID",
    "CapturedPaperServiceFence",
    "CapturedPaperServiceFenceError",
    "CapturedPaperServiceFenceReceipt",
    "read_captured_paper_prestart_admission_inventory",
    "try_acquire_generic_alpaca_arm_fence",
)
