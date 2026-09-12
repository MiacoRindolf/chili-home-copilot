"""PAPER transport identity/cancellation and incomplete exposure evidence."""
import itertools
import json
from threading import Event

import pytest

from app.services.trading.momentum_neural.paper_native_inventory_reader import PaperNativeInventoryReader, PAPER_BASE
from tests.test_broker_native_demand import ACCOUNT, asset, held, order


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, **kwargs):
        yield json.dumps(self.body).encode()


class Session:
    def __init__(self, replies):
        self.replies, self.calls, self.closed = list(replies), [], False
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.replies.pop(0)
        if isinstance(value, Exception): raise value
        return value if isinstance(value, Response) else Response(value)
    def close(self): self.closed = True


def reader(replies, **kwargs):
    session = Session(replies)
    result = PaperNativeInventoryReader(expected_account_id=ACCOUNT,
        api_key='test-key', api_secret='test-secret', paper=True, timeout_seconds=1,
        max_response_bytes=kwargs.pop('max_response_bytes', 100000),
        stop_event=kwargs.pop('stop_event', Event()), session=session,
        clock_ns=itertools.count(100).__next__, **kwargs)
    return result, session


def test_complete_two_asset_classes_and_bracketed_fractional_exposure_use_nine_paper_gets():
    account = {'id': ACCOUNT}
    r, s = reader([account, [asset(1, 'A')], [asset(2, 'BTC/USD', 'crypto')], account,
        account, [order(2, 'BTC/USD', 'crypto')], [held(2, 'BTCUSD', 'crypto')],
        [order(2, 'BTC/USD', 'crypto')], account])
    inventory, exposure = r.get_asset_inventory_probe(), r.get_coverage_inventory_probe()
    assert inventory.error is None and len(inventory.snapshot.assets) == 2
    assert all(v.complete for v in exposure.reads)
    assert len(s.calls) == 9
    for url, kw in s.calls:
        assert url.startswith(PAPER_BASE) and kw['allow_redirects'] is False and kw['stream'] is True
    assert [v[1]['params']['asset_class'] for v in s.calls if v[0].endswith('/assets')] == ['us_equity', 'crypto']
    r.close()
    assert s.closed


def test_final_account_mismatch_invalidates_both_exposure_reads():
    r, _ = reader([{'id': ACCOUNT}, [], [], [], {'id': 'different'}])
    assert all(not v.complete for v in r.get_coverage_inventory_probe().reads)


@pytest.mark.parametrize('reply', [Response([], 302), RuntimeError('SECRET'), Response('x'*500)])
def test_http_failure_never_becomes_successfully_empty_inventory(reply):
    r, _ = reader([reply], max_response_bytes=100)
    value = r.get_asset_inventory_probe()
    assert value.snapshot is None and value.error and 'SECRET' not in value.error


def test_stop_prevents_any_further_http_and_forbidden_paths_never_call_session():
    stop = Event()
    stop.set()
    r, s = reader([], stop_event=stop)
    assert r.get_asset_inventory_probe().error
    assert all(not v.complete for v in r.get_coverage_inventory_probe().reads)
    with pytest.raises(ValueError, match='path_forbidden'): r._get('orders/new')
    assert s.calls == []


def test_pending_change_preserves_incomplete_union():
    r, _ = reader([{'id': ACCOUNT}, [order(1, 'A')], [], [order(2, 'B', oid=101)], {'id': ACCOUNT}])
    pending = next(v for v in r.get_coverage_inventory_probe().reads if v.reason == 'pending')
    assert not pending.complete and len(pending.members) == 2
