from uuid import uuid4
import pytest

from app.crypto_execution.frontier_reducer import FrontierReducer
from app.crypto_execution.tick_context import CryptoTickContext
from app.tick_math.structural_prefix import Limits
from scripts.crypto_history_pages import HistoryRequest
from tests.test_native_crypto_history_source import BASE,make_batch,q,t
from tests.test_native_crypto_frontier_members import observe
from tests.test_native_crypto_frontier_source import SYMBOLS


def setup():
    assets=[dict(id=str(uuid4()),symbol=s,**{'class':'crypto'},status='active',tradable=True) for s in SYMBOLS]
    def factory():
        return CryptoTickContext(assets=assets,location='us-1',connection_id='preserved-source',
            limits=Limits(100,100,100),max_frame_bytes=65536,max_pending_quotes=100,source_kind='rest_pages')
    seed,_=make_batch(HistoryRequest('us-1',SYMBOLS,BASE,BASE+100,100,'a'*64),trades=[t(90)],quotes=[q(80)])
    def restore():return FrontierReducer(seed,book_factory=factory,published_ns=BASE+2000,max_trades=100,max_quotes=100)
    return restore(),restore


def test_grouped_delta_uses_existing_tick_context_and_preserves_cold_symbols():
    reducer,_=setup();old=reducer.current_views()
    observation=observe(reducer.members,trades=[t(90),t(150,'12',tid=2)],quotes=[q(80),q(140,'11','13')])
    rows=[];body=reducer.apply(observation,published_ns=BASE+3000,record=rows.append)
    new=reducer.current_views()
    assert old[0].print_count==1 and new[0].print_count==2
    assert new[0].last_print.price==12 and new[0].last_print.bid==11
    assert {v.symbol for v in new}==set(SYMBOLS)
    assert [v.print_count for v in new[1:]]==[0,0]
    assert not body['reconstruction_symbols'] and not body['order_authority']
    assert reducer.book.frame_sequence==2 and rows[-1]==body


def test_late_backfill_rebuilds_current_view_without_rewriting_old_view():
    reducer,_=setup();old=reducer.current_views()
    audit=observe(reducer.members,trades=[t(85,'8',tid=2),t(90)],quotes=[q(80)],mode='full_anchor_audit')
    body=reducer.apply(audit,published_ns=BASE+3000,record=lambda _:None)
    assert body['reconstruction_symbols']==['BTC/USD']
    assert reducer.book.prefixes['BTC/USD'].tick(0).price==8
    assert reducer.current_views()[0].print_count==2 and old[0].print_count==1
    assert old[0].last_print.price==10


def test_late_quote_rebind_is_a_new_current_revision_with_same_provider_identity():
    reducer,_=setup();old=reducer.current_views()
    audit=observe(reducer.members,trades=[t(90)],quotes=[q(80),q(85,'8','12')],mode='full_anchor_audit')
    reducer.apply(audit,published_ns=BASE+3000,record=lambda _:None)
    new=reducer.current_views()
    assert old[0].last_print.bid==9 and new[0].last_print.bid==8
    assert old[0].source_identity_sha256==new[0].source_identity_sha256
    assert old[0].source_root_sha256!=new[0].source_root_sha256


def test_publication_fsync_failure_never_exposes_new_view_or_allows_continuation():
    reducer,restore=setup();old=reducer.current_views();identity=reducer.members.identity
    observation=observe(reducer.members,trades=[t(90),t(150,tid=2)],quotes=[q(80)])
    def broken(_):raise OSError('fsync failed')
    with pytest.raises(OSError):reducer.apply(observation,published_ns=BASE+3000,record=broken)
    assert reducer.members.identity==identity and reducer.revision==0
    assert old[0].print_count==1
    with pytest.raises(ValueError,match='reconstruction_required'):reducer.current_views()
    with pytest.raises(ValueError,match='reconstruction_required'):
        reducer.apply(observation,published_ns=BASE+4000,record=lambda _:None)
    assert restore().current_views()==old


def test_recovery_replays_committed_group_publications_exactly():
    reducer,restore=setup()
    observation=observe(reducer.members,trades=[t(90),t(150,tid=2)],quotes=[q(80),q(140)])
    one=reducer.apply(observation,published_ns=BASE+3000,record=lambda _:None)
    audit=observe(reducer.members,trades=[t(85,tid=3),t(90),t(150,tid=2)],quotes=[q(80),q(140)],mode='full_anchor_audit',end=300)
    two=reducer.apply(audit,published_ns=BASE+4000,record=lambda _:None)
    restarted=restore()
    assert restarted.apply(observation,published_ns=BASE+3000,record=lambda _:None)==one
    assert restarted.apply(audit,published_ns=BASE+4000,record=lambda _:None)==two
    assert restarted.current_views()==reducer.current_views()
