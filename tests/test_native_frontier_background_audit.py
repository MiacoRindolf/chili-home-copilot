import json
import threading
import pytest
from app.crypto_execution.frontier_capture import FrontierCapture
from tests.test_native_crypto_frontier_capture import arguments,getter
from tests.test_native_crypto_history_source import BASE


def test_interleaved_audit_collects_while_fast_updates_publish_then_recovers(tmp_path,arguments):
    entered=threading.Event();release=threading.Event();errors=[]
    with FrontierCapture(tmp_path,**arguments) as c:
        audit_get=getter(late=True)
        def blocked(**kwargs):
            entered.set()
            if not release.wait(10):raise TimeoutError('test synchronization')
            return audit_get(**kwargs)
        def audit():
            try:c.collect_audit(BASE+200,blocked,page_limit=100)
            except Exception as e:errors.append(e)
        worker=threading.Thread(target=audit);worker.start()
        try:
            assert entered.wait(10)
            c.observe(BASE+300,getter(),page_limit=100)
            old=c.current_views();assert old[0].print_count==2 and c.reducer.revision==1
            assert c.audit_collecting and c.pending_audit is None
        finally:release.set();worker.join(10)
        assert not worker.is_alive() and not errors
        assert c.reducer.revision==1  # collector has no publication authority
        assert c.publish_audit() and c.reducer.revision==2
        view=c.current_views();assert view[0].print_count==3 and old[0].print_count==2
        assert c.reducer.members.through_ns==BASE+300  # older audit cannot regress
        status=c.status()
    with FrontierCapture(tmp_path,**arguments) as restored:
        assert restored.current_views()==view
        assert restored.status()['record_root_sha256']==status['record_root_sha256']
        assert restored.reducer.revision==2 and restored.pending_audit is None


def test_durable_pending_audit_is_recovered_without_becoming_a_publication(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments) as c:
        c.collect_audit(BASE+200,getter(late=True),page_limit=100)
        assert c.reducer.revision==0
    with FrontierCapture(tmp_path,**arguments) as c:
        assert c.pending_audit is not None and not c.valid and c.reducer.revision==0
        assert c.publish_audit() and c.reducer.revision==1
        assert c.current_views()[0].print_count==3


def test_failed_audit_is_not_erased_by_successful_fast_update(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments) as c:
        c.observe(BASE+200,getter(),page_limit=100)
        with pytest.raises(ValueError):c.collect_audit(BASE+300,getter(fault='rate'),page_limit=100)
        c.observe(BASE+400,getter(),page_limit=100)
        assert not c.valid and c.failed_modes=={'full_anchor_audit'}
        c.collect_audit(BASE+500,getter(),page_limit=100)
        assert not c.valid
        c.publish_audit();assert c.valid and not c.failed_modes
    with FrontierCapture(tmp_path,**arguments) as restored:assert restored.valid


def test_second_collector_cannot_replace_pending_audit(tmp_path,arguments):
    with FrontierCapture(tmp_path,**arguments) as c:
        c.collect_audit(BASE+200,getter(),page_limit=100)
        with pytest.raises(ValueError,match='pending_publication'):
            c.collect_audit(BASE+300,getter(),page_limit=100)
        with pytest.raises(ValueError,match='pending_publication'):
            c.observe(BASE+300,getter(),mode='full_anchor_audit',page_limit=100)
        assert c.publish_audit()


def test_close_keeps_directory_owned_until_inflight_audit_stops_writing(tmp_path,arguments):
    c=FrontierCapture(tmp_path,**arguments);entered=threading.Event();release=threading.Event();closed=threading.Event();errors=[]
    get=getter()
    def blocked(**kwargs):
        entered.set()
        if not release.wait(10):raise TimeoutError('test synchronization')
        return get(**kwargs)
    def audit():
        try:c.collect_audit(BASE+200,blocked,page_limit=100)
        except Exception as exc:errors.append(exc)
    worker=threading.Thread(target=audit);worker.start()
    assert entered.wait(10)
    closer=threading.Thread(target=lambda:(c.close(),closed.set()));closer.start()
    try:
        assert not closed.wait(.1)
        with pytest.raises(OSError):FrontierCapture(tmp_path,**arguments)
    finally:
        release.set();worker.join(10);closer.join(10)
    assert closed.is_set() and not worker.is_alive() and not errors
    with FrontierCapture(tmp_path,**arguments) as recovered:assert recovered.pending_audit is not None
