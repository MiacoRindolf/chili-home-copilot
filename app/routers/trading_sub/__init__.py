"""Trading sub-routers: AI brain, broker, Web3, operator, trades, patterns, scanning, backtest."""
from .ai import router as ai_router
from .backtest import router as backtest_router
from .broker import router as broker_router
from .data_provider import router as data_provider_router
from .inspect import router as inspect_router
from .monitor import router as monitor_router
from .operator import router as operator_router
from .patterns import router as patterns_router
from .scanning import router as scanning_router
from .trades import router as trades_router
from .web3 import router as web3_router

__all__ = [
    "ai_router",
    "backtest_router",
    "broker_router",
    "data_provider_router",
    "inspect_router",
    "monitor_router",
    "operator_router",
    "patterns_router",
    "scanning_router",
    "trades_router",
    "web3_router",
]
