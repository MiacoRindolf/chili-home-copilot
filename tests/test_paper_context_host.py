"""Owned worker lifecycle, actual journal wakeups and durable handoff recovery."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import time
import zipfile

import pytest
import sqlalchemy as sa

from app.services.trading.momentum_neural import paper_context_host as host
from app.services.trading.momentum_neural.paper_context_host_config import load_config, load_provider_catalog
from app.services.trading.momentum_neural import broker_native_demand_store as store
from app.services.trading.momentum_neural import structural_context_journal as journal
from app.services.trading.momentum_neural import structural_context_recovery as recovery
from app.services.trading.momentum_neural.current_structural_context import read_observation
from tests.test_ordinary_structural_context import source, packet, publish
from tests.test_native_iqfeed_mapping import text, listing, broker
from tests.test_broker_native_demand import ACCOUNT, inventory, coverage


def config_file(tmp_path):
    archive = tmp_path/'catalog.zip'
    raw = text([listing('A'), listing('B')])
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('mktsymbols_v2.txt', raw)
    value = dict(contract='paper_shared_tick_host_v1',
        publisher=dict(limits=dict(retained_ticks=100, frontier_ticks=100, active_references=100),
            max_symbols=5, max_trade_rows=100, max_payload_bytes=2_000_000,
            max_input_bytes=2_000_000, max_replay_inputs=100),
        catalog=dict(archive_path=str(archive.resolve()), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            observed_ns=1000, max_archive_bytes=archive.stat().st_size, max_source_bytes=len(raw), max_equities=2),
        native_checkpoint_bytes=2_000_000, http_response_bytes=2_000_000, process_rss_limit_bytes=2**50,
        native_refresh_wait_seconds=100, publisher_idle_wait_seconds=100,
        http_timeout_seconds=1, shutdown_wait_seconds=3,
        resource_bindings={'test':'Synthetic fixture allocation; never a strategy horizon.'})
    path = tmp_path/'host.json'
    path.write_text(json.dumps(value))
    return path, value


def settings(path=''):
    return SimpleNamespace(chili_scheduler_role='momentum_exec_only',
        chili_momentum_equity_execution_via_alpaca_paper=True,
        chili_momentum_crypto_execution_via_alpaca_paper=False,
        chili_alpaca_paper=True, chili_alpaca_expected_account_id=ACCOUNT,
        chili_alpaca_api_key='test-key', chili_alpaca_api_secret='test-secret',
        chili_momentum_shared_tick_host_config_path=str(path))


def eventually(check, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        value = check()
        if value: return value
        time.sleep(.01)  # Test orchestration only.
    raise AssertionError('worker condition did not arrive')


@pytest.fixture
def database(source):
    with source.begin() as c:
        store.create_schema(c)
        journal.create_schema(c)
        recovery.create_schema(c)
    return source


class Reader:
    calls = 0
    closed = 0
    def __init__(self, **kwargs): pass
    def get_asset_inventory_probe(self):
        type(self).calls += 1
        return inventory([broker(1, 'A'), broker(2, 'B')])
    def get_coverage_inventory_probe(self): return coverage()
    def close(self): type(self).closed += 1


def test_catalog_checks_full_digest_capacity_and_cancellation(tmp_path):
    path, _ = config_file(tmp_path)
    cfg = load_config(path)
    assert [x.symbol for x in load_provider_catalog(cfg.catalog, Event()).equities] == ['A', 'B']
    with pytest.raises(ValueError, match='archive_capacity'):
        load_provider_catalog(replace(cfg.catalog, max_archive_bytes=1), Event())
    with pytest.raises(ValueError, match='archive_changed'):
        load_provider_catalog(replace(cfg.catalog, archive_sha256='0'*64), Event())
    stopped = Event(); stopped.set()
    with pytest.raises(ValueError, match='stopped'): load_provider_catalog(cfg.catalog, stopped)


@pytest.mark.parametrize('field,value', [('native_checkpoint_bytes', True), ('publisher_idle_wait_seconds', 0),
    ('process_rss_limit_bytes', -1), ('http_timeout_seconds', float('nan')), ('resource_bindings', {})])
def test_invalid_resource_configuration_cannot_start(tmp_path, field, value):
    path, config = config_file(tmp_path)
    config[field] = value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError): load_config(path)


def test_duplicate_configuration_keys_rejected(tmp_path):
    path, _ = config_file(tmp_path)
    path.write_text(path.read_text().replace('"contract":', '"contract": "duplicate", "contract":'))
    with pytest.raises(ValueError, match='duplicate'): load_config(path)


def test_shutdown_before_deferred_start_prevents_resurrection(monkeypatch):
    monkeypatch.delenv('CHILI_PYTEST')
    life = host.HostLifecycle()
    life.close()
    life.start(None, settings())
    assert life.host is None and life.status()['state'] == 'stopped'


def test_missing_config_visible_and_test_mode_has_no_workers(monkeypatch):
    life = host.HostLifecycle()
    life.start(None, settings())
    assert life.status()['state'] == 'not_applicable' and life.host is None
    monkeypatch.delenv('CHILI_PYTEST')
    life.start(None, settings())
    assert life.status()['state'] == 'waiting_configuration' and life.host is None


def test_nonpaper_host_refuses_before_workers(tmp_path):
    path, _ = config_file(tmp_path)
    cfg = settings(); cfg.chili_alpaca_paper = False
    with pytest.raises(ValueError, match='requires_paper'):
        host.PaperContextHost(None, cfg, load_config(path))


def test_source_notification_and_local_stop_interrupt_long_idle(database):
    stop = Event()
    wake = host.SourceWake(database, stop)
    try:
        done = Event()
        thread = Thread(target=lambda: (wake.wait(100), done.set()))
        thread.start()
        with database.begin() as c:
            c.execute(sa.text("SELECT pg_notify('iqfeed_print_publications','untrusted wake only')"))
        assert done.wait(2)
        thread.join()
        done.clear()
        thread = Thread(target=lambda: (wake.wait(100), done.set()))
        thread.start()
        stop.set(); wake.signal()
        assert done.wait(2)
        thread.join()
    finally: wake.close()


def test_real_workers_publish_every_observed_membership_and_source_then_restore(database, tmp_path, monkeypatch):
    path, _ = config_file(tmp_path)
    cfg = load_config(path)
    Reader.calls = Reader.closed = 0
    monkeypatch.setattr(host, 'PaperNativeInventoryReader', Reader)
    publish(database, packet(database, [('SEED', 1)]))
    first = host.PaperContextHost(database, settings(), cfg)
    first.start()
    try:
        eventually(lambda: first.status().get('publisher', {}).get('enrolled_symbols') == 2)
        assert Reader.calls == 1
        publish(database, packet(database, [('A', 10), ('B', 20), ('A', 12)], start=2))
        eventually(lambda: first.status().get('publisher', {}).get('source_cursor', {}).get('revision') == 2)
        before = first.status()
        assert before['order_authority'] is False
        assert before['subscriptions_dispatched'] is False and not before['broker_event_history_complete']
    finally:
        assert first.close()['workers_alive'] == []
    assert Reader.closed == 1
    second = host.PaperContextHost(database, settings(), cfg)
    second.start()
    try:
        eventually(lambda: second.status().get('publisher', {}).get('enrolled_symbols') == 2)
        assert second.status()['publisher']['startup_mode'] == 'restore'
        assert Reader.calls == 1  # Restored checkpoint delivered without a fake new observation.
        assert second.status()['publisher']['source_cursor'] == before['publisher']['source_cursor']
    finally: assert second.close()['workers_alive'] == []


def test_waiting_source_backpressures_native_and_shutdown_releases_owner(database, tmp_path, monkeypatch):
    path, _ = config_file(tmp_path)
    Reader.calls = 0
    monkeypatch.setattr(host, 'PaperNativeInventoryReader', Reader)
    w = host.PaperContextHost(database, settings(), load_config(path))
    w.start()
    try:
        eventually(lambda: w.status()['native'].get('revision') == 1)
        eventually(lambda: w.status()['publisher'].get('state') == 'waiting_source')
        assert Reader.calls == 1
        with database.connect() as c:
            assert c.execute(sa.text('SELECT revision FROM momentum_native_demand_checkpoints')).scalar_one() == 1
    finally: assert w.close()['workers_alive'] == []


def test_memory_capacity_stops_without_dropping_membership(database, tmp_path, monkeypatch):
    path, _ = config_file(tmp_path)
    monkeypatch.setattr(host, 'PaperNativeInventoryReader', Reader)
    w = host.PaperContextHost(database, settings(), replace(load_config(path), process_rss_limit_bytes=1))
    w.start()
    try:
        eventually(lambda: w.status()['state'] == 'failed')
        assert any(w.status()[name].get('reason') == 'shared_tick_host_process_rss_capacity'
                   for name in ('native', 'publisher'))
    finally: assert w.close()['workers_alive'] == []


def test_failed_state_cannot_be_erased_by_sibling_and_unstarted_threads_close(tmp_path, monkeypatch):
    path, _ = config_file(tmp_path)
    w = host.PaperContextHost(None, settings(), load_config(path))
    def failed_start(self): raise RuntimeError('thread resource unavailable')
    monkeypatch.setattr(Thread, 'start', failed_start)
    with pytest.raises(RuntimeError): w.start()
    w._set('state', 'observing')
    assert w.close()['state'] == 'failed' and not w.status()['workers_alive']
