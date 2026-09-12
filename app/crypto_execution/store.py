"""Atomic native-cycle journal and exposure projection; no broker order authority.

SQL mutations use the existing Alpaca/adaptive account lock order. An admission
callback must read the OTHER account ledgers while these locks are held. This
module does not certify that callback's coverage or activate an execution loop.
"""
from copy import deepcopy
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import re

from sqlalchemy import text

from .funding import FundingClaim,amount,funding_residual
from .lifecycle import canonical,decimal_text,digest,exposure,initial_cycle,transition
from .truth import identity
from .allocation import allocate_native_lots

ACCOUNT_SCOPE='alpaca:paper'
ACCOUNT_LOCK=int.from_bytes(hashlib.sha256(('chili|alpaca|account-risk|'+ACCOUNT_SCOPE).encode()).digest()[:8],'big',signed=True)
ADAPTIVE_NAMESPACE=0x4152
CYCLE_OWNER_NAMESPACE=0x4352  # CR: independent per-cycle transport serialization.


def schema_statements(schema='public'):
    if not re.fullmatch('[a-z][a-z0-9_]*',schema):raise ValueError('native_cycle_schema_invalid')
    cycles=f'{schema}.native_crypto_cycles';events=f'{schema}.native_crypto_cycle_events'
    return [f'''CREATE TABLE IF NOT EXISTS {cycles} (
        cycle_id UUID PRIMARY KEY,account_id UUID NOT NULL,asset_id UUID NOT NULL,
        client_order_id TEXT NOT NULL,quote_currency TEXT NOT NULL,revision BIGINT NOT NULL CHECK(revision>=0),
        state_json TEXT NOT NULL,state_sha256 TEXT NOT NULL,head_sha256 TEXT NOT NULL,
        debit NUMERIC NOT NULL CHECK(debit>=0),risk NUMERIC NOT NULL CHECK(risk>=0),closed BOOLEAN NOT NULL,
        UNIQUE(account_id,client_order_id),
        CHECK(closed=(state_json::jsonb->>'closed')::boolean),
        CHECK(revision=(state_json::jsonb->>'revision')::bigint),
        CHECK(cycle_id=(state_json::jsonb->>'cycle_id')::uuid),
        CHECK(account_id=(state_json::jsonb->>'account_id')::uuid),
        CHECK(asset_id=(state_json::jsonb->'asset'->>'id')::uuid),
        CHECK(debit=CASE WHEN closed THEN 0 ELSE (state_json::jsonb->>'original_debit')::numeric END),
        CHECK(risk=debit))''',
        f'''CREATE UNIQUE INDEX IF NOT EXISTS native_crypto_one_open_asset
            ON {cycles}(account_id,asset_id) WHERE NOT closed''',
        f'''CREATE TABLE IF NOT EXISTS {events} (
            cycle_id UUID NOT NULL REFERENCES {cycles}(cycle_id),revision BIGINT NOT NULL CHECK(revision>=0),
            event_id UUID NOT NULL,event_json TEXT NOT NULL,previous_sha256 TEXT,
            event_sha256 TEXT NOT NULL,state_sha256 TEXT NOT NULL,
            PRIMARY KEY(cycle_id,revision),UNIQUE(cycle_id,event_id))''',
        f'''CREATE OR REPLACE FUNCTION {schema}.native_crypto_events_immutable() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'native crypto events are append only'; END $$''',
        f'''DROP TRIGGER IF EXISTS native_crypto_events_immutable ON {events}''',
        f'''CREATE TRIGGER native_crypto_events_immutable BEFORE UPDATE OR DELETE ON {events}
            FOR EACH ROW EXECUTE FUNCTION {schema}.native_crypto_events_immutable()''',
        f'''DROP TRIGGER IF EXISTS native_crypto_events_no_truncate ON {events}''',
        f'''CREATE TRIGGER native_crypto_events_no_truncate BEFORE TRUNCATE ON {events}
            FOR EACH STATEMENT EXECUTE FUNCTION {schema}.native_crypto_events_immutable()''']


class NativeCycleStore:
    def __init__(self,engine,*,account_id,schema='public',max_event_bytes,lock_timeout_ms):
        schema_statements(schema)
        if (type(max_event_bytes) is not int or max_event_bytes<=0 or
                type(lock_timeout_ms) is not int or lock_timeout_ms<=0):
            raise ValueError('native_cycle_resource_bounds_required')
        self.engine=engine;self.account_id=identity(account_id)
        self.cycles=f'{schema}.native_crypto_cycles';self.events=f'{schema}.native_crypto_cycle_events'
        self.max_bytes=max_event_bytes;self.lock_timeout_ms=lock_timeout_ms

    def _lock(self,c):
        # A repeatable-read snapshot taken before waiting on the lock could miss
        # the previous owner's newly committed debit. Every ledger scan must see
        # commits completed before this owner acquired the account lock.
        if c.get_isolation_level()!='READ COMMITTED':
            raise ValueError('native_cycle_read_committed_required')
        c.execute(text("SELECT set_config('lock_timeout',:timeout,true)"),{'timeout':str(self.lock_timeout_ms)+'ms'})
        c.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':ACCOUNT_LOCK})
        c.execute(text('SELECT pg_advisory_xact_lock(:namespace,hashtext(:scope))'),
            {'namespace':ADAPTIVE_NAMESPACE,'scope':ACCOUNT_SCOPE})

    def _encode(self,value):
        raw=canonical(value)
        if len(raw.encode())>self.max_bytes:raise ValueError('native_cycle_event_resource_capacity')
        return raw

    def _read(self,c,cycle_id):
        row=c.execute(text(f'SELECT * FROM {self.cycles} WHERE cycle_id=:id AND account_id=:account'),
            {'id':identity(cycle_id),'account':self.account_id}).mappings().first()
        if row is None:raise ValueError('native_cycle_not_found')
        state=json.loads(row['state_json'])
        if (digest(state)!=row['state_sha256'] or state['revision']!=row['revision'] or
                state['account_id']!=self.account_id or state['cycle_id']!=str(row['cycle_id']) or
                state['asset']['id']!=str(row['asset_id']) or state['quote_currency']!=row['quote_currency'] or
                state['instruction']['client_order_id']!=row['client_order_id']):
            raise ValueError('native_cycle_projection_corrupt')
        values=exposure(state)
        if any(Decimal(values[k])!=row[k] for k in ('debit','risk')) or values['closed']!=row['closed']:
            raise ValueError('native_cycle_exposure_projection_corrupt')
        head=c.execute(text(f'SELECT event_sha256,state_sha256 FROM {self.events} WHERE cycle_id=:id AND revision=:revision'),
            {'id':identity(cycle_id),'revision':row['revision']}).first()
        if head is None or head[0]!=row['head_sha256'] or head[1]!=row['state_sha256']:
            raise ValueError('native_cycle_projection_head_mismatch')
        return row,state

    def read(self,cycle_id):
        with self.engine.connect() as c:return self._read(c,cycle_id)[1]

    @contextmanager
    def cycle_owner(self,cycle_id):
        """One transport worker per cycle; unrelated symbols do not share this lock.

        Session locks never go back to the connection pool, including on a
        failed unlock. The returned check must run immediately before HTTP.
        This fence does not replace the application's PAPER ownership lease.
        """
        key=self.account_id+'|'+identity(cycle_id)
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
            try:
                acquired=c.execute(text('SELECT pg_try_advisory_lock(:ns,hashtext(:key))'),
                    {'ns':CYCLE_OWNER_NAMESPACE,'key':key}).scalar_one()
                if not acquired:
                    yield None
                    return
                backend=c.execute(text('SELECT pg_backend_pid()')).scalar_one()
                def check():
                    if c.invalidated:raise ValueError('native_cycle_transport_owner_lost')
                    held=c.execute(text('''SELECT pg_backend_pid()=:pid AND EXISTS (
                        SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory'
                        AND granted AND classid=:ns
                        AND objid=(hashtext(:key)::bigint & 4294967295) AND objsubid=2)'''),
                        {'pid':backend,'ns':CYCLE_OWNER_NAMESPACE,'key':key}).scalar_one()
                    if held is not True:raise ValueError('native_cycle_transport_owner_lost')
                yield check
            finally:
                c.invalidate()  # Closing the backend releases only this session's locks.

    def record_evidence(self,cycle_id,payload):
        """Append raw transport evidence using the current revision under lock."""
        from uuid import uuid4
        with self.engine.begin() as c:
            self._lock(c)
            _,state=self._read(c,cycle_id)
            # Inline the event transaction: a stale external decision may not
            # overwrite state, but its raw broker response still needs retention.
            event=dict(kind='broker_evidence',payload=payload)
            return self._append(c,cycle_id,str(uuid4()),event,state)

    def _append(self,c,cycle_id,event_id,event,state):
        row,current=self._read(c,cycle_id)
        if current!=state:raise ValueError('native_cycle_revision_changed')
        encoded=self._encode(event);new=transition(state,event)
        sha=digest(new);raw=self._encode(new);values=exposure(new)
        head=digest({'previous':row['head_sha256'],'event':event,'state_sha256':sha})
        c.execute(text(f'''INSERT INTO {self.events} VALUES(:id,:revision,:eid,:event,:previous,:head,:sha)'''),
            {'id':cycle_id,'revision':new['revision'],'eid':event_id,'event':encoded,
             'previous':row['head_sha256'],'head':head,'sha':sha})
        c.execute(text(f'''UPDATE {self.cycles} SET revision=:revision,state_json=:state,state_sha256=:sha,
            head_sha256=:head,debit=:debit,risk=:risk,closed=:closed WHERE cycle_id=:id'''),
            {'revision':new['revision'],'state':raw,'sha':sha,'head':head,**values,'id':cycle_id})
        return new

    def _totals(self,c):
        # The caller owns the shared account lock. Read every active native
        # claim; no LIMIT/top-N may turn a partial risk ledger into a full one.
        rows=c.execute(text(f'SELECT * FROM {self.cycles} WHERE account_id=:account AND NOT closed ORDER BY cycle_id'),
            {'account':self.account_id}).mappings().all()
        totals=[]
        for row in rows:
            checked,state=self._read(c,str(row['cycle_id']))
            totals.append((checked,state))
        return totals

    def _admit(self,c,rows,new_state,admission_reader):
        if not callable(admission_reader):raise ValueError('native_cycle_locked_admission_reader_required')
        admission=admission_reader(c)
        return self._admit_snapshot(rows,new_state,admission)

    def _admit_snapshot(self,rows,new_state,admission):
        account=admission['account'];read_id=identity(admission['observation_id'])
        if account.account_id!=self.account_id:raise ValueError('native_cycle_admission_account_mismatch')
        external=list(admission['external_claims'])
        claims=list(external)
        claims.extend(FundingClaim(str(r['cycle_id']),self.account_id,s['quote_currency'],r['debit'],Decimal(0)) for r,s in rows)
        if new_state is not None:
            claims.append(FundingClaim(new_state['cycle_id'],self.account_id,'USD',Decimal(new_state['original_debit']),Decimal(0)))
        funding=funding_residual(account,quote_currency='USD',claims=claims,observation_id=read_id)
        if funding['unavailable_reasons'] or funding['unreflected_debit_upper_bound']>account.non_marginable_buying_power:
            raise ValueError('native_cycle_funding_exceeded_or_unavailable')
        budget=amount(admission['account_risk_budget']);other=amount(admission['external_risk_upper_bound'])
        required=Fraction(other)+sum((Fraction(r['risk']) for r,_ in rows),Fraction(0))
        if new_state is not None:required+=Fraction(Decimal(new_state['original_debit']))
        if budget<=0 or other<0 or required>budget:raise ValueError('native_cycle_unprotected_risk_budget_exceeded')
        receipt = dict(account_id=self.account_id,observation_id=read_id,
            non_marginable_buying_power=format(account.non_marginable_buying_power,'f'),
            unreflected_debit_upper_bound=decimal_text(funding['unreflected_debit_upper_bound']),
            account_risk_budget=format(budget,'f'),account_risk_required=decimal_text(required),
            external_risk_upper_bound=format(other,'f'),
            claim_ids=list(funding['claim_ids']),
            external_coverage_certified_by_store=False,broker_reflection_assumed=False)
        if 'evidence_receipt' in admission:
            evidence=admission['evidence_receipt']
            if (type(evidence) is not dict or evidence.get('observation_id')!=read_id or
                    evidence.get('account_id')!=self.account_id):
                raise ValueError('native_cycle_admission_evidence_identity_mismatch')
            receipt['evidence_receipt']=deepcopy(evidence)
        return receipt

    def _insert(self,c,state):
        raw=self._encode(state);sha=digest(state)
        head=digest({'previous':None,'event':state,'state_sha256':sha})
        c.execute(text(f'''INSERT INTO {self.cycles} VALUES
            (:id,:a,:asset,:cid,:currency,0,:state,:sha,:head,:debit,:debit,false)'''),
            {'id':state['cycle_id'],'a':self.account_id,'asset':state['asset']['id'],
                'cid':state['instruction']['client_order_id'],'currency':'USD','state':raw,'sha':sha,
                'head':head,'debit':state['original_debit']})
        c.execute(text(f'''INSERT INTO {self.events} VALUES(:id,0,:id,:event,NULL,:head,:sha)'''),
            {'id':state['cycle_id'],'event':raw,'head':head,'sha':sha})

    def reserve_all(self,opportunities,*,admission_reader):
        """Allocate and claim the full supplied candidate set under account locks.

        One locked fresh admission snapshot covers all ordinary/adaptive/native
        exposure. Persist the complete allocation receipt with every new cycle.
        No order is submitted. Replays keep original quantities; already owned
        assets remain explicit, while other candidates can still be reserved.
        The caller derives stable opportunity keys from actual signal events,
        not HTTP polling clocks, and retains every noneligible selection receipt.
        """
        if not callable(admission_reader):raise ValueError('native_cycle_locked_admission_reader_required')
        candidates=deepcopy(tuple(opportunities));seen=set();cids=set()
        required={'cycle_id','asset','limit_price','context_sha256','client_order_id','opportunity_key'}
        for o in candidates:
            if set(o)!=required:raise ValueError('native_cycle_candidate_contract_invalid')
            o['cycle_id']=identity(o['cycle_id'])
            if (o['cycle_id'] in seen or type(o['client_order_id']) is not str or not o['client_order_id'] or
                    o['client_order_id'] in cids or any(type(o[k]) is not str or not re.fullmatch('[0-9a-f]{64}',o[k])
                    for k in ('context_sha256','opportunity_key'))):raise ValueError('native_cycle_candidate_identity_invalid')
            seen.add(o['cycle_id']);cids.add(o['client_order_id'])
        # Validate every native price/lot input, even if funding will be zero.
        allocate_native_lots(candidates,funding_available='0',risk_available='0')
        reused=[];owned=[];pending=[]
        with self.engine.begin() as c:
            self._lock(c);rows=self._totals(c)
            owned_assets={s['asset']['id']:s['cycle_id'] for _,s in rows}
            for o in candidates:
                matches=c.execute(text(f'''SELECT cycle_id FROM {self.cycles} WHERE account_id=:a
                    AND (cycle_id=:id OR client_order_id=:cid)'''),
                    {'a':self.account_id,'id':o['cycle_id'],'cid':o['client_order_id']}).scalars().all()
                if matches:
                    if len(matches)!=1:raise ValueError('native_cycle_candidate_identity_conflict')
                    _,old=self._read(c,str(matches[0]))
                    if (old['cycle_id']!=o['cycle_id'] or old['asset']['id']!=identity(o['asset']['id']) or
                            old['asset']['symbol']!=o['asset']['symbol'] or
                            any(Fraction(old['asset'][k])!=Fraction(o['asset'][k]) for k in
                                ('price_increment','min_trade_increment','min_order_size')) or
                            old['instruction']['client_order_id']!=o['client_order_id'] or
                            Fraction(old['instruction']['limit_price'])!=Fraction(o['limit_price']) or
                            old['admission_receipt'].get('opportunity_key')!=o['opportunity_key']):
                        raise ValueError('native_cycle_candidate_replay_changed')
                    reused.append(old);continue
                existing=owned_assets.get(identity(o['asset']['id']))
                if existing:
                    owned.append(dict(cycle_id=o['cycle_id'],asset_id=identity(o['asset']['id']),
                        existing_cycle_id=existing,reason='asset_already_owned'));continue
                pending.append(o)
            if not pending:return dict(created=[],reused=reused,already_owned=owned,allocation=None)
            admission=admission_reader(c)
            before=self._admit_snapshot(rows,None,admission)
            allocation=allocate_native_lots(pending,
                funding_available=decimal_text(Fraction(before['non_marginable_buying_power'])-Fraction(before['unreflected_debit_upper_bound'])),
                risk_available=decimal_text(Fraction(before['account_risk_budget'])-Fraction(before['account_risk_required'])))
            by_id={o['cycle_id']:o for o in pending};new=[]
            for a in allocation['allocated']:
                o=by_id[a['cycle_id']]
                instruction=dict(symbol=a['symbol'],side='buy',type='limit',time_in_force='ioc',
                    qty=a['quantity'],limit_price=a['limit_price'],client_order_id=o['client_order_id'])
                state=initial_cycle(cycle_id=o['cycle_id'],account_id=self.account_id,asset=o['asset'],
                    instruction=instruction,context_sha256=o['context_sha256'])
                new.append(state)
            additions=[(dict(cycle_id=s['cycle_id'],debit=Decimal(s['original_debit']),risk=Decimal(s['original_debit'])),s) for s in new]
            after=self._admit_snapshot([*rows,*additions],None,admission)
            for state in new:
                state['admission_receipt']=dict(after,allocation=allocation,
                    opportunity_key=by_id[state['cycle_id']]['opportunity_key'])
                self._insert(c,state)
        return dict(created=new,reused=reused,already_owned=owned,allocation=allocation)

    def reserve(self,*,cycle_id,asset,instruction,context_sha256,admission_reader):
        state=initial_cycle(cycle_id=cycle_id,account_id=self.account_id,asset=asset,
            instruction=instruction,context_sha256=context_sha256)
        if state['quote_currency']!='USD':raise ValueError('native_cycle_quote_asset_ledger_required')
        if not callable(admission_reader):raise ValueError('native_cycle_locked_admission_reader_required')
        with self.engine.begin() as c:
            self._lock(c)
            existing=c.execute(text(f'SELECT cycle_id FROM {self.cycles} WHERE account_id=:a AND (cycle_id=:id OR client_order_id=:cid)'),
                {'a':self.account_id,'id':state['cycle_id'],'cid':instruction['client_order_id']}).scalars().all()
            if existing:
                if len(existing)!=1:raise ValueError('native_cycle_duplicate_instruction_identity')
                _,old=self._read(c,str(existing[0]))
                first=c.execute(text(f'SELECT event_json FROM {self.events} WHERE cycle_id=:id AND revision=0'),{'id':str(existing[0])}).scalar_one()
                original=json.loads(first);original.pop('admission_receipt',None)
                if original!=state:raise ValueError('native_cycle_reserved_identity_changed')
                return old
            rows=self._totals(c)
            if any(s['asset']['id']==state['asset']['id'] for _,s in rows):
                raise ValueError('native_cycle_asset_already_owned')
            # The callback must bind native funding plus all ordinary/adaptive
            # claims and account risk budget under these same locks. No HTTP or
            # coverage proof is fabricated by this storage layer.
            state['admission_receipt']=self._admit(c,rows,state,admission_reader)
            self._insert(c,state)
        return state

    def apply(self,cycle_id,*,event_id,event,expected_revision,admission_reader=None):
        event_id=identity(event_id);encoded=self._encode(event)
        with self.engine.begin() as c:
            self._lock(c);row,state=self._read(c,cycle_id)
            old=c.execute(text(f'SELECT event_json FROM {self.events} WHERE cycle_id=:id AND event_id=:event_id'),
                {'id':cycle_id,'event_id':event_id}).scalar_one_or_none()
            if old is not None:
                prior_event=json.loads(old);prior_event.pop('locked_admission_receipt',None)
                if canonical(prior_event)!=encoded:raise ValueError('native_cycle_event_identity_conflict')
                return {'state':state,'applied':False}
            if type(expected_revision) is not int or row['revision']!=expected_revision:
                raise ValueError('native_cycle_revision_changed')
            if 'locked_admission_receipt' in event:raise ValueError('native_cycle_caller_supplied_admission_receipt')
            if event.get('kind')=='entry_transport_started':
                event=deepcopy(event)
                event['locked_admission_receipt']=self._admit(c,self._totals(c),None,admission_reader)
                encoded=self._encode(event)
            new=self._append(c,cycle_id,event_id,event,state)
        return {'state':new,'applied':True}

    def audit(self,cycle_id,*,max_events):
        if type(max_events) is not int or max_events<=0:raise ValueError('native_cycle_audit_resource_bound_required')
        with self.engine.begin() as c:
            self._lock(c);row,state=self._read(c,cycle_id)
            if row['revision']+1>max_events:raise ValueError('native_cycle_audit_resource_capacity')
            events=c.execute(text(f'SELECT * FROM {self.events} WHERE cycle_id=:id ORDER BY revision'),{'id':cycle_id}).mappings().all()
            prior=None;replayed=None
            for index,item in enumerate(events):
                if item['revision']!=index or item['previous_sha256']!=prior:raise ValueError('native_cycle_journal_gap')
                event=json.loads(item['event_json']);replayed=event if index==0 else transition(replayed,event)
                sha=digest(replayed);head=digest({'previous':prior,'event':event,'state_sha256':sha})
                if sha!=item['state_sha256'] or head!=item['event_sha256']:raise ValueError('native_cycle_journal_corrupt')
                prior=head
            if replayed!=state or prior!=row['head_sha256']:raise ValueError('native_cycle_journal_projection_mismatch')
            return deepcopy(state)
