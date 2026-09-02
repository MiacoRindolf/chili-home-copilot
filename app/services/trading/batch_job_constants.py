"""`brain_batch_jobs.job_type` values for scheduler + scan persistence."""

JOB_CRYPTO_BREAKOUT_SCANNER = "crypto_breakout_scanner"
JOB_STOCK_BREAKOUT_SCANNER = "stock_breakout_scanner"
JOB_MOMENTUM_SCANNER = "momentum_scanner"
JOB_PATTERN_IMMINENT_SCANNER = "pattern_imminent_scanner"
JOB_SCHEDULER_WORKER_HEARTBEAT = "scheduler_worker_heartbeat"
JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT = "momentum_live_loop_heartbeat"
JOB_IQFEED_EXACT_PRINT_HEARTBEAT = "iqfeed_exact_print_heartbeat"
IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA = "iqfeed_exact_print_heartbeat_v1"
IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE = "committed_exact_print_release"
# Separate job type ON PURPOSE (2026-09-02): the exact-print heartbeat body is
# matched key-set-exactly by lane_health and its content_sha256 covers the body,
# so it is effectively frozen. Drain telemetry -- above all the age of the
# OLDEST UNWRITTEN arrival, which no committed-row receipt can express -- gets
# its own job type and its own frozen key set instead.
JOB_IQFEED_DRAIN_METRICS_HEARTBEAT = "iqfeed_drain_metrics_heartbeat"
IQFEED_DRAIN_METRICS_HEARTBEAT_SCHEMA = "iqfeed_drain_metrics_heartbeat_v1"
IQFEED_DRAIN_METRICS_HEARTBEAT_SCOPE = "writer_drain_window"
JOB_BRAIN_MARKET_SNAPSHOTS = "brain_market_snapshots"
