"""Auditable experimental PAPER decisions from shared native tick context.

The operator's front/front long, local-back veto matrix is combined with
observed quote progress and conservative reported-flow bounds. Parent-back /
local-front remains an explicit study case. These are experimental conditions,
not an estimated probability, a profit forecast, or an optimal allocation rule.
All symbols produce receipts; this module neither ranks nor reserves capital.
"""
from dataclasses import dataclass
from fractions import Fraction
import json

from .lifecycle import canonical,digest,decimal_text
from .opportunity_math import credited_asset_roundtrip
from .owner import TickDecision
from .tick_context import NativeSymbolContext
from .truth import asset_identity,decimal


def exact(value):
    if isinstance(value,Fraction):return dict(numerator=value.numerator,denominator=value.denominator)
    if isinstance(value,dict):return {k:exact(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [exact(v) for v in value]
    return value


@dataclass(frozen=True)
class SelectionAssessment:
    symbol: str
    asset_id: str
    receipt_json: str
    sha256: str
    decision: TickDecision

    def receipt(self):return json.loads(self.receipt_json)


def assess_native_context(view,asset,*,fee_evidence):
    """Evaluate one immutable observed revision, independently of account funds.

    Fee evidence must name rates and a retained evidence SHA. The current
    bid/ask scenario reports costs; it does not invent a future target or silently
    demand a profitable immediate round trip (which would veto ordinary spreads).
    Actual quantity, minimum lot, funding and shared risk belong to admission.
    """
    if type(view) is not NativeSymbolContext:raise ValueError('native_selection_context_required')
    aid,symbol=asset_identity(asset)
    if (aid,symbol)!=(view.asset_id,view.symbol):raise ValueError('native_selection_asset_context_mismatch')
    step=Fraction(decimal(asset.get('price_increment')))
    available=view.coverage in ('observed_connection_prefix','observed_rest_page_prefix')
    wave=view.wave;quote=view.current_quote
    local=wave.local_phase.side if wave else 'unknown'
    parent=wave.parent_phase.side if wave else 'unknown'
    quote_valid=bool(quote is not None and 0<quote.bid<quote.ask and quote.bid_size>0 and quote.ask_size>0)
    # Use exact ceiling/floor on the broker price lattice, never float rounding.
    buy_limit=decimal_text(-(-Fraction(quote.ask)//step)*step) if quote_valid else None
    sell_limit=decimal_text((Fraction(quote.bid)//step)*step) if quote_valid and Fraction(quote.bid)>=step else None
    reasons=[];allowed=None;exit_requested=available and local=='back'
    flow=None;progress=None;cost=None
    if not available:reasons.append('shared_tick_observation_unavailable')
    elif local=='back':allowed=False;reasons.append('local_backside_long_veto_full_sale')
    elif local!='front':reasons.append('local_phase_not_established')
    elif parent=='back':reasons.append('parent_back_local_front_requires_study')
    elif parent!='front':reasons.append('parent_phase_not_established')
    else:allowed=True
    if asset.get('status')!='active' or asset.get('tradable') is not True:
        allowed=False;reasons.append('asset_not_active_and_tradable_for_entry')
    if available and wave and wave.local_valley:
        ref=wave.local_valley
        matches=[f for f in view.reported_flow if
            (f.basis,f.kind,f.order,f.origin_id,f.confirmation_id)==
            (ref.basis,ref.kind,ref.order,ref.origin_id,ref.confirmation_id)]
        evidence=[e for e in view.quote_inferred_evidence if e.reference==ref]
        if len(matches)==1:
            f=matches[0];b,s,u=(a+z for a,z in zip(f.formation,f.follow_through))
            flow=dict(buy_volume=b,sell_volume=s,unknown_volume=u,net_lower=b-s-u,net_upper=b-s+u,
                origin_id=ref.origin_id,confirmation_id=ref.confirmation_id,end_id=f.end_id,
                interval='(confirmed_valley_origin,current_print]',basis=f.side_basis)
        if len(evidence)==1:
            e=evidence[0].whole
            progress=dict(print_change=e.price_change,bid_change=e.bid_change,ask_change=e.ask_change,
                origin_id=e.start_id,end_id=e.end_id,basis='valley_to_current_print_bound_quotes')
    if allowed is True:
        if flow is None:allowed=None;reasons.append('valley_reported_flow_unavailable')
        elif flow['net_lower']<=0:allowed=False;reasons.append('buyer_dominance_not_established')
        if progress is None or progress['bid_change'] is None or progress['ask_change'] is None:
            if allowed is True:allowed=None
            reasons.append('valley_quote_progress_unavailable')
        elif min(progress['print_change'],progress['bid_change'],progress['ask_change'])<=0:
            allowed=False;reasons.append('valley_price_and_quote_progress_not_positive')
    if not quote_valid:
        if allowed is True:allowed=None
        reasons.append('executable_quote_unavailable')
    if fee_evidence is None:
        if allowed is True:allowed=None
        reasons.append('fee_evidence_unavailable')
    elif quote_valid:
        try:
            cost=credited_asset_roundtrip(entry_ask=buy_limit,exit_bid=sell_limit or str(quote.bid),quantity='1',
                entry_fee_rate=fee_evidence['entry_fee_rate'],exit_fee_rate=fee_evidence['exit_fee_rate'],
                exit_price_increment=str(asset['price_increment']),fee_evidence_sha256=fee_evidence['sha256'])
        except (KeyError,TypeError,ValueError):
            if allowed is True:allowed=None
            reasons.append('fee_evidence_invalid')
    if available and quote_valid and wave and wave.local_valley:
        # A quote update can invalidate the executable setup before another
        # trade arrives. It does not relabel the print-derived wave itself.
        bid=Fraction(quote.bid);valley=wave.local_valley.price
        if bid<=valley:
            allowed=False;reasons.append('current_bid_not_above_confirmed_valley')
        if bid<valley:
            exit_requested=True;reasons.append('current_bid_broke_confirmed_valley_full_sale')
    if allowed is True:reasons.append('front_front_positive_valley_flow_and_quote_progress')
    body=exact(dict(contract='native_tick_selection_experiment_v1',symbol=symbol,asset_id=aid,
        source_identity_sha256=view.source_identity_sha256,source_root_sha256=view.source_root_sha256,
        source_sequence=view.source_sequence,prefix_sha256=view.prefix_sha256,coverage=view.coverage,
        source_kind=view.source_kind,history_before_acquisition=view.history_before_connection,
        parent_phase=parent,local_phase=local,structural_definition=wave.definition if wave else None,
        entry_allowed=allowed,exit_requested=exit_requested,exit_fraction='1' if exit_requested else None,
        entry_limit_price=buy_limit,exit_limit_price=sell_limit,reasons=reasons,
        current_quote_event_ns=quote.event_ns if quote else None,
        current_quote_source_sequence=view.current_quote_source_sequence,
        valley_reported_flow=flow,valley_quote_progress=progress,current_roundtrip_scenario=cost,
        cost_quantity_basis='one_base_unit_for_normalization_only_not_order_size',
        funding_evaluated=False,quantity_allocated=False,profit_forecast=None,
        experimentally_eligible_is_not_proven_profitable=True,order_authority=False))
    raw=canonical(body);sha=digest(body)
    return SelectionAssessment(symbol,aid,raw,sha,TickDecision(sha,allowed,exit_requested,sell_limit))


def assess_all_native(views,assets,*,fee_evidence):
    """Require complete membership and retain every symbol, including cold ones."""
    rows={}
    for asset in assets:
        aid,symbol=asset_identity(asset)
        if symbol in rows:raise ValueError('native_selection_duplicate_asset')
        rows[symbol]=asset
    values=tuple(views)
    if len({v.symbol for v in values})!=len(values) or {v.symbol for v in values}!=set(rows):
        raise ValueError('native_selection_membership_incomplete')
    if len({(v.source_identity_sha256,v.source_sequence,v.source_root_sha256) for v in values})!=1:
        raise ValueError('native_selection_mixed_source_publications')
    return tuple(assess_native_context(v,rows[v.symbol],fee_evidence=fee_evidence) for v in values)


class NativeTickDecisionReader:
    """Concrete owner callback; durable receipt must precede the returned verdict.

    A source adapter returns an immutable context or None. Account authorization
    and lifecycle reconciliation remain with NativeCycleOwner/PaperWindowAuthority.
    """
    def __init__(self,*,context_reader,asset_reader,fee_reader,record):
        if not all(callable(v) for v in (context_reader,asset_reader,fee_reader,record)):
            raise ValueError('native_selection_runtime_readers_required')
        self.context_reader=context_reader;self.asset_reader=asset_reader
        self.fee_reader=fee_reader;self.record=record

    def __call__(self,state):
        view=self.context_reader(state['asset']['symbol'])
        if view is None:return None
        asset=self.asset_reader(state['asset']['id'])
        assessment=assess_native_context(view,asset,fee_evidence=self.fee_reader())
        if assessment.asset_id!=state['asset']['id']:raise ValueError('native_selection_cycle_asset_changed')
        receipt=assessment.receipt();decision=assessment.decision
        # A previously reserved limit cannot exceed the current tick price cap.
        if decision.entry_allowed is True and Fraction(decimal(state['instruction']['limit_price']))>Fraction(decimal(receipt['entry_limit_price'])):
            receipt['entry_allowed']=False;receipt['reasons'].append('reserved_entry_limit_exceeds_current_tick_cap')
            sha=digest(receipt);decision=TickDecision(sha,False,decision.exit_requested,decision.exit_limit_price)
        self.record(dict(cycle_id=state['cycle_id'],context_sha256=decision.context_sha256,selection=receipt))
        return decision
