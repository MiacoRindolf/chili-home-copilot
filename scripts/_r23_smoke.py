"""Smoke test for R23 changes — invoked from dispatch validate."""
import inspect
from app.services.trading import bracket_writer_g2 as g2
from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter
from app.config import settings

print("=== writer module ===")
print("__all__:", g2.__all__)

print("=== adapter ===")
print("place_stop_loss_sell_order present:", hasattr(RobinhoodSpotAdapter, "place_stop_loss_sell_order"))
sig = inspect.signature(RobinhoodSpotAdapter.place_stop_loss_sell_order)
print("signature:", sig)

print("=== config flags ===")
print("chili_bracket_sweep_writer_enabled:", settings.chili_bracket_sweep_writer_enabled)
print("chili_bracket_writer_g2_enabled:", settings.chili_bracket_writer_g2_enabled)
print("chili_bracket_writer_g2_place_missing_stop:", settings.chili_bracket_writer_g2_place_missing_stop)
print("chili_bracket_writer_g2_partial_fill_resize:", settings.chili_bracket_writer_g2_partial_fill_resize)
print("brain_live_brackets_mode:", settings.brain_live_brackets_mode)

print("=== sweep service ===")
from app.services.trading import bracket_reconciliation_service as svc
print("_invoke_writer_for_decision present:", hasattr(svc, "_invoke_writer_for_decision"))

print("=== broker_service primitive ===")
from app.services import broker_service
print("place_sell_stop_loss_order present:", hasattr(broker_service, "place_sell_stop_loss_order"))

print("=== mig 214 registered ===")
from app import migrations as M
ids = [m[0] for m in M.MIGRATIONS]
print("count:", len(ids))
print("last 5:", ids[-5:])
print("214 present:", any(m.startswith("214_") for m in ids))

print("smoke OK")
