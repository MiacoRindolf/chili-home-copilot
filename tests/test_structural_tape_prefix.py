"""Pure evidence tests: no database, runtime import, or trading decision."""
from dataclasses import replace
from fractions import Fraction
import importlib.util
from pathlib import Path
import sys

import pytest


PATH = Path(__file__).resolve().parents[1]/'app/services/trading/momentum_neural/structural_tape_prefix.py'
SPEC = importlib.util.spec_from_file_location('structural_tape_prefix_under_test',PATH)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def tick(i, p, *, known=None, size=10, bid=None, ask=None, epoch=('run',1)):
    return m.Tick(i,p,size,bid,ask,i,i,known or i,epoch)


def prefix(n=100, front=100, active=100, stream='TNON', segment='recorded'):
    return m.Prefix(stream,segment,m.Limits(n,front,active))


def add(p, rows):
    receipt=m.FrontierReceipt(rows[0].known_ns,len(rows),m.rows_sha256(rows),p.prefix_sha256)
    return p.append_frontier(rows,receipt),receipt


def feed(p, prices):
    for i,price in enumerate(prices,1):
        result,_=add(p,[tick(i,price)])
        assert result.status=='applied'
    return p


def snapshot(p):
    return (p.count,p.prefix_sha256,p.last_receipt,p.active_references(),
            tuple(p.tick(i) for i in range(p.count)),
            tuple(p.label(i) for i in range(p.count)),
            p.mass(0) if p.count else None,p.extrema(0) if p.count else None)


def test_boundary_is_unknown_until_a_real_turn():
    p=feed(prefix(),[5,6,7])
    assert p.active_references()==()
    add(p,[tick(4,6)])
    assert [(r.kind,r.origin_id,r.confirmation_id) for r in p.active_references()]==[('peak',3,4)]


def test_equal_plateau_uses_last_source_identity_and_first_actual_reversal():
    p=feed(prefix(),[5,6,6,6,5])
    r=p.active_references()[0]
    assert (r.origin_id,r.confirmation_id)==(4,5)
    assert (r.plateau_first_id,r.plateau_first_index)==(2,1)
    assert p.extrema(0)[4:]==(6,1,3,3)


def test_equal_level_touch_does_not_strictly_breach():
    p=feed(prefix(),[5,6,5,6,5])
    assert len(p.active_references())==3
    result,_=add(p,[tick(6,4)])
    assert [(r.kind,r.origin_id) for r in result.breached]==[('valley',3)]
    assert all(r.kind=='peak' for r in p.active_references())


def test_atomic_birth_then_break_is_not_an_active_confirmation():
    p=feed(prefix(),[6,5])
    result,_=add(p,[tick(3,6,known=10),tick(4,4,known=10)])
    born_low=next(r for r in result.born if r.kind=='valley')
    assert born_low in result.breached
    assert born_low not in p.active_references()


def test_confirmation_survives_only_if_entire_frontier_does():
    p=feed(prefix(),[6,5])
    result,_=add(p,[tick(3,6,known=10),tick(4,4,known=10),tick(5,6,known=10)])
    assert any(r.origin_id==2 for r in result.breached)
    assert not any(r.origin_id==2 for r in p.active_references())
    assert any(r.kind=='valley' and r.origin_id==4 for r in p.active_references())


def test_persistent_classification_and_exact_fractional_mass():
    p=prefix()
    add(p,[tick(1,6,size=.1,bid=5,ask=6)])
    add(p,[tick(2,6,size=.2,bid=5,ask=7)])
    add(p,[tick(3,6,size=.3)])
    assert p.label(0)==(1,1)
    assert p.label(1)==p.label(2)==(1,0)
    mass=dict(zip(m.MASS_FIELDS,p.mass(0)))
    assert mass['inferred_buy']==Fraction(1,2)
    assert mass['quote_buy']==0 and mass['fallback_buy']==Fraction(1,2)


def test_initial_midpoint_is_unknown_and_new_segment_does_not_inherit_carry():
    a,b=prefix(),prefix(segment='new')
    add(a,[tick(1,6,bid=5,ask=6)])
    add(b,[tick(1,6,bid=5,ask=7)])
    assert a.label(0)==(1,1) and b.label(0)==(0,0)


def test_idempotence_validates_actual_rows():
    p=prefix(); rows=[tick(1,5)]
    _,receipt=add(p,rows)
    before=snapshot(p)
    assert p.append_frontier(rows,receipt).status=='already_applied'
    assert p.append_frontier([tick(1,6)],receipt).reason=='frontier_membership_mismatch'
    assert snapshot(p)==before


@pytest.mark.parametrize('failure', ['count','hash','prior','clock','order','mixed_epoch','duplicate'])
def test_bad_whole_frontier_preserves_all_state(failure):
    p=feed(prefix(),[5,6,5])
    rows=[tick(4,6,known=10),tick(5,7,known=10)]
    if failure=='clock': rows[1]=tick(5,7,known=11)
    if failure=='order': rows=rows[::-1]
    if failure=='mixed_epoch': rows[1]=tick(5,7,known=10,epoch=('other',1))
    if failure=='duplicate': rows[1]=replace(rows[1],id=1)
    receipt=m.FrontierReceipt(10,len(rows),m.rows_sha256(rows),p.prefix_sha256)
    if failure=='count': receipt=replace(receipt,row_count=3)
    if failure=='hash': receipt=replace(receipt,rows_sha256='0'*64)
    if failure=='prior': receipt=replace(receipt,previous_prefix_sha256='0'*64)
    before=snapshot(p)
    assert p.append_frontier(rows,receipt).status=='unresolved'
    assert snapshot(p)==before


@pytest.mark.parametrize('limits', [(3,10,10),(100,1,100),(100,10,1)])
def test_resource_failure_never_trims_rows_or_old_references(limits):
    p=m.Prefix('TNON','recorded',m.Limits(*limits))
    feed(p,[5,6,5])
    before=snapshot(p)
    result,_=add(p,[tick(4,6,known=10),tick(5,5,known=10)])
    assert result.reason=='resource_capacity_unresolved'
    assert snapshot(p)==before


def test_clock_values_do_not_select_strategy_membership():
    prices=[5,6,5,7,6,8,7]
    a,b=prefix(),prefix()
    for i,price in enumerate(prices,1):
        add(a,[tick(i,price)])
        add(b,[replace(tick(i,price),event_ns=i*10**15,received_ns=i*10**15,published_ns=i*10**15)])
    assert a.active_references()==b.active_references()
    assert a.mass(0)==b.mass(0)
    assert a.extrema(0)==b.extrema(0)


def test_source_epoch_and_reference_identity_are_not_interchangeable():
    a,b=feed(prefix(),[6,5,6]),feed(prefix(stream='OTHER'),[6,5,6])
    with pytest.raises(ValueError,match='reference_not_active'):
        a.context(b.active_references()[0])
    foreign=replace(a.active_references()[0],epoch=('other',1))
    with pytest.raises(ValueError,match='reference_not_active'):
        a.context(foreign)


def test_all_nested_active_scopes_are_retained():
    p=feed(prefix(),[10,12,10,11,10.5,11.5,11,11.4])
    valleys=[r for r in p.active_references() if r.kind=='valley']
    assert [p.tick(r.origin_index).price for r in valleys]==[10,10.5,11]
    for r in p.active_references():
        context=p.context(r)
        combined=tuple(sum(v[i] for v in context['phase_mass']) for i in range(len(m.MASS_FIELDS)))
        assert combined==context['whole_mass']
        assert sum(b-a for a,b in context['phase_bounds'])==p.count-1-r.origin_index


def test_reanchored_recovery_does_not_reclassify_old_prints():
    p=feed(prefix(),[5,7,6,7,6.5,7.5,7,7.2])
    before=tuple(p.label(i) for i in range(p.count))
    for r in p.active_references(): p.context(r)
    assert tuple(p.label(i) for i in range(p.count))==before


def test_rejected_frontier_can_be_retried_without_hidden_state_drift():
    p=feed(prefix(),[5,7,6])
    good=[tick(4,8,known=10),tick(5,7,known=10),tick(6,8,known=10)]
    bad=[*good[:-1],replace(good[-1],epoch=('wrong',1))]
    result,_=add(p,bad)
    assert result.status=='unresolved'
    assert add(p,good)[0].status=='applied'
    clean=feed(prefix(),[5,7,6])
    assert add(clean,good)[0].status=='applied'
    assert snapshot(p)==snapshot(clean)
    assert [p.context(r) for r in p.active_references()]==[clean.context(r) for r in clean.active_references()]


def test_rebuild_from_serialized_source_frontiers_is_identical():
    import json
    from dataclasses import asdict
    batches=[[tick(1,5,known=3),tick(2,6,known=3)],
             [tick(3,5,known=8),tick(4,5,known=8),tick(5,6,known=8)],
             [tick(6,7,known=9)]]
    p=prefix(); other=prefix()
    for batch in batches:
        assert add(p,batch)[0].status=='applied'
        restored=[m.Tick(**{**r,'epoch':tuple(r['epoch'])}) for r in json.loads(json.dumps([asdict(r) for r in batch]))]
        assert add(other,restored)[0].status=='applied'
    assert snapshot(p)==snapshot(other)


def test_completed_swing_micro_facts_join_first_and_last_plateau_identities():
    spec=importlib.util.spec_from_file_location('completed_facts_for_prefix_join',PATH.with_name('completed_swing_facts.py'))
    facts=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=facts
    spec.loader.exec_module(facts)
    import random
    rng=random.Random(714)
    prices=[rng.randrange(1,8) for _ in range(100)]
    p=prefix(200,20,200)
    state=facts.State('TNON','recorded')
    caps=facts.Capacities(200,200,20,1000000)
    for start in range(0,len(prices),10):
        # Exact microsecond clocks, represented in this component's ns fields.
        known=(start+10)*1000
        rows=[replace(tick(i+1,prices[i]),event_ns=(i+1)*1000,received_ns=(i+1)*1000,published_ns=known)
              for i in range(start,start+10)]
        result,_=add(p,rows)
        assert result.status=='applied'
        other_rows=[facts.Print(r.id,r.price,r.size,r.event_ns//1000,r.received_ns//1000,r.published_ns//1000,r.epoch)
                    for r in rows]
        receipt=facts.Receipt('TNON','recorded',known//1000,len(rows),other_rows[-1].cursor,
                              facts.rows_sha256(other_rows),facts.state_sha256(state))
        other=facts.reduce_frontier(state,receipt,other_rows,caps)
        assert other.status=='applied'
        state=other.state
        lows=[f for f in other.facts if f.variant==facts.MICRO]
        valleys=[r for r in result.born if r.kind=='valley']
        assert [(r.plateau_first_id,r.origin_id,r.confirmation_id) for r in valleys]==[
            (f.candidate.low.first.id,f.candidate.low.last.id,f.confirmation.id) for f in lows]
        for ref,fact in zip(valleys,lows):
            assert (ref in p.active_references())==(fact.status=='confirmed_intact_at_frontier')


def test_additional_row_from_same_known_frontier_requires_reconstruction():
    p=prefix()
    assert add(p,[tick(1,5,known=3)])[0].status=='applied'
    before=snapshot(p)
    assert add(p,[tick(2,6,known=3)])[0].reason=='late_or_conflicting_frontier'
    assert snapshot(p)==before


@pytest.mark.parametrize('origin,end',[(-1,0),(0,100),(2,1),(True,2)])
def test_range_queries_never_read_uncommitted_rows(origin,end):
    p=feed(prefix(),[5,6,7])
    with pytest.raises(ValueError): p.mass(origin,end)
    with pytest.raises(ValueError): p.extrema(origin,end)


def test_randomized_index_and_references_against_brute_force():
    import random
    rng=random.Random(713)
    prices=[rng.randrange(1,20) for _ in range(300)]
    p=prefix(400,400,400)
    references=[]; direction=0
    for i,price in enumerate(prices):
        add(p,[tick(i+1,price)])
        references=[r for r in references if (price>=prices[r[1]] if r[0]=='valley' else price<=prices[r[1]])]
        if i:
            step=(price>prices[i-1])-(price<prices[i-1])
            if step and step!=direction:
                if direction: references.append(('valley' if direction<0 else 'peak',i-1,i))
                direction=step
        assert [(r.kind,r.origin_index,r.confirmation_index) for r in p.active_references()]==references
        for start in {0,i,rng.randrange(i+1)}:
            sub=prices[start:i+1]; low,high=min(sub),max(sub)
            mins=[j for j in range(start,i+1) if prices[j]==low]
            maxs=[j for j in range(start,i+1) if prices[j]==high]
            assert p.extrema(start)==(low,mins[0],mins[-1],len(mins),high,maxs[0],maxs[-1],len(maxs))


@pytest.mark.parametrize('field,value',[('id',True),('price',float('nan')),('price',10**400),('size',0),
    ('bid',float('inf')),('published_ns',0),('epoch',('run',True))])
def test_invalid_tick_rejected(field,value):
    with pytest.raises(ValueError): replace(tick(1,5),**{field:value})


def consumer_receipt(p, rows, *, known, sequence, **changes):
    import hashlib
    prior=p.last_receipt
    values=dict(known_ns=known,row_count=len(rows),rows_sha256=m.rows_sha256(rows),
        previous_prefix_sha256=p.prefix_sha256,source_identity_sha256='a'*64,
        source_sequence=sequence,source_root_sha256=hashlib.sha256(str(sequence).encode()).hexdigest(),
        previous_source_sequence=0 if prior is None else prior.source_sequence,
        previous_source_root_sha256='0'*64 if prior is None else prior.source_root_sha256)
    return m.ConsumerFrontierReceipt(**{**values,**changes})


def test_capture_sequence_advances_at_equal_clock_without_changing_tick_clocks():
    p=prefix()
    for i,price in enumerate([6,5,6],3):
        rows=[tick(i,price,known=10)]
        receipt=consumer_receipt(p,rows,known=10,sequence=i)
        assert p.append_frontier(rows,receipt).status=='applied'
        assert p.last_receipt==receipt
    assert [(r.kind,r.origin_id,r.confirmation_id) for r in p.active_references()]==[('valley',4,5)]
    assert [p.tick(i).published_ns for i in range(p.count)]==[10]*3


def test_capture_delta_can_span_clocks_and_other_stream_sequences():
    p=prefix()
    rows=[tick(3,6,known=7),tick(5,5,known=8),tick(9,6,known=12)]
    receipt=consumer_receipt(p,rows,known=15,sequence=11,previous_source_sequence=2)
    assert p.append_frontier(rows,receipt).status=='applied'
    assert p.count==3 and p.active_references()[0].origin_id==5
    assert p.last_receipt.previous_source_sequence==2


def test_capture_empty_delta_advances_boundary_digest_but_not_market_evidence():
    p=prefix()
    rows=[tick(3,6,known=10),tick(4,5,known=10),tick(5,6,known=10)]
    assert p.append_frontier(rows,consumer_receipt(p,rows,known=10,sequence=5)).status=='applied'
    before=snapshot(p)
    receipt=consumer_receipt(p,[],known=10,sequence=8)
    assert p.append_frontier([],receipt).status=='applied'
    after=snapshot(p)
    assert before[0]==after[0] and before[3:]==after[3:]
    assert before[1]!=after[1] and p.last_receipt==receipt
    assert p.append_frontier([],receipt).status=='already_applied'
    assert snapshot(p)==after


def test_capture_segment_can_start_at_an_explicit_empty_delta():
    p=prefix()
    receipt=consumer_receipt(p,[],known=10,sequence=8,previous_source_sequence=7)
    assert p.append_frontier([],receipt).status=='applied'
    assert p.count==0 and p.active_references()==()
    rows=[tick(9,6,known=10)]
    assert p.append_frontier(rows,consumer_receipt(p,rows,known=10,sequence=9)).status=='applied'
    assert p.label(0)==(0,0)


def test_capture_duplicate_requires_identical_actual_rows():
    p=prefix();rows=[tick(3,6,known=10)]
    receipt=consumer_receipt(p,rows,known=10,sequence=3)
    assert p.append_frontier(rows,receipt).status=='applied'
    before=snapshot(p)
    assert p.append_frontier(rows,receipt).status=='already_applied'
    assert p.append_frontier([replace(rows[0],price=7)],receipt).reason=='frontier_membership_mismatch'
    assert snapshot(p)==before


@pytest.mark.parametrize('failure,reason',[
    ('identity','source_identity_change_requires_new_segment'),
    ('prior_sequence','previous_source_prefix_mismatch'),
    ('prior_root','previous_source_prefix_mismatch'),
    ('clock','late_or_conflicting_frontier'),
    ('future_row','row_outside_frontier'),
    ('old_id','row_outside_source_delta'),
    ('future_id','row_outside_source_delta'),
    ('source_order','row_outside_source_delta'),
    ('late_event','late_or_duplicate_tick'),
    ('epoch','epoch_change_requires_new_segment'),
])
def test_capture_rejects_conflicting_delta_atomically(failure,reason):
    p=prefix();first=[tick(3,6,known=10)]
    assert p.append_frontier(first,consumer_receipt(p,first,known=10,sequence=4)).status=='applied'
    rows=[tick(5,5,known=10),tick(7,6,known=10)]
    changes={}
    if failure=='identity': changes['source_identity_sha256']='b'*64
    if failure=='prior_sequence': changes['previous_source_sequence']=3
    if failure=='prior_root': changes['previous_source_root_sha256']='c'*64
    if failure=='clock': changes['known_ns']=9
    if failure=='future_row': rows[1]=replace(rows[1],published_ns=11)
    if failure=='old_id': rows[0]=replace(rows[0],id=4)
    if failure=='future_id': rows[1]=replace(rows[1],id=9)
    if failure=='source_order': rows[1]=replace(rows[1],id=5)
    if failure=='late_event': rows[1]=replace(rows[1],event_ns=2)
    if failure=='epoch': rows[1]=replace(rows[1],epoch=('other',2))
    receipt=consumer_receipt(p,rows,known=10,sequence=8)
    receipt=replace(receipt,**changes)
    before=snapshot(p)
    assert p.append_frontier(rows,receipt).reason==reason
    assert snapshot(p)==before


@pytest.mark.parametrize('consumer_first',[False,True])
def test_recorded_and_capture_contracts_cannot_mix(consumer_first):
    p=prefix();rows=[tick(1,6)]
    receipt=consumer_receipt(p,rows,known=1,sequence=1) if consumer_first else m.FrontierReceipt(1,1,m.rows_sha256(rows),p.prefix_sha256)
    assert p.append_frontier(rows,receipt).status=='applied'
    rows=[tick(2,5)]
    receipt=(m.FrontierReceipt(2,1,m.rows_sha256(rows),p.prefix_sha256) if consumer_first else
        m.ConsumerFrontierReceipt(2,1,m.rows_sha256(rows),p.prefix_sha256,'a'*64,2,'b'*64,1,'c'*64))
    before=snapshot(p)
    assert p.append_frontier(rows,receipt).reason=='frontier_contract_change_requires_new_segment'
    assert snapshot(p)==before


@pytest.mark.parametrize('change',[
    {'row_count':-1},{'row_count':True},{'known_ns':1.0},{'source_sequence':True},
    {'previous_source_sequence':-1},{'previous_source_sequence':3},
    {'source_identity_sha256':'not-a-digest'}, {'previous_source_root_sha256':'A'*64},
    {'source_root_sha256':'0'*64},
])
def test_invalid_capture_receipt(change):
    p=prefix();rows=[tick(3,6)]
    with pytest.raises(ValueError): consumer_receipt(p,rows,known=3,sequence=3,**change)


def test_capture_proof_changes_digest_without_changing_geometry_or_mass():
    a,b=prefix(),prefix()
    rows=[tick(i,price,known=10) for i,price in enumerate([6,5,6,7,6],1)]
    assert add(a,rows)[0].status=='applied'
    assert b.append_frontier(rows,consumer_receipt(b,rows,known=10,sequence=7)).status=='applied'
    assert a.prefix_sha256!=b.prefix_sha256
    assert a.active_references()==b.active_references()
    assert a.mass(0)==b.mass(0) and a.extrema(0)==b.extrema(0)
    assert [a.label(i) for i in range(5)]==[b.label(i) for i in range(5)]


def test_capture_capacity_failure_can_retry_same_valid_source_delta():
    p=prefix(active=1)
    rows=[tick(i,price,known=10) for i,price in enumerate([5,6,5,6],1)]
    receipt=consumer_receipt(p,rows,known=10,sequence=4)
    before=snapshot(p)
    assert p.append_frontier(rows,receipt).reason=='resource_capacity_unresolved'
    assert snapshot(p)==before
    p.limits=m.Limits(100,100,2)
    assert p.append_frontier(rows,receipt).status=='applied'
    assert p.last_receipt==receipt


def event_keys(events):
    return [(e.kind,e.reference.kind,e.reference.origin_id,e.at_id) for e in events]


def test_ordered_events_keep_transient_turns_and_exact_breach_print():
    p=feed(prefix(),[6,5])
    rows=[tick(i,price,known=10) for i,price in enumerate([6,4,6],3)]
    result,_=add(p,rows)
    assert event_keys(result.events)==[
        ('born','valley',2,3), ('breached','valley',2,4),
        ('born','peak',3,4), ('born','valley',4,5)]
    for event in result.events:
        assert p.tick(event.at_index).id==event.at_id
    assert tuple(e.reference for e in result.events if e.kind=='born')==result.born
    assert tuple(e.reference for e in result.events if e.kind=='breached')==result.breached


def test_one_print_breaches_nested_peaks_in_stack_order_before_birth():
    p=feed(prefix(),[10,12,10,11,10.5])
    result,_=add(p,[tick(6,13)])
    assert event_keys(result.events)==[
        ('breached','peak',4,6), ('breached','peak',2,6), ('born','valley',5,6)]


def test_source_events_do_not_depend_on_consumer_batch_partition():
    # All 128 contiguous partitions of this same source stream. Receipt digests
    # differ by design; source events, labels, mass and active state must not.
    prices=[10,12,12,10,11,10.5,13,9]
    rows=[tick(i,price,known=20) for i,price in enumerate(prices,1)]
    full=prefix()
    expected=full.append_frontier(rows,consumer_receipt(full,rows,known=20,sequence=len(rows)))
    assert expected.status=='applied'
    for mask in range(1 << (len(rows)-1)):
        p=prefix(); events=[]; start=0
        ends=[i for i in range(1,len(rows)) if mask & (1 << (i-1))]+[len(rows)]
        for end in ends:
            chunk=rows[start:end]
            result=p.append_frontier(chunk,consumer_receipt(p,chunk,known=20,sequence=end))
            assert result.status=='applied'
            events.extend(result.events)
            start=end
        assert tuple(events)==expected.events
        assert p.active_references()==full.active_references()
        assert p.mass(0)==full.mass(0)
        assert [p.label(i) for i in range(p.count)]==[full.label(i) for i in range(full.count)]


def test_uncommitted_and_repeated_reads_emit_no_structural_events():
    p=prefix(active=1)
    rows=[tick(i,price,known=10) for i,price in enumerate([5,6,5,6],1)]
    receipt=consumer_receipt(p,rows,known=10,sequence=4)
    rejected=p.append_frontier(rows,receipt)
    assert rejected.status=='unresolved' and rejected.events==()
    assert p.count==0 and p.active_references()==()
    p.limits=m.Limits(100,100,2)
    committed=p.append_frontier(rows,receipt)
    assert committed.status=='applied' and len(committed.events)==2
    assert p.append_frontier(rows,receipt).events==()
    empty=consumer_receipt(p,[],known=10,sequence=5)
    assert p.append_frontier([],empty).events==()


def test_event_births_and_first_breaches_match_raw_plateaus_and_forward_search():
    import random
    rng=random.Random(19)
    prices=[rng.randrange(1,12) for _ in range(400)]
    runs=[]
    for index,price in enumerate(prices):
        if runs and runs[-1][0]==price:
            runs[-1][2]=index
        else:
            runs.append([price,index,index])
    expected=[]
    for left,middle,right in zip(runs,runs[1:],runs[2:]):
        kind=('valley' if middle[0]<left[0] and middle[0]<right[0]
              else 'peak' if middle[0]>left[0] and middle[0]>right[0] else None)
        if kind is None:
            continue
        origin,confirmation=middle[2],right[1]
        expected.append(('born',kind,origin+1,confirmation+1))
        for at in range(confirmation+1,len(prices)):
            if prices[at]<middle[0] if kind=='valley' else prices[at]>middle[0]:
                expected.append(('breached',kind,origin+1,at+1))
                break
    p=prefix(n=len(prices),front=len(prices),active=len(prices))
    rows=[tick(i,price,known=len(prices)) for i,price in enumerate(prices,1)]
    result,_=add(p,rows)
    assert result.status=='applied'
    # Sort only for this raw set comparison; separate tests pin source ordering.
    assert sorted(event_keys(result.events))==sorted(expected)
    assert len(result.events)==len(set(event_keys(result.events)))
