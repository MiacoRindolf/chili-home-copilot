"""All-symbol REST acquisition from observed per-channel frontiers.

Frontiers reduce transport work; they assert neither provider finality nor a
strategy horizon. A full-anchor audit is still required to discover older late
members. Every group retains its actual request and exhausted page-chain receipt.
This module has no order endpoint, scheduler, or authority to publish a strategy.
"""
from bisect import bisect_left
from dataclasses import asdict, dataclass
import json

from scripts.crypto_history_pages import HistoryRequest, HistoryWalk, canonical, sha
from .history_source import QuoteHistoryWalk, _event_messages


@dataclass(frozen=True)
class ChannelGroup:
    channel: str
    request: HistoryRequest
    known_overlap_rows: int
    estimated_pages: int


@dataclass(frozen=True)
class FrontierPlan:
    groups: tuple[ChannelGroup, ...]
    symbols: tuple[str, ...]
    prior_observation_sha256: str
    observed_through_ns: int
    anchor_ns: int
    end_ns: int
    mode: str

    def receipt(self):
        return dict(contract='native_grouped_frontier_plan_v1',
            groups=[asdict(g) for g in self.groups], symbols=self.symbols,
            prior_observation_sha256=self.prior_observation_sha256,
            observed_through_ns=self.observed_through_ns, anchor_ns=self.anchor_ns,
            end_ns=self.end_ns, mode=self.mode,
            optimization_scope='contiguous_observed_frontier_partitions',
            objective='lexicographic_known_page_units_then_overlap_rows_then_request_groups',
            future_arrivals_predicted=False, opportunity_ranking=False,
            provider_finality_certified=False, order_authority=False)

    @property
    def identity(self):
        return sha(canonical(self.receipt()))


def _partition(histories, anchor_ns, page_limit):
    times={s:tuple(sorted(v)) for s,v in histories.items()}
    frontiers={s:v[-1] if v else anchor_ns for s,v in times.items()}
    members=sorted(times,key=lambda s:(frontiers[s],s))
    # Dynamic programming is over transport groups, never selected opportunities.
    best=[((0,0,0),())]+[None]*len(members)
    for end in range(1,len(members)+1):
        candidates=[]
        for start in range(end):
            lower=frontiers[members[start]];symbols=members[start:end]
            overlap=sum(len(times[s])-bisect_left(times[s],lower) for s in symbols)
            pages=max(1,(overlap+page_limit-1)//page_limit)
            score,path=best[start]
            candidates.append(((score[0]+pages,score[1]+overlap,score[2]+1),
                path+((tuple(sorted(symbols)),lower,overlap,pages),)))
        best[end]=min(candidates,key=lambda candidate:candidate[0])
    return best[-1][1]


def plan_frontiers(*, histories, location, symbols, anchor_ns, observed_through_ns,
                   end_ns, page_limit, inventory_sha256, prior_observation_sha256,
                   mode='observed_frontiers'):
    """Plan exclusively from prior observed members, including empty channels.

    ``full_anchor_audit`` scans the original acquisition range. Its eventual
    result must merge by the exact request interval, never replace newer state.
    """
    template=HistoryRequest(location,symbols,anchor_ns,end_ns,page_limit,inventory_sha256)
    if (type(observed_through_ns) is not int or
            not anchor_ns<=observed_through_ns<=end_ns or
            type(prior_observation_sha256) is not str or len(prior_observation_sha256)!=64 or
            any(c not in '0123456789abcdef' for c in prior_observation_sha256) or
            mode not in ('observed_frontiers','full_anchor_audit') or
            type(histories) is not dict or set(histories)!={'trades','quotes'}):
        raise ValueError('native_frontier_prior_observation_invalid')
    groups=[]
    for kind in ('trades','quotes'):
        history=histories[kind]
        if type(history) is not dict or set(history)!=set(symbols):
            raise ValueError('native_frontier_whole_inventory_required')
        if any(type(values) not in (list,tuple) or any(type(t) is not int or
                not anchor_ns<=t<=observed_through_ns for t in values) for values in history.values()):
            raise ValueError('native_frontier_unobserved_or_invalid_event')
        if mode=='full_anchor_audit':
            count=sum(len(v) for v in history.values())
            partitions=((symbols,anchor_ns,count,max(1,(count+page_limit-1)//page_limit)),)
        else:
            partitions=_partition(history,anchor_ns,page_limit)
        for members,start,count,pages in partitions:
            request=HistoryRequest(template.location,members,start,end_ns,page_limit,inventory_sha256)
            groups.append(ChannelGroup(kind,request,count,pages))
    result=FrontierPlan(tuple(groups),symbols,prior_observation_sha256,
        observed_through_ns,anchor_ns,end_ns,mode)
    validate_plan(result)
    return result


def validate_plan(plan):
    if type(plan) is not FrontierPlan or not plan.groups or type(plan.groups) is not tuple:
        raise ValueError('native_frontier_plan_required')
    if (plan.mode not in ('observed_frontiers','full_anchor_audit') or
            any(type(t) is not int for t in (plan.anchor_ns,plan.observed_through_ns,plan.end_ns)) or
            not 0<plan.anchor_ns<=plan.observed_through_ns<=plan.end_ns or
            type(plan.symbols) is not tuple or not plan.symbols or
            type(plan.prior_observation_sha256) is not str or len(plan.prior_observation_sha256)!=64 or
            any(c not in '0123456789abcdef' for c in plan.prior_observation_sha256)):
        raise ValueError('native_frontier_plan_binding_invalid')
    for channel in ('trades','quotes'):
        members=tuple(sorted(s for g in plan.groups if g.channel==channel for s in g.request.symbols))
        if members!=plan.symbols:raise ValueError('native_frontier_exact_channel_coverage_required')
    first=plan.groups[0].request
    for group in plan.groups:
        request=group.request
        if (group.channel not in ('trades','quotes') or type(request) is not HistoryRequest or
                request.end_ns!=plan.end_ns or not plan.anchor_ns<=request.start_ns<=plan.observed_through_ns or
                (request.location,request.inventory_sha256,request.page_limit)!=
                (first.location,first.inventory_sha256,first.page_limit) or
                type(group.known_overlap_rows) is not int or group.known_overlap_rows<0 or
                type(group.estimated_pages) is not int or group.estimated_pages<1):
            raise ValueError('native_frontier_group_binding_invalid')
        if plan.mode=='full_anchor_audit' and request.start_ns!=plan.anchor_ns:
            raise ValueError('native_frontier_audit_anchor_required')


@dataclass(frozen=True)
class GroupedObservation:
    plan: FrontierPlan
    trades: tuple
    quotes: tuple
    receipt_json: str

    @property
    def identity(self):return sha(self.receipt_json)


def collect_frontiers(plan, get, *, record, max_pages, max_trades, max_quotes, max_page_bytes):
    """Collect every group atomically as one input observation.

    ``record`` must durably retain the supplied envelope before returning. The
    returned HTTP body must match that exact recorded response. No partial group
    success is a complete observation. Resource caps apply to the whole pass,
    not independently multiplied by the number of groups. There are no retries.
    """
    validate_plan(plan)
    if any(type(n) is not int or n<=0 for n in (max_pages,max_trades,max_quotes,max_page_bytes)):
        raise ValueError('native_frontier_global_resource_bounds_required')
    record(dict(kind='frontier_observation_started',plan_sha256=plan.identity,plan=plan.receipt()))
    values={'trades':[],'quotes':[]};receipts=[];pages=0;known=0
    for index,group in enumerate(plan.groups):
        request=group.request;channel=group.channel
        walk=(HistoryWalk(request,max_pages=max_pages,max_trades=max_trades,max_page_bytes=max_page_bytes)
            if channel=='trades' else QuoteHistoryWalk(request,max_pages=max_pages,max_quotes=max_quotes,max_page_bytes=max_page_bytes))
        while not walk.complete:
            if pages>=max_pages:raise ValueError('native_frontier_total_page_capacity')
            parameters=walk.parameters();expected_parameters=canonical(parameters);recorded=[]
            def retain(value):
                # The HTTP implementation must not mutate an already recorded
                # envelope and then return different bytes under that receipt.
                value=json.loads(canonical(value))
                expected='/v1beta3/crypto/'+request.location+'/'+channel
                if value.get('path')!=expected or canonical(value.get('parameters'))!=expected_parameters:
                    raise ValueError('native_frontier_transport_binding_changed')
                recorded.append(json.loads(canonical(value)))
                record(dict(kind='frontier_transport',plan_sha256=plan.identity,group_index=index,transport=value))
                headers={k.lower():str(v) for k,v in value.get('rate_headers',{}).items()}
                if value.get('status')==429 or headers.get('x-ratelimit-remaining')=='0':
                    raise ValueError('native_frontier_provider_rate_exhausted')
            raw,received=get(location=request.location,kind=channel,parameters=parameters,record=retain)
            body=raw.encode() if type(raw) is str else raw
            if len(recorded)!=1 or recorded[0].get('kind')!='http_response':
                raise ValueError('native_frontier_recorded_response_required')
            evidence=recorded[0]
            if (evidence.get('status')!=200 or evidence.get('complete') is not True or
                    evidence.get('body_hex')!=body.hex() or evidence.get('body_sha256')!=sha(body) or
                    evidence.get('received_ns')!=received):
                raise ValueError('native_frontier_returned_response_changed')
            walk.accept(body,received_ns=received);pages+=1;known=max(known,received)
            # Includes raw duplicate quote rows, so memory stays bounded too.
            count=walk.receipt()['unique_trades'] if channel=='trades' else len(walk.quotes)
            maximum=max_trades if channel=='trades' else max_quotes
            if len(values[channel])+count>maximum:raise ValueError('native_frontier_total_member_capacity')
        rows=walk.complete_trades() if channel=='trades' else tuple(walk.quotes)
        values[channel].extend(rows)
        receipts.append(dict(group_index=index,channel=channel,request=asdict(request),receipt=walk.receipt()))
    result=dict(contract='native_grouped_frontier_observation_v1',plan_sha256=plan.identity,
        groups=receipts,total_pages=pages,known_ns=known,
        trade_count=len(values['trades']),quote_count=len(values['quotes']),
        content_sha256=sha(canonical(_event_messages(values['trades'],values['quotes']))),
        all_requested_channel_page_chains_exhausted=True,provider_finality_certified=False,
        full_history_audit_required=plan.mode!='full_anchor_audit',order_authority=False)
    raw=canonical(result)
    record(dict(kind='frontier_observation_complete',receipt=result,receipt_sha256=sha(raw)))
    return GroupedObservation(plan,tuple(values['trades']),tuple(values['quotes']),raw)
