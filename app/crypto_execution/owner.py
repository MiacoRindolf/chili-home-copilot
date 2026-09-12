"""Native fractional order execution from durable cycles and tick decisions.

The host supplies a PAPER ownership authority, a complete locked admission
reader and tick decision source. This owner never invents a setup, ranks symbols,
starts a timer, or turns an unknown broker submission into a fresh order.
"""
from dataclasses import dataclass
import re
from urllib.parse import quote
from uuid import uuid4

from .lifecycle import next_action
from .paper_http import NotTransported
from .truth import crypto_order_truth, identity


@dataclass(frozen=True)
class TickDecision:
    context_sha256: str
    entry_allowed: bool | None
    exit_requested: bool
    exit_limit_price: str | None = None

    def __post_init__(self):
        if (type(self.context_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',self.context_sha256) or
                self.entry_allowed is not None and type(self.entry_allowed) is not bool or
                type(self.exit_requested) is not bool or
                self.exit_limit_price is not None and type(self.exit_limit_price) is not str):
            raise ValueError('native_cycle_tick_decision_invalid')


class NativeCycleOwner:
    def __init__(self,store,broker,*,admission_reader,decision_reader):
        if identity(broker.account_id)!=store.account_id:
            raise ValueError('native_cycle_broker_account_mismatch')
        if not callable(admission_reader) or not callable(decision_reader):
            raise ValueError('native_cycle_runtime_readers_required')
        self.store=store;self.broker=broker
        self.admission_reader=admission_reader;self.decision_reader=decision_reader

    def _event(self,state,kind,**payload):
        result=self.store.apply(state['cycle_id'],event_id=str(uuid4()),
            event=dict(kind=kind,payload=payload),expected_revision=state['revision'],
            admission_reader=self.admission_reader)
        if not result['applied']:raise ValueError('native_cycle_intent_not_new')
        return result['state']

    def _request(self,state,method,path,payload,fence):
        def before_transport():
            fence()
            if method=='POST' and payload['side']=='buy':
                current=self.store.read(state['cycle_id'])
                decision=self._decision(current)
                if current['exit_requested'] or decision is None or decision.entry_allowed is not True or decision.exit_requested:
                    raise ValueError('native_cycle_tick_entry_withdrawn_before_http')
                self.store.record_evidence(state['cycle_id'],dict(phase='tick_entry_revalidated',
                    context_sha256=decision.context_sha256,intent_revision=state['entry_intent_revision']))
                fence()
        try:
            response=self.broker.request(method,path,payload,
                record=lambda evidence:self.store.record_evidence(state['cycle_id'],evidence),
                before_transport=before_transport)
        except NotTransported as error:
            if method=='POST':
                current=self.store.read(state['cycle_id'])
                revision=(state['entry_intent_revision'] if payload['side']=='buy' else
                          state['exit_requests'][-1]['intent_revision'])
                self._event(current,'transport_proven_unsent',request_id=error.request_id,
                            side=payload['side'],intent_revision=revision)
            raise
        return self.store.read(state['cycle_id']),response

    def _decision(self,state):
        decision=self.decision_reader(state)
        if decision is not None and type(decision) is not TickDecision:
            raise ValueError('native_cycle_tick_decision_type_invalid')
        return decision

    def _result(self,state,outcome,**detail):
        return dict(outcome=outcome,cycle_id=state['cycle_id'],revision=state['revision'],
                    next_action=next_action(state),closed=state['closed'],**detail)

    def _observe_order(self,state,response,kind):
        if not 200<=response.status<300:
            # Even a CID lookup 404 cannot prove that an interrupted POST was
            # never accepted. Keep the original intent and reservation intact.
            return state,self._result(state,'broker_order_unresolved',http_status=response.status)
        row=response.json()
        if type(row) is not dict:raise ValueError('native_cycle_broker_order_shape')
        state=self._event(state,kind,**row)
        return state,None

    def _position(self,state,fence):
        read_id=str(uuid4())
        state=self._event(state,'position_read_started',read_id=read_id)
        read_revision=state['revision']
        state,response=self._request(state,'GET','/v2/positions/'+state['asset']['id'],None,fence)
        if response.status==404:
            state=self._event(state,'position_observed',read_id=read_id,read_revision=read_revision,found=False)
        elif response.status==200:
            row=response.json()
            if type(row) is not dict:raise ValueError('native_cycle_broker_position_shape')
            state=self._event(state,'position_observed',read_id=read_id,read_revision=read_revision,
                              found=True,position=row)
        else:
            return state,self._result(state,'broker_position_unresolved',http_status=response.status)
        return state,None

    def step(self,cycle_id):
        """Make one bounded progress step; caller schedules broker/tick wakeups.

        Database/HTTP exceptions propagate. The persisted intent and raw evidence
        remain available; a caller must not turn an exception into another POST.
        """
        with self.store.cycle_owner(cycle_id) as fence:
            if fence is None:
                return dict(outcome='cycle_owned_by_another_worker',cycle_id=identity(cycle_id))
            state=self.store.read(cycle_id)
            if state['closed']:return self._result(state,'closed')
            if state['contract']!='native_crypto_long_cycle_v2':
                raise ValueError('native_cycle_owner_requires_v2_transport_contract')
            # The fixed credential client must identify the same PAPER account
            # immediately in this step, before any economic mutation.
            state,account=self._request(state,'GET','/v2/account',None,fence)
            if account.status!=200: return self._result(state,'account_read_unavailable',http_status=account.status)
            row=account.json()
            if type(row) is not dict or identity(row.get('id'))!=self.store.account_id:
                raise ValueError('native_cycle_broker_account_identity_changed')
            decision=self._decision(state)
            if decision is not None and decision.exit_requested and not state['exit_requested']:
                state=self._event(state,'exit_requested',context_sha256=decision.context_sha256)
            action=next_action(state)
            if action=='cancel_unsubmitted_entry':
                return self._result(self._event(state,'cancel_unsubmitted_entry'),'unsubmitted_entry_retired')
            if action=='submit_entry':
                if decision is None or decision.entry_allowed is None:
                    return self._result(state,'waiting_for_tick_entry_decision')
                if not decision.entry_allowed:
                    return self._result(self._event(state,'cancel_unsubmitted_entry'),
                                        'tick_setup_invalidated_before_submit')
                state=self._event(state,'entry_transport_started',context_sha256=decision.context_sha256)
                state,response=self._request(state,'POST','/v2/orders',state['instruction'],fence)
                state,error=self._observe_order(state,response,'entry_observed')
                return error or self._result(state,'entry_observed')
            if action in ('reconcile_entry_by_client_id','reconcile_entry','cancel_then_reconcile_entry'):
                path=('/v2/orders/'+state['entry']['id'] if state['entry'] else
                      '/v2/orders:by_client_order_id?client_order_id='+quote(state['instruction']['client_order_id'],safe=''))
                state,response=self._request(state,'GET',path,None,fence)
                state,error=self._observe_order(state,response,'entry_observed')
                if error:return error
                entry=crypto_order_truth(state['entry'],asset=state['asset'],expected_order_id=state['entry']['id'])
                if state['exit_requested'] and not entry.terminal and entry.status!='pending_cancel':
                    state=self._event(state,'entry_cancel_transport_started',order_id=entry.order_id)
                    state,response=self._request(state,'DELETE','/v2/orders/'+entry.order_id,None,fence)
                    # A successful cancel request is not a terminal order/fill.
                    return self._result(state,'entry_cancel_requested_reconciliation_required',http_status=response.status)
                return self._result(state,'entry_reconciled')
            if action=='reconcile_exit_by_client_id':
                path=('/v2/orders/'+state['exits'][-1]['id'] if state['exits'][-1] else
                      '/v2/orders:by_client_order_id?client_order_id='+quote(state['exit_requests'][-1]['client_order_id'],safe=''))
                state,response=self._request(state,'GET',path,None,fence)
                state,error=self._observe_order(state,response,'exit_observed')
                return error or self._result(state,'exit_reconciled')
            if action in ('read_position','reconcile_unexplained_flat_position','reconcile_unexplained_position',
                          'reconcile_reserved_balance','submit_full_exit'):
                # Refresh again immediately before selling, even if a previous
                # completed step had already read a whole available balance.
                state,error=self._position(state,fence)
                if error:return error
                if next_action(state)!='submit_full_exit':return self._result(state,'position_reconciled')
                decision=self._decision(state)
                if decision is None or decision.exit_limit_price is None:
                    return self._result(state,'waiting_for_tick_exit_quote')
                request=dict(position_revision=state['position_revision'],qty=state['position']['qty'],
                    limit_price=decision.exit_limit_price,client_order_id='chili-x-'+uuid4().hex,
                    context_sha256=decision.context_sha256)
                state=self._event(state,'exit_transport_started',**request)
                instruction=dict(symbol=state['asset']['symbol'],side='sell',type='limit',time_in_force='ioc',
                    qty=request['qty'],limit_price=request['limit_price'],client_order_id=request['client_order_id'])
                state,response=self._request(state,'POST','/v2/orders',instruction,fence)
                state,error=self._observe_order(state,response,'exit_observed')
                return error or self._result(state,'full_exit_observed')
            return self._result(state,'holding_for_tick_decision')
