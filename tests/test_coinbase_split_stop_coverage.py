"""Coinbase split stop-coverage guards.

Coinbase can hold multiple resting stop-limit sells against one spot
position. CHILI's local bracket row stores a single broker_stop_order_id, so
writer/reconciler code must look at aggregate broker stop coverage before
placing another stop. Otherwise partial stop coverage looks like missing_stop
forever and the writer keeps over-covering the same inventory.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.trading import bracket_writer_g2 as bw


RECON_PATH = (
    Path(__file__).parent.parent
    / "app"
    / "services"
    / "trading"
    / "bracket_reconciliation_service.py"
)


def test_coinbase_stop_order_base_size_reads_stop_limit_config() -> None:
    order = SimpleNamespace(
        raw={
            "order_configuration": {
                "stop_limit_stop_limit_gtc": {
                    "base_size": "9442.1",
                    "stop_price": "0.01493",
                }
            }
        }
    )
    assert bw._coinbase_stop_order_base_size(order) == 9442.1


def test_coinbase_open_stop_order_filter_sums_only_working_sell_stops() -> None:
    orders = [
        SimpleNamespace(
            product_id="THQ-USD",
            side="sell",
            order_type="STOP_LIMIT",
            status="OPEN",
            raw={"order_configuration": {"x": {"base_size": "4.5"}}},
        ),
        SimpleNamespace(
            product_id="THQ-USD",
            side="buy",
            order_type="STOP_LIMIT",
            status="OPEN",
            raw={"order_configuration": {"x": {"base_size": "99"}}},
        ),
        SimpleNamespace(
            product_id="THQ-USD",
            side="sell",
            order_type="LIMIT",
            status="OPEN",
            raw={"order_configuration": {"x": {"base_size": "99"}}},
        ),
        SimpleNamespace(
            product_id="OTHER-USD",
            side="sell",
            order_type="STOP_LIMIT",
            status="OPEN",
            raw={"order_configuration": {"x": {"base_size": "99"}}},
        ),
    ]

    class Adapter:
        def list_open_orders(self, *, product_id=None, limit=100):
            return orders, None

    filtered = bw._coinbase_open_stop_orders_for_ticker(Adapter(), "THQ-USD")
    assert len(filtered) == 1
    assert sum(bw._coinbase_stop_order_base_size(o) for o in filtered) == 4.5


def test_reconciler_reads_coinbase_open_stop_orders() -> None:
    src = RECON_PATH.read_text(encoding="utf-8")
    assert 'get_adapter("coinbase")' in src
    assert "list_open_orders" in src
    assert "cb_stops_by_ticker" in src
    assert '"STOP"' in src

