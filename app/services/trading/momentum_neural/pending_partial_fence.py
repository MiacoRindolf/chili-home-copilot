"""Non-expiring, per-session serialization for historical exit retirement.

The dedicated backend holds only a SESSION advisory lock during broker I/O,
never a row lock or open transaction. This follows the existing captured
transport/live-loop fence pattern without borrowing their ownership authority.
"""
from contextlib import contextmanager

from sqlalchemy import text


# Distinct two-key advisory namespace: ASCII "PPTR" (pending partial retirement).
NAMESPACE = int.from_bytes(b"PPTR", "big")


class PendingPartialFenceUnavailable(RuntimeError):
    pass


_OWNED = """
SELECT pg_backend_pid(), EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND pid = pg_backend_pid()
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND classid = :namespace AND objid = :session_id AND objsubid = 2 AND granted
)
"""


@contextmanager
def retirement_fence(engine, session_id: int):
    """Yield an ownership checker or None for a nonblocking contender.

    The caller must release its row/transaction before entering this context.
    Engine acquisition uses its configured bounded pool timeout. An uncertain
    acquire/commit/unlock invalidates the physical backend before pool return;
    SQLAlchemy may never reconnect and inherit authority from the old backend.
    """
    connection = None
    acquired = False
    params = {"namespace": NAMESPACE, "session_id": int(session_id)}
    try:
        connection = engine.connect()
        with connection.begin():
            backend, leaked = connection.execute(text(_OWNED), params).one()
            if leaked:
                raise PendingPartialFenceUnavailable("retirement_fence_found_on_pooled_backend")
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:namespace, :session_id)"), params,
            ).scalar_one() is True
        if not acquired:
            yield None
            return

        def assert_owned():
            if connection.closed or connection.invalidated:
                raise PendingPartialFenceUnavailable("retirement_fence_backend_lost")
            try:
                with connection.begin():
                    current_backend, held = connection.execute(text(_OWNED), params).one()
                if current_backend != backend or held is not True:
                    raise PendingPartialFenceUnavailable("retirement_fence_ownership_lost")
            except BaseException as exc:
                connection.invalidate()
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, PendingPartialFenceUnavailable):
                    raise
                raise PendingPartialFenceUnavailable("retirement_fence_check_failed") from exc

        assert_owned()
        yield assert_owned
    except BaseException:
        if connection is not None:
            connection.invalidate()
        raise
    finally:
        if connection is not None:
            try:
                if not connection.closed and not connection.invalidated:
                    if connection.in_transaction():
                        connection.rollback()
                    if acquired:
                        with connection.begin():
                            released = connection.execute(
                                text("SELECT pg_advisory_unlock(:namespace, :session_id)"), params,
                            ).scalar_one()
                            if released is not True:
                                raise PendingPartialFenceUnavailable("retirement_fence_release_unproven")
            except BaseException:
                connection.invalidate()
                raise
            finally:
                connection.close()
