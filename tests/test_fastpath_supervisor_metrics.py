import logging

from app.services.trading.fast_path.supervisor import FastPathSupervisor


def test_ws_metrics_log_surfaces_entry_and_exit_only_pairs(caplog):
    entry_pair = "RANKED-USD"
    exit_only_pair = "OPEN-ONLY-USD"
    supervisor = object.__new__(FastPathSupervisor)
    supervisor._status = object()
    supervisor._db_writer = object()
    supervisor._snapshot = lambda: {
        "writer": {},
        "status": {},
        "ws": {
            "entry_pairs": [entry_pair],
            "exit_only_subscription_pairs": [exit_only_pair],
            "alerts_suppressed_exit_only_subscription": 3,
        },
    }

    caplog.set_level(
        logging.INFO,
        logger="app.services.trading.fast_path.supervisor",
    )

    supervisor._emit_metrics()

    ws_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("[fast_path] ws raw_messages=")
    ]
    assert len(ws_lines) == 1
    assert "exit_only_suppressed=3" in ws_lines[0]
    assert f"entry_pairs=['{entry_pair}']" in ws_lines[0]
    assert f"exit_only_pairs=['{exit_only_pair}']" in ws_lines[0]
