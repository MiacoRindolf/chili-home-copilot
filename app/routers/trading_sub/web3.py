"""Web3 / MetaMask DEX swap endpoints for the trading module."""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.trading import web3_service as w3

router = APIRouter(tags=["trading-web3"])


class SwapQuoteRequest(BaseModel):
    chain_id: int
    sell_token: str
    buy_token: str
    sell_amount: str
    taker_address: str
    slippage_bps: int = 50


@router.get("/api/trading/web3/chains")
def api_chains():
    """Return supported blockchain networks."""
    return JSONResponse({"ok": True, "chains": w3.get_supported_chains()})


@router.get("/api/trading/web3/tokens")
def api_tokens(
    chain_id: int = Query(137, description="Chain ID"),
    q: str = Query("", description="Search query"),
):
    """Search tokens on a specific chain."""
    tokens = w3.search_tokens(chain_id, q)
    return JSONResponse({"ok": True, "tokens": tokens})


@router.get("/api/trading/web3/price")
def api_price(
    chain_id: int = Query(...),
    sell_token: str = Query(..., alias="sell"),
    buy_token: str = Query(..., alias="buy"),
    sell_amount: str = Query(..., alias="amount"),
    taker: str = Query("", description="Taker wallet address"),
):
    """Get a swap price indication (no calldata)."""
    result = w3.get_swap_price(chain_id, sell_token, buy_token, sell_amount, taker)
    return JSONResponse(result)


@router.post("/api/trading/web3/quote")
def api_quote(req: SwapQuoteRequest):
    """Get a full swap quote with transaction calldata for MetaMask."""
    result = w3.get_swap_quote(
        chain_id=req.chain_id,
        sell_token=req.sell_token,
        buy_token=req.buy_token,
        sell_amount=req.sell_amount,
        taker_address=req.taker_address,
        slippage_bps=req.slippage_bps,
    )
    return JSONResponse(result)
