from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_replay_benchmark_common import emit_result, run_pytest_slice  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "TRADING_BUSINESS_INVARIANT_BENCHMARK.md"
SCHEMA = "chili.trading-business-invariant-benchmark.v1"


def _portfolio_risk_sizing_invariants_pass() -> str:
    return run_pytest_slice(REPO_ROOT, ("tests/test_portfolio_risk_sizing.py",), timeout_seconds=120)


def _option_scope_invariants_pass() -> str:
    return run_pytest_slice(
        REPO_ROOT,
        (
            "tests/test_autopilot_scope_options.py::test_is_option_trade_honors_asset_kind_without_snapshot",
            "tests/test_autopilot_scope_options.py::test_is_option_trade_plain_equity_false",
            "tests/test_autopilot_scope_options.py::test_asset_kind_option_quote_never_falls_back_to_stock_quote",
        ),
        timeout_seconds=120,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return emit_result(
        argv=argv,
        description="Replay trading business invariants.",
        title="CHILI Trading Business Invariant Benchmark",
        schema=SCHEMA,
        output_path=DEFAULT_OUTPUT,
        checks=(
            ("portfolio_risk_sizing_invariants_pass", _portfolio_risk_sizing_invariants_pass),
            ("option_scope_invariants_pass", _option_scope_invariants_pass),
        ),
        required_behavior="business-risk fixtures must preserve direction-aware sizing, restrictive risk limits, and option-vs-equity scope boundaries.",
        safety="focused pytest replay only; no runtime restart, deployment, database migration, broker call, or live-trading action.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
