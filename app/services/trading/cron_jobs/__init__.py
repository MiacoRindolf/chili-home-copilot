"""Cron-driven jobs for the trading brain.

Jobs in this package are timer-driven (APScheduler) rather than
event-driven (brain_work_events). Used when the natural trigger is
"every N hours / weekly" rather than "this thing happened."

Each job module exports a single ``run_*`` function that takes a
``Session`` and returns a dict for log visibility. The wrapper that
opens the session + handles errors lives in ``trading_scheduler.py``.
"""
