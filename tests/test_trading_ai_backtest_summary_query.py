from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.trading import BacktestResult
from app.routers.trading_sub.ai import _evidence_backtest_summary_query


def test_evidence_backtest_query_is_summary_only_and_pattern_scoped() -> None:
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as db:
        query = _evidence_backtest_summary_query(
            db,
            BacktestResult,
            sibling_insight_ids=[11, 12],
            scan_pattern_id=42,
        ).limit(4000)
        sql = str(
            query.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

    assert "trading_backtests.related_insight_id IN (11, 12)" in sql
    assert "trading_backtests.scan_pattern_id = 42" in sql
    assert "trading_backtests.equity_curve" not in sql
    assert "trading_backtests.params" in sql
