"""Bind native execution to the current child of an accepted PAPER supervisor.

The receipt supplies ownership evidence, not trading authorization. Operator
authorization and PAPER configuration remain the host's responsibility. This
module cannot launch, elevate, rewrite a task, acquire somebody else's lease,
or approve another process as the current application.
"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

import psutil
from sqlalchemy import text

from .paper_http import PAPER
from .truth import identity

LEASE_CLASS=0x414C5041
LEASE_OBJECT=0x4F574E52
COUNTERS={'active_action_claims','active_fill_watches','active_outbox_rows',
          'active_reservations','active_sessions','reserved_opportunities'}


def _process_identity():
    app=psutil.Process()
    parent=app.parent()
    if parent is None:raise ValueError('native_paper_parent_missing')
    return dict(app_pid=app.pid,app_created=app.create_time(),app_cwd=str(Path(app.cwd()).resolve()),
        parent_pid=parent.pid,parent_created=parent.create_time(),parent_cwd=str(Path(parent.cwd()).resolve()),
        parent_arguments=parent.cmdline())


def _sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


class PaperWindowAuthority:
    def __init__(self,engine,*,account_id,receipt_path,receipt_sha256,supervisor_path,env_path,
                 code_root,max_receipt_bytes):
        if (type(max_receipt_bytes) is not int or max_receipt_bytes<=0 or
                type(receipt_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',receipt_sha256)):
            raise ValueError('native_paper_authority_inputs_invalid')
        self.engine=engine;self.account_id=identity(account_id)
        self.receipt_path=Path(receipt_path).resolve(strict=True)
        self.supervisor_path=Path(supervisor_path).resolve(strict=True)
        self.env_path=Path(env_path).resolve(strict=True)
        self.code_root=Path(code_root).resolve(strict=True)
        with self.receipt_path.open('rb') as handle:raw=handle.read(max_receipt_bytes+1)
        if len(raw)>max_receipt_bytes or hashlib.sha256(raw).hexdigest()!=receipt_sha256:
            raise ValueError('native_paper_receipt_bytes_unverified')
        self.receipt_sha=receipt_sha256;self.doc=json.loads(raw)
        self.process=_process_identity()
        self._validate_binding()
        self.backend_started=self._read_lease()

    def _validate_binding(self):
        d=self.doc;p=self.process
        census=d.get('broker_census',{});producer=d.get('producer_census',{})
        counters=d.get('prestart_counters',{})
        if (d.get('schema')!='chili.timeshare-handoff-accepted.v3' or d.get('clean') is not True or
                d.get('lease_held_verified') is not True or
                d.get('holder_pid')!=p['parent_pid'] or d.get('lease_key')!={'classid':LEASE_CLASS,'objid':LEASE_OBJECT} or
                census.get('endpoint')!=PAPER or census.get('clean') is not True or
                identity(census.get('account_id'))!=self.account_id or
                identity(census.get('expected_account_id'))!=self.account_id or
                producer.get('clean') is not True or producer.get('order_capable_total')!=0 or
                set(counters)!=COUNTERS or any(type(v) is not int or v!=0 for v in counters.values()) or
                census.get('positions_count')!=0 or census.get('orders_count')!=0):
            raise ValueError('native_paper_accepted_owner_not_verified')
        receipt_at=datetime.fromisoformat(d['at_utc']).timestamp()
        if not p['parent_created']<=receipt_at<=p['app_created']:
            raise ValueError('native_paper_receipt_not_before_this_child')
        if Path(p['app_cwd'])!=self.code_root or Path(__file__).resolve().parents[2]!=self.code_root:
            raise ValueError('native_paper_loaded_code_root_mismatch')
        arguments=[]
        parent_cwd=Path(p['parent_cwd'])
        if not parent_cwd.is_absolute():raise ValueError('native_paper_parent_directory_unverified')
        for arg in p['parent_arguments']:
            try:
                path=Path(arg)
                if not path.is_absolute():path=parent_cwd/path
                if path.is_file():arguments.append(path.resolve())
            except OSError:pass
        if self.supervisor_path not in arguments:
            raise ValueError('native_paper_parent_is_not_bound_supervisor')
        if _sha(self.supervisor_path)!=d.get('script_sha256') or _sha(self.env_path)!=d.get('env_file_sha256'):
            raise ValueError('native_paper_supervisor_or_environment_changed')

    def _read_lease(self):
        with self.engine.connect() as c:
            c.execute(text("SELECT set_config('statement_timeout','20000',true)"))
            row=c.execute(text('''SELECT a.backend_start FROM pg_stat_activity a
                JOIN pg_locks l ON l.pid=a.pid WHERE a.pid=:pid
                AND a.datname=current_database() AND a.usename=current_user
                AND l.locktype='advisory' AND l.granted AND l.classid=:classid
                AND l.objid=:objid AND l.objsubid=2'''),
                {'pid':self.doc['lease_backend_pid'],'classid':LEASE_CLASS,'objid':LEASE_OBJECT}).first()
        if row is None:raise ValueError('native_paper_supervisor_lease_lost')
        created=row[0]
        parent=datetime.fromtimestamp(self.process['parent_created'],timezone.utc)
        accepted=datetime.fromisoformat(self.doc['at_utc'])
        if not parent<=created<=accepted:raise ValueError('native_paper_lease_backend_generation_changed')
        return created

    def __call__(self,account_id):
        if identity(account_id)!=self.account_id:raise ValueError('native_paper_authority_account_changed')
        if _process_identity()!=self.process:raise ValueError('native_paper_application_generation_changed')
        if _sha(self.receipt_path)!=self.receipt_sha:raise ValueError('native_paper_receipt_changed')
        self._validate_binding()
        if self._read_lease()!=self.backend_started:raise ValueError('native_paper_lease_backend_generation_changed')
