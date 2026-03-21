# Brain worker vs HTTPS app

- **HTTPS app**: FastAPI/Uvicorn (e.g. `scripts/start-https.ps1`). Serves the UI and APIs.
- **Brain worker**: Separate process: `python scripts/brain_worker.py` (or **Start** on the Brain page). Runs full `run_learning_cycle` in a loop, including the backtest queue step.

Both must use the **same `DATABASE_URL`** (same `.env`).

## Buttons

| Control | What it does |
|--------|----------------|
| **Run next cycle** | Queues a wake in PostgreSQL (and a legacy file). Only `brain_worker.py` consumes it to **skip idle sleep** between full cycles. Does **not** run work inside the web server. |
| **Process queue on server** | Runs one `_auto_backtest_from_queue` batch via `POST /api/trading/brain/worker/run-queue-batch` inside the web process. **No worker required.** |

## Pending / queue

Dashboard “pending” counts **eligible** patterns: active and (boosted, or never backtested, or last backtest older than the retest interval). Use **Eligible queue pattern IDs** on the Brain page to debug.

## Remote brain service

If `CHILI_USE_BRAIN_SERVICE` is set for the worker, long cycles may run on the configured Brain HTTP service; check that service’s logs for queue progress.

## Local “deploy”

There is no separate cloud deploy in this flow: restart the HTTPS server locally after pulling code changes. Ensure migration `038` (and later) applied so `brain_worker_control` has `stop_requested` / `last_heartbeat_at` if you use DB wake/stop/heartbeat.
