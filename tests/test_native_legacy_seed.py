from dataclasses import replace
import json
import pytest
from app.crypto_execution.legacy_seed import load_sealed_seed
from app.crypto_execution.source_capture import NativeSourceCapture,publication
from scripts.crypto_history_pages import canonical,sha
from tests.test_native_crypto_source_capture import revisable,getter
from tests.test_native_crypto_history_source import BASE,q,t


def fixture(tmp_path):
    c,m=revisable(tmp_path)
    get,_=getter(trade_rows=[t(90)],quote_rows=[q(80)])
    c.observe(BASE+100,get)
    get,_=getter(trade_rows=[t(85,tid=2),t(90),t(150,tid=3)],quote_rows=[q(80),q(140)])
    c.observe(BASE+200,get)
    seal=dict(metadata_sha256=sha((tmp_path/'metadata.json').read_bytes()),record_root_sha256=c.root,
        record_count=c.record_count,journal_bytes=c.byte_count,revision=c.revision)
    return c,seal


def test_sealed_latest_full_prefix_rebuilds_once_and_preserves_late_rows(tmp_path,monkeypatch):
    c,seal=fixture(tmp_path);old=NativeSourceCapture._new_book;calls=[]
    def count(self):calls.append(1);return old(self)
    monkeypatch.setattr(NativeSourceCapture,'_new_book',count)
    seed=load_sealed_seed(tmp_path,seal)
    assert len(calls)==1
    assert [r.trade_id for r in seed.batch.trades]==[2,1,3]
    assert seed.publication_record['view']==publication(c.book) and seed.origin==seal


@pytest.mark.parametrize('fault',['old_bytes','truncated','root','metadata','math'])
def test_changed_seal_or_retained_evidence_is_rejected(tmp_path,fault):
    c,seal=fixture(tmp_path);path=tmp_path/'source.jsonl'
    if fault=='root':seal['record_root_sha256']='f'*64
    elif fault=='metadata':seal['metadata_sha256']='f'*64
    elif fault=='truncated':path.write_bytes(path.read_bytes()[:-1])
    else:
        rows=[json.loads(r) for r in path.read_bytes().splitlines()]
        if fault=='old_bytes':rows[0]['body']['observation']='changed'
        else:
            rows[-1]['body']['view']['source_sequence']+=1
            rows[-1]['root']=sha(canonical({k:v for k,v in rows[-1].items() if k!='root'}))
            seal['record_root_sha256']=rows[-1]['root']
        path.write_text('\n'.join(canonical(r) for r in rows)+'\n')
        seal['journal_bytes']=path.stat().st_size
    with pytest.raises(ValueError):load_sealed_seed(tmp_path,seal)


def test_unsealed_future_bytes_are_never_used_as_seed(tmp_path):
    c,seal=fixture(tmp_path)
    with (tmp_path/'source.jsonl').open('ab') as f:f.write(b'not a sealed record')
    seed=load_sealed_seed(tmp_path,seal)
    assert seed.batch.request.end_ns==BASE+200 and seed.origin['journal_bytes']==seal['journal_bytes']


def test_incomplete_sealed_attempt_keeps_only_last_committed_seed(tmp_path):
    c,seal=fixture(tmp_path)
    c._record(dict(kind='observation_started',observation='next',request={
        'location':'us','symbols':list(c.book.assets),'start_ns':BASE,'end_ns':BASE+300,
        'page_limit':100,'inventory_sha256':'a'*64}))
    seal.update(record_root_sha256=c.root,record_count=c.record_count,journal_bytes=c.byte_count)
    seed=load_sealed_seed(tmp_path,seal)
    assert seed.publication_record['revision']==2 and seed.batch.request.end_ns==BASE+200
