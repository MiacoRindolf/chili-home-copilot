"""Trading sub-routers for AI brain, broker, and Web3 endpoints."""
from .ai import router as ai_router
from .broker import router as broker_router
from .web3 import router as web3_router

__all__ = ["ai_router", "broker_router", "web3_router"]
