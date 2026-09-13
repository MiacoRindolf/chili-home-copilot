"""Immutable observed membership merge for fast REST input and full audits.

No mutation of old publications, deletion of older evidence, or replacement of
newer input by an older audit. Rebinding old prints requires a new current-view
reconstruction. These records carry input evidence, never trading authority.
"""
from dataclasses import dataclass
from functools import cached_property
import json

from scripts.crypto_history_pages import canonical, sha
from .history_source import batch_messages, _event_messages
from .frontier_source import GroupedObservation, validate_plan
from .member_sequence import MemberSequence


def trade_key(t):return t.symbol,t.trade_id
def trade_value(t):return t.event_ns,t.price,t.size,t.reported_taker_side
def quote_key(q):return q.symbol,q.event_ns,q.bid,q.ask,q.bid_size,q.ask_size


@dataclass(frozen=True)
class MemberState:
    location: str
    symbols: tuple
    anchor_ns: int
    inventory_sha256: str
    through_ns: int
    known_ns: int
    trades: tuple
    quotes: tuple
    observation_sha256: str
    ancestors: tuple = ()

    def __post_init__(self):
        for channel in ('trades','quotes'):
            rows=getattr(self,channel)
            if type(rows) is not MemberSequence:
                rows=MemberSequence(channel,rows)
                object.__setattr__(self,channel,rows)
            elif rows.channel!=channel:
                raise ValueError('native_members_sequence_channel_changed')

    @cached_property
    def identity(self):
        # Every immutable member node commits to exact values and its prior
        # content root. Checksumming new state no longer reserializes history.
        content={channel:dict(root=getattr(self,channel).root,count=len(getattr(self,channel)))
                 for channel in ('trades','quotes')}
        return sha(canonical(dict(contract='native_observed_members_v3',location=self.location,
            symbols=self.symbols,anchor_ns=self.anchor_ns,inventory_sha256=self.inventory_sha256,
            through_ns=self.through_ns,known_ns=self.known_ns,observation_sha256=self.observation_sha256,
            ancestors=self.ancestors,content=content)))

    def histories(self):
        result={kind:{s:[] for s in self.symbols} for kind in ('trades','quotes')}
        for kind,rows in (('trades',self.trades),('quotes',self.quotes)):
            for row in rows:result[kind][row.symbol].append(row.event_ns)
        return result


def seed_members(batch):
    """Seed from an already verified complete retained full-anchor observation."""
    batch_messages(batch)  # Verify contents and exhausted response-walk bindings.
    request=batch.request
    unique={}
    for quote in batch.quotes:unique.setdefault(quote_key(quote),quote)
    return MemberState(request.location,request.symbols,request.start_ns,request.inventory_sha256,
        request.end_ns,batch.received_ns,batch.trades,tuple(unique.values()),batch.evidence_sha256)


@dataclass(frozen=True)
class MemberMerge:
    state: MemberState
    new_trades: tuple
    new_quotes: tuple
    reconstruction_symbols: tuple
    older_audit_merged: bool


def merge_members(current, observation, *, max_trades, max_quotes):
    if type(current) is not MemberState or type(observation) is not GroupedObservation:
        raise ValueError('native_members_typed_evidence_required')
    if any(type(n) is not int or n<=0 for n in (max_trades,max_quotes)):
        raise ValueError('native_members_resource_bounds_required')
    plan=observation.plan;validate_plan(plan)
    reference=plan.groups[0].request
    if (plan.symbols,plan.anchor_ns,reference.location,reference.inventory_sha256)!=(
            current.symbols,current.anchor_ns,current.location,current.inventory_sha256):
        raise ValueError('native_members_source_changed')
    previous=current.identity
    older=plan.prior_observation_sha256!=previous
    if older and (plan.mode!='full_anchor_audit' or plan.prior_observation_sha256 not in current.ancestors):
        raise ValueError('native_members_stale_or_unrelated_plan')
    receipt=json.loads(observation.receipt_json)
    content=sha(canonical(_event_messages(observation.trades,observation.quotes)))
    if (receipt.get('contract')!='native_grouped_frontier_observation_v1' or
            receipt.get('plan_sha256')!=plan.identity or receipt.get('content_sha256')!=content or
            receipt.get('all_requested_channel_page_chains_exhausted') is not True or
            type(receipt.get('known_ns')) is not int or receipt['known_ns']<plan.end_ns or
            receipt.get('trade_count')!=len(observation.trades) or receipt.get('quote_count')!=len(observation.quotes)):
        raise ValueError('native_members_observation_binding_changed')
    groups={kind:{s:g.request for g in plan.groups if g.channel==kind for s in g.request.symbols}
        for kind in ('trades','quotes')}
    for kind,rows in (('trades',observation.trades),('quotes',observation.quotes)):
        for row in rows:
            request=groups[kind].get(row.symbol)
            if request is None or not request.start_ns<=row.event_ns<=request.end_ns:
                raise ValueError('native_members_outside_recorded_group')
    incoming={}
    for trade in observation.trades:
        key=trade_key(trade)
        if key in incoming and trade_value(incoming[key])!=trade_value(trade):
            raise ValueError('native_members_trade_identity_conflict')
        incoming.setdefault(key,trade)
    quoted={quote_key(q):q for q in observation.quotes}
    old_trades={trade_key(t):t for t in current.trades}
    old_quotes={quote_key(q):q for q in current.quotes}
    # Check only actual queried ranges. A slow audit has no authority over
    # events beyond its end. Missing retained members inside its range remain
    # explicit conflicts, not permission to delete them from current state.
    for trade in current.trades:
        query=groups['trades'][trade.symbol]
        if query.start_ns<=trade.event_ns<=query.end_ns and trade_key(trade) not in incoming:
            raise ValueError('native_members_observed_trade_missing')
    for quote in current.quotes:
        query=groups['quotes'][quote.symbol]
        if query.start_ns<=quote.event_ns<=query.end_ns and quote_key(quote) not in quoted:
            raise ValueError('native_members_observed_quote_missing')
    for key,trade in incoming.items():
        if key in old_trades and trade_value(old_trades[key])!=trade_value(trade):
            raise ValueError('native_members_trade_identity_conflict')
    new_trades=tuple(t for key,t in incoming.items() if key not in old_trades)
    new_quotes=tuple(q for key,q in quoted.items() if key not in old_quotes)
    if len(old_trades)+len(new_trades)>max_trades or len(old_quotes)+len(new_quotes)>max_quotes:
        raise ValueError('native_members_retained_capacity')
    frontiers=dict.fromkeys(current.symbols)
    for t in current.trades:
        if frontiers[t.symbol] is None or t.event_ns>frontiers[t.symbol]:frontiers[t.symbol]=t.event_ns
    rebuild={t.symbol for t in new_trades if frontiers[t.symbol] is not None and t.event_ns<=frontiers[t.symbol]}
    # REST uses STRICT event-before-print quote linkage. An equal-time quote
    # cannot change an existing equal-time print's binding, but an older one can.
    rebuild.update(q.symbol for q in new_quotes if frontiers[q.symbol] is not None and q.event_ns<frontiers[q.symbol])
    state=MemberState(current.location,current.symbols,current.anchor_ns,current.inventory_sha256,
        max(current.through_ns,plan.end_ns),max(current.known_ns,receipt['known_ns']),
        current.trades+new_trades,current.quotes+new_quotes,observation.identity,current.ancestors+(previous,))
    return MemberMerge(state,new_trades,new_quotes,tuple(sorted(rebuild)),older)
