"""Web3 service: 0x DEX aggregator integration for multi-chain token swaps.

Supports Ethereum, Polygon, BSC, Arbitrum, and Base via the 0x Swap API.
The backend fetches quotes / prices / token lists; the frontend handles
MetaMask signing and transaction submission.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from ...config import settings

logger = logging.getLogger(__name__)

# ── Chain configuration ──────────────────────────────────────────────

CHAINS: dict[int, dict[str, str]] = {
    1:     {"name": "Ethereum", "zerox": "https://api.0x.org",           "symbol": "ETH",  "explorer": "https://etherscan.io",    "rpc": "https://eth.llamarpc.com"},
    137:   {"name": "Polygon",  "zerox": "https://polygon.api.0x.org",   "symbol": "POL",  "explorer": "https://polygonscan.com", "rpc": "https://polygon-rpc.com"},
    56:    {"name": "BSC",      "zerox": "https://bsc.api.0x.org",       "symbol": "BNB",  "explorer": "https://bscscan.com",     "rpc": "https://bsc-dataseed.binance.org"},
    42161: {"name": "Arbitrum", "zerox": "https://arbitrum.api.0x.org",  "symbol": "ETH",  "explorer": "https://arbiscan.io",     "rpc": "https://arb1.arbitrum.io/rpc"},
    8453:  {"name": "Base",     "zerox": "https://base.api.0x.org",      "symbol": "ETH",  "explorer": "https://basescan.org",    "rpc": "https://mainnet.base.org"},
}

NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# ── Rate limiter (2 req/s to 0x) ─────────────────────────────────────

_zerox_lock = threading.Lock()
_zerox_last_call = 0.0


def _rate_limit_zerox() -> None:
    global _zerox_last_call
    with _zerox_lock:
        now = time.time()
        elapsed = now - _zerox_last_call
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)
        _zerox_last_call = time.time()


def _zerox_headers() -> dict[str, str]:
    key = settings.zerox_api_key
    if not key:
        return {}
    return {"0x-api-key": key}


# ── Token list cache ─────────────────────────────────────────────────

_token_cache: dict[int, tuple[float, list[dict]]] = {}
_TOKEN_CACHE_TTL = 3600

_POPULAR_TOKENS: dict[int, list[dict[str, str]]] = {
    1: [
        {"symbol": "ETH",  "name": "Ether",     "address": NATIVE_TOKEN, "decimals": "18"},
        {"symbol": "USDC", "name": "USD Coin",   "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": "6"},
        {"symbol": "USDT", "name": "Tether",     "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": "6"},
        {"symbol": "WBTC", "name": "Wrapped BTC", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "decimals": "8"},
        {"symbol": "WETH", "name": "Wrapped ETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": "18"},
        {"symbol": "DAI",  "name": "Dai",        "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "decimals": "18"},
        {"symbol": "LINK", "name": "Chainlink",  "address": "0x514910771AF9Ca656af840dff83E8264EcF986CA", "decimals": "18"},
        {"symbol": "UNI",  "name": "Uniswap",    "address": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", "decimals": "18"},
        {"symbol": "AAVE", "name": "Aave",       "address": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", "decimals": "18"},
        {"symbol": "SHIB", "name": "Shiba Inu",  "address": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", "decimals": "18"},
        {"symbol": "PEPE", "name": "Pepe",       "address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933", "decimals": "18"},
    ],
    137: [
        {"symbol": "POL",   "name": "Polygon",    "address": NATIVE_TOKEN, "decimals": "18"},
        {"symbol": "USDC",  "name": "USD Coin",   "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": "6"},
        {"symbol": "USDT",  "name": "Tether",     "address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "decimals": "6"},
        {"symbol": "WETH",  "name": "Wrapped ETH", "address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", "decimals": "18"},
        {"symbol": "WBTC",  "name": "Wrapped BTC", "address": "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", "decimals": "8"},
        {"symbol": "AAVE",  "name": "Aave",       "address": "0xD6DF932A45C0f255f85145f286eA0b292B21C90B", "decimals": "18"},
        {"symbol": "LINK",  "name": "Chainlink",  "address": "0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", "decimals": "18"},
    ],
    56: [
        {"symbol": "BNB",  "name": "BNB",         "address": NATIVE_TOKEN, "decimals": "18"},
        {"symbol": "USDT", "name": "Tether",      "address": "0x55d398326f99059fF775485246999027B3197955", "decimals": "18"},
        {"symbol": "USDC", "name": "USD Coin",    "address": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "decimals": "18"},
        {"symbol": "WETH", "name": "Wrapped ETH", "address": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8", "decimals": "18"},
        {"symbol": "BTCB", "name": "Bitcoin BEP2","address": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", "decimals": "18"},
        {"symbol": "CAKE", "name": "PancakeSwap", "address": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", "decimals": "18"},
    ],
    42161: [
        {"symbol": "ETH",  "name": "Ether",       "address": NATIVE_TOKEN, "decimals": "18"},
        {"symbol": "USDC", "name": "USD Coin",    "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": "6"},
        {"symbol": "USDT", "name": "Tether",      "address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": "6"},
        {"symbol": "WBTC", "name": "Wrapped BTC", "address": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", "decimals": "8"},
        {"symbol": "ARB",  "name": "Arbitrum",    "address": "0x912CE59144191C1204E64559FE8253a0e49E6548", "decimals": "18"},
        {"symbol": "GMX",  "name": "GMX",         "address": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a", "decimals": "18"},
    ],
    8453: [
        {"symbol": "ETH",  "name": "Ether",       "address": NATIVE_TOKEN, "decimals": "18"},
        {"symbol": "USDC", "name": "USD Coin",    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": "6"},
        {"symbol": "WETH", "name": "Wrapped ETH", "address": "0x4200000000000000000000000000000000000006", "decimals": "18"},
        {"symbol": "cbETH","name": "Coinbase ETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": "18"},
    ],
}


def get_token_list(chain_id: int) -> list[dict]:
    """Return a list of popular tokens for a chain (cached)."""
    now = time.time()
    if chain_id in _token_cache:
        ts, tokens = _token_cache[chain_id]
        if now - ts < _TOKEN_CACHE_TTL:
            return tokens
    tokens = _POPULAR_TOKENS.get(chain_id, [])
    _token_cache[chain_id] = (now, tokens)
    return tokens


def search_tokens(chain_id: int, query: str) -> list[dict]:
    """Search tokens by symbol or name (case-insensitive)."""
    q = query.lower().strip()
    if not q:
        return get_token_list(chain_id)[:20]
    tokens = get_token_list(chain_id)
    return [t for t in tokens if q in t["symbol"].lower() or q in t["name"].lower()][:20]


# ── 0x Swap API ──────────────────────────────────────────────────────

def get_swap_price(
    chain_id: int,
    sell_token: str,
    buy_token: str,
    sell_amount: str,
    taker_address: str = "",
) -> dict[str, Any]:
    """Get a swap price indication (no calldata, lighter API call)."""
    chain = CHAINS.get(chain_id)
    if not chain:
        return {"ok": False, "error": f"Unsupported chain {chain_id}"}

    _rate_limit_zerox()
    try:
        params: dict[str, Any] = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": sell_amount,
            "chainId": chain_id,
        }
        if taker_address:
            params["takerAddress"] = taker_address

        resp = requests.get(
            f"{chain['zerox']}/swap/permit2/price",
            params=params,
            headers=_zerox_headers(),
            timeout=15,
        )
        data = resp.json()
        if resp.status_code != 200:
            return {"ok": False, "error": data.get("reason", resp.text)[:200]}

        return {
            "ok": True,
            "buyAmount": data.get("buyAmount", "0"),
            "sellAmount": data.get("sellAmount", "0"),
            "price": data.get("price", "0"),
            "estimatedGas": data.get("estimatedGas", "0"),
            "gasPrice": data.get("gasPrice", "0"),
            "sources": data.get("sources", []),
            "buyTokenAddress": buy_token,
            "sellTokenAddress": sell_token,
        }
    except Exception as e:
        logger.warning(f"[web3] 0x price failed: {e}")
        return {"ok": False, "error": str(e)[:200]}


def get_swap_quote(
    chain_id: int,
    sell_token: str,
    buy_token: str,
    sell_amount: str,
    taker_address: str,
    slippage_bps: int = 50,
) -> dict[str, Any]:
    """Get a full swap quote with calldata ready for MetaMask signing."""
    chain = CHAINS.get(chain_id)
    if not chain:
        return {"ok": False, "error": f"Unsupported chain {chain_id}"}
    if not taker_address:
        return {"ok": False, "error": "taker_address required"}

    _rate_limit_zerox()
    try:
        slippage_pct = slippage_bps / 10000.0
        params: dict[str, Any] = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": sell_amount,
            "takerAddress": taker_address,
            "slippagePercentage": str(slippage_pct),
            "chainId": chain_id,
        }

        resp = requests.get(
            f"{chain['zerox']}/swap/permit2/quote",
            params=params,
            headers=_zerox_headers(),
            timeout=20,
        )
        data = resp.json()
        if resp.status_code != 200:
            return {"ok": False, "error": data.get("reason", resp.text)[:200]}

        result: dict[str, Any] = {
            "ok": True,
            "to": data.get("to", ""),
            "data": data.get("data", ""),
            "value": data.get("value", "0"),
            "gas": data.get("gas", data.get("estimatedGas", "0")),
            "gasPrice": data.get("gasPrice", "0"),
            "buyAmount": data.get("buyAmount", "0"),
            "sellAmount": data.get("sellAmount", "0"),
            "price": data.get("price", "0"),
            "minimumBuyAmount": data.get("minBuyAmount", data.get("guaranteedPrice", "0")),
            "sources": data.get("sources", []),
            "allowanceTarget": data.get("allowanceTarget", ""),
        }

        if "permit2" in data:
            result["permit2"] = data["permit2"]
        if "transaction" in data:
            result["transaction"] = data["transaction"]

        return result
    except Exception as e:
        logger.warning(f"[web3] 0x quote failed: {e}")
        return {"ok": False, "error": str(e)[:200]}


def get_supported_chains() -> list[dict[str, Any]]:
    """Return the list of supported chains with metadata."""
    return [
        {
            "chainId": cid,
            "name": info["name"],
            "symbol": info["symbol"],
            "explorer": info["explorer"],
            "rpc": info["rpc"],
        }
        for cid, info in CHAINS.items()
    ]
