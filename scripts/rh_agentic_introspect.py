"""Operator tool: introspect the Robinhood Agentic MCP server's live tool schema.

Run this AFTER you have:
  1. Connected + authenticated to the Robinhood Trading MCP, e.g.:
        claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
     then `/mcp` -> select robinhood-trading -> authenticate (desktop browser).
  2. Opened + funded a dedicated **Agentic account** (separate from your primary
     portfolio; the agent only touches funds deposited there).
  3. Made a bearer token available to THIS process:
        set CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN=<token>
     (or point CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE / settings at a token file).

It calls ``initialize`` + ``tools/list`` and writes the real tool names + input
schemas to ``logs/rh_agentic_tools.json`` so we can finalize ``robinhood_mcp.py``
against reality — no schema guessing. It does **not** place any order.

Usage:
    conda run -n chili-env python scripts/rh_agentic_introspect.py
    conda run -n chili-env python scripts/rh_agentic_introspect.py --out logs/rh_agentic_tools.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Bootstrap: allow running as `python scripts/rh_agentic_introspect.py` from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.services.trading.venue.rh_mcp_client import (  # noqa: E402
    RhMcpClient,
    RhMcpError,
    resolve_mcp_endpoint,
    resolve_mcp_token,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Introspect the Robinhood Agentic MCP tool schema.")
    parser.add_argument("--out", default="logs/rh_agentic_tools.json", help="output JSON path")
    parser.add_argument("--endpoint", default=None, help="override MCP endpoint URL")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds")
    args = parser.parse_args()

    endpoint = resolve_mcp_endpoint(args.endpoint)
    token = resolve_mcp_token()
    if not token:
        print(
            "[rh_agentic_introspect] ERROR: no token. Set CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN "
            "(or CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE) after authenticating to the RH Trading MCP.",
            file=sys.stderr,
        )
        return 2

    print(f"[rh_agentic_introspect] endpoint={endpoint}")
    client = RhMcpClient(endpoint=endpoint, token=token, timeout=args.timeout)
    try:
        client.connect(force=True)
    except RhMcpError as exc:
        print(f"[rh_agentic_introspect] connect failed: {exc} (code={exc.code})", file=sys.stderr)
        return 1

    info = client.server_info
    print(f"[rh_agentic_introspect] connected: serverInfo={info}")

    try:
        tools = client.list_tools()
    except RhMcpError as exc:
        print(f"[rh_agentic_introspect] tools/list failed: {exc} (code={exc.code})", file=sys.stderr)
        return 1

    summary = [
        {
            "name": t.get("name"),
            "description": (t.get("description") or "")[:200],
            "inputSchema": t.get("inputSchema"),
        }
        for t in tools
    ]
    payload = {"endpoint": endpoint, "serverInfo": info, "tool_count": len(tools), "tools": summary}

    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)

    print(f"[rh_agentic_introspect] {len(tools)} tools written to {out_path}")
    for t in summary:
        print(f"  - {t['name']}: {t['description']}")
    print(
        "\nNext: paste logs/rh_agentic_tools.json back so robinhood_mcp.py's tool mapping "
        "is finalized against the real schema (not guessed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
