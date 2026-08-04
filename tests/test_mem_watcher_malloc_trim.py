"""mem_watcher malloc_trim tick — the 2026-07-31 scheduler-worker 9.9GB
RSS incident fix. The trim must be default-ON, opt-out via env, and the
tick must never raise on platforms without glibc (Windows dev boxes,
musl images) where ``_libc`` is None.
"""
import logging

from app.services.diagnostics import mem_watcher


def test_tick_never_raises_and_logs_rss(caplog):
    prev_ref = [{}]
    with caplog.at_level(logging.INFO, logger="app.services.diagnostics.mem_watcher"):
        mem_watcher.run_memory_watcher_tick(prev_ref, log_prefix="[mem_test]")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "[mem_test]" in joined
    assert "vm_rss=" in joined
    # Delta state persisted for the next tick on the same process.
    assert prev_ref[0]


def test_malloc_trim_default_on(monkeypatch):
    monkeypatch.delenv("CHILI_MEM_WATCHER_MALLOC_TRIM", raising=False)
    assert mem_watcher._malloc_trim_enabled() is True


def test_malloc_trim_env_opt_out(monkeypatch):
    for off in ("0", "false", "No", " OFF "):
        monkeypatch.setenv("CHILI_MEM_WATCHER_MALLOC_TRIM", off)
        assert mem_watcher._malloc_trim_enabled() is False
    monkeypatch.setenv("CHILI_MEM_WATCHER_MALLOC_TRIM", "1")
    assert mem_watcher._malloc_trim_enabled() is True


def test_run_malloc_trim_never_raises():
    # On glibc Linux this trims and returns a log fragment; elsewhere
    # (_libc is None) it must return "" — either way, no exception.
    frag = mem_watcher._run_malloc_trim()
    assert isinstance(frag, str)
    if mem_watcher._libc is None:
        assert frag == ""
    else:
        assert frag.startswith("malloc_trim_")
