"""Durable capture can resume a token without inventing a complete universe."""
import itertools
import json

import pytest

from scripts.capture_crypto_history_pages import HistoryCapture, capture_lock, collect
from scripts.crypto_history_pages import canonical, sha
from tests.test_crypto_history_pages import request, page, trade, END


def capture(path):
    return HistoryCapture(path,request(),max_pages=10,max_trades=20,max_page_bytes=10000)


def record(c,raw,status=200,start=END+1):
    return c.record_response(raw=raw,status=status,started_ns=start,received_ns=start+1)


def test_raw_chain_restores_and_retries_only_failed_get_token(tmp_path):
    first=capture(tmp_path)
    record(first,page({'A/USD':[trade()]},'a'))
    record(first,'{"message":"temporary error"}',503,start=END+3)
    restored=capture(tmp_path)
    assert restored.receipt()==first.receipt()
    calls=[]
    def get(url,params):
        calls.append(params)
        return 200,page({'C/BTC':[trade(2)]}),{}
    result=collect(restored,get,max_wall_seconds=10,clock_ns=itertools.count(END+5).__next__)
    assert calls[0]['page_token']=='a' and len(calls)==1
    assert result['provider_page_chain_exhausted'] and result['trade_counts']['C/BTC']==1
    assert capture(tmp_path).receipt()==restored.receipt()


def test_corrupt_or_missing_earlier_raw_response_cannot_resume_from_token(tmp_path):
    c=capture(tmp_path)
    record(c,page({'A/USD':[trade()]},'a'))
    raw=(tmp_path/'responses.jsonl').read_text().replace('1.123456789123456789','999')
    (tmp_path/'responses.jsonl').write_text(raw)
    with pytest.raises(ValueError,match='record_mismatch'): capture(tmp_path)


def test_torn_record_never_silently_discarded(tmp_path):
    c=capture(tmp_path)
    record(c,page({},'a'))
    with (tmp_path/'responses.jsonl').open('a') as f: f.write('{"partial":')
    with pytest.raises(ValueError,match='torn'): capture(tmp_path)


def test_bad_success_payload_is_retained_before_decoder_failure(tmp_path):
    c=capture(tmp_path)
    with pytest.raises(ValueError): record(c,'{"trades":{},"next_page_token":""}')
    assert len((tmp_path/'responses.jsonl').read_text().splitlines())==1
    assert not c.walk.complete and c.walk.page_count==0
    with pytest.raises(ValueError): capture(tmp_path)


def test_request_change_is_not_a_resume(tmp_path):
    capture(tmp_path)
    with pytest.raises(ValueError,match='binding_changed'):
        HistoryCapture(tmp_path,request(end_ns=END+1),max_pages=10,max_trades=20,max_page_bytes=10000)


def test_transport_budget_does_not_advance_capture_end_or_certify_empty_symbols(tmp_path):
    c=capture(tmp_path)
    timer=iter([1,2]).__next__
    result=collect(c,lambda *a: pytest.fail('no HTTP budget'),max_wall_seconds=1,monotonic=timer)
    assert result['reason']=='capture_wall_resource_limit'
    assert not result['provider_page_chain_exhausted'] and result['capture_end_ns']==END


def test_owner_lock_is_released_but_concurrent_owner_is_refused(tmp_path):
    with capture_lock(tmp_path):
        with pytest.raises(OSError):
            with capture_lock(tmp_path): pytest.fail('second owner')
    with capture_lock(tmp_path): pass


def test_record_order_or_parameters_cannot_be_swapped_without_chain_failure(tmp_path):
    c=capture(tmp_path)
    record(c,page({},'a'))
    record(c,page({}),start=END+3)
    lines=(tmp_path/'responses.jsonl').read_text().splitlines(True)
    (tmp_path/'responses.jsonl').write_text(''.join(reversed(lines)))
    with pytest.raises(ValueError,match='record_mismatch'): capture(tmp_path)


@pytest.mark.parametrize('status', [True, '200', 99, 600])
def test_restore_revalidates_status_even_with_self_consistent_hash(tmp_path, status):
    c=capture(tmp_path)
    record(c,page({}))
    path=tmp_path/'responses.jsonl'
    row=json.loads(path.read_text())
    row['status']=status
    row.pop('record_sha256')
    row['record_sha256']=sha(canonical(row))
    path.write_text(canonical(row)+'\n')
    with pytest.raises(ValueError,match='response_status_invalid'): capture(tmp_path)
