"""One-shot recovery: re-apply the place_stop_loss_sell_order method
insertion to robinhood_spot.py from a clean HEAD checkout. Edit-tool
truncated the file at line 763 mid-call. Same recovery pattern as
Round 17/18/21 — write via Python with ast.parse validation.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "trading" / "venue" / "robinhood_spot.py"

head = subprocess.check_output(
    ['git', 'show', 'HEAD:app/services/trading/venue/robinhood_spot.py'],
    cwd=str(ROOT),
).decode('utf-8')

NEW_METHOD = '''    def place_stop_loss_sell_order(
        self,
        *,
        product_id: str,
        base_size: str,
        trigger_price: str,
        client_order_id: Optional[str] = None,
        market_hours_override: Optional[str] = None,
        extended_hours_override: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Place a server-side STOP-LOSS SELL order on Robinhood equities.

        The order rests at the broker and triggers (becomes a market
        order) when the last trade prints at or below ``trigger_price``.
        This is the protective primitive used by the Phase G.2 bracket
        writer to repair ``missing_stop`` reconciliation findings.

        Symbol convention: plain stock ticker (``AAPL``); a trailing
        ``-USD`` suffix is stripped for parity with ``place_limit_order_gtc``.

        Returns the same envelope shape as the other place_* methods so
        callers can dispatch uniformly:
            {"ok": True,  "order_id": "...", "client_order_id": "...", "raw": {...}}
            {"ok": False, "error": "...", "client_order_id": "..."}
        """
        ticker = _to_ticker(product_id)
        qty = float(base_size)
        trigger = float(trigger_price)

        if idempotency_store.is_duplicate(client_order_id, venue=_VENUE):
            return {
                "ok": False,
                "error": "duplicate_client_order_id",
                "client_order_id": client_order_id,
            }

        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 - record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=ticker, source="rh_place_stop_loss",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(
                _VENUE, retry_after, client_order_id=client_order_id
            )

        # P1.1 - SUBMITTING state before broker call.
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="rh_place_stop_loss",
                client_order_id=client_order_id,
                raw_payload={
                    "ticker": ticker,
                    "side": "sell",
                    "qty": qty,
                    "trigger_price": trigger,
                    "kind": "stop_loss_market",
                },
            )
        except Exception:
            pass

        from ...broker_service import place_sell_stop_loss_order

        result = place_sell_stop_loss_order(
            ticker,
            qty,
            trigger_price=trigger,
            market_hours_override=market_hours_override,
            extended_hours_override=extended_hours_override,
        )

        if result.get("ok") and client_order_id:
            idempotency_store.remember(
                client_order_id,
                venue=_VENUE,
                symbol=ticker,
                side="sell",
                qty=qty,
                broker_order_id=result.get("order_id") or None,
                status="submitted",
            )

        if result.get("ok"):
            oid = result.get("order_id", "") or None
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.ACK,
                    venue=_VENUE,
                    source="rh_place_stop_loss",
                    order_id=oid,
                    client_order_id=client_order_id,
                    broker_status="accepted",
                    raw_payload={
                        "ticker": ticker,
                        "order_id": oid,
                        "trigger_price": trigger,
                    },
                )
            except Exception:
                pass
            return {
                "ok": True,
                "order_id": result.get("order_id", ""),
                "client_order_id": client_order_id,
                "raw": result.get("raw", {}),
            }
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.REJECTED,
                venue=_VENUE,
                source="rh_place_stop_loss",
                client_order_id=client_order_id,
                broker_status="rejected",
                raw_payload={
                    "ticker": ticker,
                    "trigger_price": trigger,
                    "error": str(result.get("error") or ""),
                },
            )
        except Exception:
            pass
        return result

'''

# Anchor: insert immediately before "    def cancel_order(self, order_id: str)"
anchor = "    def cancel_order(self, order_id: str)"
idx = head.find(anchor)
if idx == -1:
    print("ANCHOR NOT FOUND"); sys.exit(1)
print(f"anchor at byte {idx}")

# Find start of the line to insert before
line_start = head.rfind('\n', 0, idx) + 1
new_content = head[:line_start] + NEW_METHOD + head[line_start:]

try:
    ast.parse(new_content)
    print("ast OK")
except SyntaxError as e:
    print(f"SYNTAX: {e}")
    lines = new_content.split('\n')
    for i in range(max(0, e.lineno - 5), min(len(lines), e.lineno + 3)):
        print(f"{i+1}: {lines[i][:120]}")
    sys.exit(1)

with open(target, 'w', encoding='utf-8', newline='\n') as f:
    f.write(new_content)
print(f"wrote {len(new_content.splitlines())} lines to {target}")
