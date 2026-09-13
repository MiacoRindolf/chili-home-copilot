import json
from uuid import uuid4
import pytest

from app.crypto_execution import frontier_capture as module
from app.crypto_execution.frontier_capture import FrontierCapture
from app.crypto_execution.tick_context import CryptoTickContext
from app.tick_math.structural_prefix import Limits
from scripts.crypto_history_pages import HistoryRequest,canonical,sha
from tests.test_native_crypto_history_source import BASE,make_batch,q,t
from tests.test_native_crypto_frontier_source import SYMBOLS,fake_get


@pytest.fixture
def arguments():
    assets=[dict(id=str(uuid4()),symbol=s,**{'class':'crypto'},status='active',tradable=True) for s in SYMBOLS]
    seed,_=make_batch(HistoryRequest('us-1',SYMBOLS,BASE,BASE+100,100,'a'*64),trades=[t(90)],quotes=[q(80)])
    def factory():
        return CryptoTickContext(assets=assets,location='us-1',connection_id='preserved-source',
            limits=Limits(100,100,100),max_frame_bytes=65536,max_pending_quotes=100,source_kind='rest_pages')
    return dict(seed=seed,seed_origin=dict(metadata_sha256='a'*64,record_root_sha256='b'*64,
        record_count=5,journal_bytes=1000,revision=1),seed_published_ns=BASE+2000,book_factory=factory,
        resources=dict(max_pages=20,max_trades=100,max_quotes=100,max_page_bytes=65536,max_journal_bytes=1048576),
        clock_ns=lambda:BASE+4000)


def getter(*,late=False,fault=None):
    trades=[t(90),t(150,'12',tid=2)]
    if late:trades=[t(85,'8',tid=3)]+trades
    return fake_get([dict(trades={'BTC/USD':trades},next_page_token=None),
        dict(quotes={'BTC/USD':[q(80),q(140)]},next_page_token=None)],fault=fault)[0]


def test_disk_recovery_seeds_once_then_replays_delta_and_late_audit(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments) as c:
        c.observe(BASE+200,getter(),page_limit=100)
        old=c.current_views()
        c.observe(BASE+300,getter(late=True),mode='full_anchor_audit',page_limit=100)
        view=c.current_views();status=c.status()
        assert old[0].print_count==2 and view[0].print_count==3
    with FrontierCapture(tmp_path,**arguments) as recovered:
        assert recovered.current_views()==view
        assert recovered.status()['record_root_sha256']==status['record_root_sha256']
        assert recovered.reducer.revision==2


def test_exclusive_owner_and_release(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments):
        with pytest.raises(OSError):FrontierCapture(tmp_path,**arguments)
    with FrontierCapture(tmp_path,**arguments) as c:
        with pytest.raises(ValueError,match='unavailable'):c.current_views()


def test_failed_attempt_is_retained_and_retry_never_advances_failed_frontier(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments) as c:
        c.observe(BASE+200,getter(),page_limit=100)
        with pytest.raises(ValueError):c.observe(BASE+300,getter(fault='rate'),page_limit=100)
        assert c.reducer.members.through_ns==BASE+200
        with pytest.raises(ValueError,match='unavailable'):c.current_views()
    with FrontierCapture(tmp_path,**arguments) as c:
        assert not c.valid and c.reducer.revision==1
        c.observe(BASE+300,getter(),page_limit=100)
        assert c.valid and c.reducer.revision==2


@pytest.mark.parametrize('fault',['torn','hash','publication','incomplete'])
def test_retained_faults_never_produce_new_context(tmp_path,arguments,fault):
    with FrontierCapture(tmp_path,**arguments) as c:c.observe(BASE+200,getter(),page_limit=100)
    path=tmp_path/'frontier.jsonl';raw=path.read_bytes();rows=[json.loads(r) for r in raw.splitlines()]
    if fault=='torn':path.write_bytes(raw[:-2])
    elif fault=='hash':
        rows[1]['body']['group_index']=99;path.write_text('\n'.join(canonical(r) for r in rows)+'\n')
    elif fault=='publication':
        rows[-1]['body']['view']['source_sequence']+=1
        last=rows[-1];last['root']=sha(canonical({k:v for k,v in last.items() if k!='root'}))
        path.write_text('\n'.join(canonical(r) for r in rows)+'\n')
    else:
        path.write_text('\n'.join(canonical(r) for r in rows[:-1])+'\n')
        with FrontierCapture(tmp_path,**arguments) as c:
            assert not c.valid and c.reducer.revision==0
        return
    with pytest.raises(ValueError):FrontierCapture(tmp_path,**arguments)


def test_fsync_failure_disables_writer_and_does_not_expose_publication(tmp_path,arguments,monkeypatch):
    with FrontierCapture(tmp_path,**arguments) as c:
        def fail(_):raise OSError('disk failed')
        monkeypatch.setattr(module.os,'fsync',fail)
        with pytest.raises(OSError):c.observe(BASE+200,getter(),page_limit=100)
        assert c.failed_write and c.reducer.revision==0
        with pytest.raises(ValueError,match='recovery_required'):c.observe(BASE+200,getter(),page_limit=100)


def test_seed_origin_changed_is_rejected(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments):pass
    arguments['seed_origin']['record_root_sha256']='c'*64
    with pytest.raises(ValueError,match='binding_changed'):FrontierCapture(tmp_path,**arguments)
