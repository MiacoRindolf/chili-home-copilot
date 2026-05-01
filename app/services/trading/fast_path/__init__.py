"""Fast-lane scalping package (F1+).

See ``docs/ARCHITECTURE-fast-path.md`` for the contract.

Phase F1 ships ingestion only:
* ``ws_client`` — Coinbase Advanced Trade WebSocket connection lifecycle
* ``bar_aggregator`` — consume the ``candles`` channel, emit closed bars
* ``db_writer`` — bounded queue + batched inserts into ``fast_snapshots``
* ``status_tracker`` — per-pair circuit breaker + ``fast_path_status``
* ``supervisor`` — asyncio task graph, health, graceful shutdown
* ``healthz`` — minimal HTTP /healthz endpoint for compose
"""
