"""Existing PAPER lease identity is verified; no test touches a live process/lease."""
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.crypto_execution import window_authority as wa
from tests.test_native_crypto_cycle_store import store
from tests.test_native_crypto_lifecycle import ACCOUNT


@pytest.fixture
def bound(store,tmp_path,monkeypatch):
    lease=store.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    lease.execute(text('SELECT pg_advisory_lock(:c,:o)'),{'c':wa.LEASE_CLASS,'o':wa.LEASE_OBJECT})
    pid,started=lease.execute(text('SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()')).one()
    supervisor=tmp_path/'supervisor.py';supervisor.write_text('# fixture supervisor')
    env=tmp_path/'paper.env';env.write_text('fixture configuration')
    stamp=datetime.now(timezone.utc)
    root=Path(wa.__file__).resolve().parents[2]
    process=dict(app_pid=900002,app_created=(stamp+timedelta(seconds=1)).timestamp(),app_cwd=str(root),
        parent_pid=900001,parent_created=(started-timedelta(seconds=1)).timestamp(),parent_arguments=['python',str(supervisor)])
    monkeypatch.setattr(wa,'_process_identity',lambda:dict(process))
    doc=dict(schema='chili.timeshare-handoff-accepted.v3',at_utc=stamp.isoformat(),clean=True,
        holder_pid=900001,lease_backend_pid=pid,lease_held_verified=True,
        lease_key=dict(classid=wa.LEASE_CLASS,objid=wa.LEASE_OBJECT),
        broker_census=dict(endpoint=wa.PAPER,account_id=ACCOUNT,expected_account_id=ACCOUNT,clean=True,positions_count=0,orders_count=0),
        producer_census=dict(clean=True,order_capable_total=0),prestart_counters={k:0 for k in wa.COUNTERS},
        script_sha256=hashlib.sha256(supervisor.read_bytes()).hexdigest(),
        env_file_sha256=hashlib.sha256(env.read_bytes()).hexdigest())
    path=tmp_path/'accepted.json';path.write_text(json.dumps(doc))
    def construct():
        return wa.PaperWindowAuthority(store.engine,account_id=ACCOUNT,receipt_path=path,
            receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),supervisor_path=supervisor,
            env_path=env,code_root=root,max_receipt_bytes=16384)
    try:yield dict(construct=construct,doc=doc,path=path,process=process,env=env,lease=lease)
    finally:lease.invalidate();lease.close()


def test_current_child_and_real_db_lease_bind_then_lease_loss_is_detected(bound):
    authority=bound['construct']();authority(ACCOUNT)
    bound['lease'].execute(text('SELECT pg_advisory_unlock(:c,:o)'),{'c':wa.LEASE_CLASS,'o':wa.LEASE_OBJECT})
    with pytest.raises(ValueError,match='lease_lost'):authority(ACCOUNT)


def test_another_process_cannot_reuse_an_accepted_window_receipt(bound):
    bound['process']['parent_pid']=900003
    with pytest.raises(ValueError,match='owner_not_verified'):bound['construct']()


def test_process_generation_change_invalidates_previously_bound_authority(bound):
    authority=bound['construct']()
    bound['process']['app_created']+=1
    with pytest.raises(ValueError,match='application_generation_changed'):authority(ACCOUNT)


@pytest.mark.parametrize('field', ['receipt','environment'])
def test_changed_bound_artifact_is_not_authority(bound,field):
    authority=bound['construct']()
    path=bound['path'] if field=='receipt' else bound['env']
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='changed'):authority(ACCOUNT)


def test_dirty_prestart_census_cannot_bind_even_if_top_level_clean_claims_true(bound):
    bound['doc']['prestart_counters']['active_action_claims']=1
    bound['path'].write_text(json.dumps(bound['doc']))
    with pytest.raises(ValueError,match='owner_not_verified'):bound['construct']()


def test_live_endpoint_in_receipt_never_binds_to_paper_transport(bound):
    bound['doc']['broker_census']['endpoint']='https://api.alpaca.markets'
    bound['path'].write_text(json.dumps(bound['doc']))
    with pytest.raises(ValueError,match='owner_not_verified'):bound['construct']()
