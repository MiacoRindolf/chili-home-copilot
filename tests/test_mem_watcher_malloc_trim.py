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


def test_watcher_never_retains_heap_objects_or_forces_collection(monkeypatch,caplog):
    calls=[]
    def forbidden():
        calls.append('unsafe_heap_probe')
        raise AssertionError('background diagnostics must not retain in-flight tuples')
    monkeypatch.setattr(mem_watcher._gc,'get_objects',forbidden)
    monkeypatch.setattr(mem_watcher._gc,'collect',forbidden)
    monkeypatch.setattr(mem_watcher._gc,'get_stats',lambda:[dict(collections=7,collected=20,uncollectable=0)])
    monkeypatch.setattr(mem_watcher._gc,'get_count',lambda:(3,2,1))
    previous=[dict(collections=5,collected=17,uncollectable=0)]
    with caplog.at_level(logging.INFO,logger=mem_watcher.__name__):
        mem_watcher.run_memory_watcher_tick(previous)
    assert calls==[]
    assert previous==[dict(collections=7,collected=20,uncollectable=0)]
    message=caplog.records[-1].getMessage()
    assert 'heap_census=omitted' in message and 'gc_allocation_counts=(3, 2, 1)' in message
    assert "gc_delta_since_last={'collections': 2, 'collected': 3, 'uncollectable': 0}" in message
    assert 'py_objects=' not in message


def test_watcher_during_inflight_tuple_construction():
    """The watcher can run while a different thread builds a tuple."""
    import threading
    paused=threading.Event();resume=threading.Event();outcome=[];errors=[]
    def items():
        yield object()
        paused.set()
        assert resume.wait(5)
        yield object()
    def build():
        try:outcome.append(tuple(items()))
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=build)
    thread.start()
    try:
        assert paused.wait(5)
        mem_watcher.run_memory_watcher_tick([{}])
    finally:
        resume.set();thread.join(5)
    assert not thread.is_alive() and not errors and len(outcome[0])==2
