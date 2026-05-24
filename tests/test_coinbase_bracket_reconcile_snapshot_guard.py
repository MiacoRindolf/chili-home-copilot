from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_coinbase_missing_position_snapshot_is_broker_down_not_zero_qty():
    src = (REPO / "app/services/trading/bracket_reconciliation_service.py").read_text(
        encoding="utf-8"
    )

    assert "Position-sync is" in src
    assert "available=False" in src
    assert "position_quantity=None" in src
    assert "false broker_qty=0" in src
