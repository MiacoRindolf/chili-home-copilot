"""Read-only PostgreSQL candidate inventory for captured Alpaca PAPER.

The initial-material provider must record the complete considered candidate set,
including candidates that are inactive or economically ineligible.  This port
therefore applies only the durable route boundary (exact symbol and
``scope='symbol'``), joins every referenced strategy variant, and leaves all
eligibility decisions to the provider.

Each call owns one short REPEATABLE READ / READ ONLY transaction.  Returned ORM
rows are expunged before rollback so downstream content-hash helpers can inspect
stable detached values without retaining a SQLAlchemy session or performing a
lazy read.  No user ownership predicate is applied: neither backing table has a
user/owner column.  ``user_id`` remains an exact positive route value carried by
the typed read receipt.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ....models.trading import MomentumStrategyVariant, MomentumSymbolViability
from .captured_paper_initial_provider import (
    CapturedPaperInitialCandidateRead,
    CapturedPaperInitialCandidateReadPort,
    CapturedPaperInitialCandidateRow,
)


_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,35}")
_READ_ONLY_TRANSACTION_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)
_READ_AT_SQL = (
    "SELECT LEAST(transaction_timestamp(), "
    "CAST(:decision_at AS timestamptz))"
)


class CapturedPaperInitialCandidateReaderUnavailable(RuntimeError):
    """The exact read-only candidate inventory could not be produced."""

    def __init__(self, reason: str):
        self.reason = str(reason or "initial_candidate_reader_unavailable")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperInitialCandidateReaderUnavailable(reason)


def _positive_user_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        _reject("initial_candidate_reader_user_id_invalid")
    return value


def _symbol(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _SYMBOL_RE.fullmatch(value) is None
        or value.endswith(".")
        or ".." in value
    ):
        _reject("initial_candidate_reader_symbol_invalid")
    return value


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(f"{field_name}_invalid")
    try:
        offset = value.utcoffset()
    except Exception as exc:  # pragma: no cover - defensive tzinfo boundary
        raise CapturedPaperInitialCandidateReaderUnavailable(
            f"{field_name}_invalid"
        ) from exc
    if offset is None:
        _reject(f"{field_name}_invalid")
    return value.astimezone(timezone.utc)


class SqlAlchemyCapturedPaperInitialCandidateReader(
    CapturedPaperInitialCandidateReadPort
):
    """Exact detached candidate read port bound to one SQLAlchemy Engine."""

    def __init__(self, bind: Engine) -> None:
        if not isinstance(bind, Engine):
            _reject("initial_candidate_reader_engine_invalid")
        self._bind = bind

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def mutation_allowed(self) -> bool:
        return False

    def read_candidates(
        self,
        *,
        user_id: int,
        symbol: str,
        decision_at: datetime,
    ) -> CapturedPaperInitialCandidateRead:
        exact_user_id = _positive_user_id(user_id)
        exact_symbol = _symbol(symbol)
        decision_utc = _aware_utc(
            decision_at,
            "initial_candidate_reader_decision_at",
        )

        db = Session(bind=self._bind, expire_on_commit=False)
        try:
            # This must be the first statement.  The database, rather than a
            # process wall clock, owns the stable snapshot/read frontier.
            db.execute(text(_READ_ONLY_TRANSACTION_SQL))
            raw_read_at = db.execute(
                text(_READ_AT_SQL),
                {"decision_at": decision_utc},
            ).scalar_one()
            read_at = _aware_utc(
                raw_read_at,
                "initial_candidate_reader_read_at",
            )

            pairs = (
                db.query(MomentumSymbolViability, MomentumStrategyVariant)
                .join(
                    MomentumStrategyVariant,
                    MomentumStrategyVariant.id
                    == MomentumSymbolViability.variant_id,
                )
                .filter(
                    MomentumSymbolViability.symbol == exact_symbol,
                    MomentumSymbolViability.scope == "symbol",
                )
                .order_by(
                    MomentumStrategyVariant.id.asc(),
                    MomentumSymbolViability.id.asc(),
                )
                .all()
            )
            exact_pairs: list[
                tuple[MomentumSymbolViability, MomentumStrategyVariant]
            ] = []
            for pair in pairs:
                try:
                    viability, variant = pair
                except (TypeError, ValueError) as exc:
                    raise CapturedPaperInitialCandidateReaderUnavailable(
                        "initial_candidate_reader_row_invalid"
                    ) from exc
                if not isinstance(
                    viability, MomentumSymbolViability
                ) or not isinstance(variant, MomentumStrategyVariant):
                    _reject("initial_candidate_reader_row_invalid")
                if (
                    viability.symbol != exact_symbol
                    or viability.scope != "symbol"
                    or int(viability.variant_id or 0) != int(variant.id or 0)
                ):
                    _reject("initial_candidate_reader_row_scope_mismatch")
                exact_pairs.append((viability, variant))

            exact_pairs.sort(
                key=lambda pair: (
                    int(pair[1].id or 0),
                    int(pair[0].id or 0),
                )
            )
            detached_ids: set[int] = set()
            rows: list[CapturedPaperInitialCandidateRow] = []
            for viability, variant in exact_pairs:
                for orm_row in (viability, variant):
                    object_id = id(orm_row)
                    if object_id not in detached_ids:
                        db.expunge(orm_row)
                        detached_ids.add(object_id)
                rows.append(
                    CapturedPaperInitialCandidateRow(
                        variant=variant,
                        viability=viability,
                    )
                )
            return CapturedPaperInitialCandidateRead(
                user_id=exact_user_id,
                symbol=exact_symbol,
                read_at=read_at,
                rows=tuple(rows),
            )
        finally:
            try:
                db.rollback()
            finally:
                db.close()


__all__ = [
    "CapturedPaperInitialCandidateReaderUnavailable",
    "SqlAlchemyCapturedPaperInitialCandidateReader",
]
