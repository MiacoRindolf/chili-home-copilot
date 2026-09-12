"""Read ordinary and adaptive PAPER economics for a native crypto admission."""
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import text

from .account_bridge import require_account_lock
from .funding import FundingClaim
from .lifecycle import decimal_text,digest
from .store import ACCOUNT_SCOPE,ADAPTIVE_NAMESPACE
from .truth import identity


def require_equity_account_locks(c):
    require_account_lock(c)
    if c.execute(text('SHOW transaction_isolation')).scalar_one()!='read committed':
        raise ValueError('native_equity_read_committed_required')
    held=c.execute(text('''SELECT EXISTS (SELECT 1 FROM pg_locks
        WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted
        AND classid=:ns AND objid=(hashtext(:scope)::bigint & 4294967295) AND objsubid=2)'''),
        dict(ns=ADAPTIVE_NAMESPACE,scope=ACCOUNT_SCOPE)).scalar_one()
    if held is not True:raise ValueError('native_equity_adaptive_account_lock_required')


def number(value,*,zero=False):
    if isinstance(value,bool) or value is None:raise ValueError('native_equity_amount_unavailable')
    value=Fraction(str(value))
    if value<0 or value==0 and not zero:raise ValueError('native_equity_amount_invalid')
    return value


def locked_equity_snapshot(c,*,account_id):
    """Every unresolved claim and exposure-bearing session, plus adaptive heads.

    Ordinary whole-share evidence stays in its original domain. A matching
    pending CID/quantity/price can prove an adaptive/ordinary duplicate; other
    overlap is retained conservatively and named in the receipt. No broker
    reflection or release is inferred from wall-clock age or FSM terminality.
    """
    from app.services.trading.momentum_neural import alpaca_orphan_claims as ordinary
    from app.services.trading.momentum_neural.ordinary_alpaca_ledger import ordinary_risk_totals
    from app.services.trading.momentum_neural.alpaca_paper_identity import alpaca_paper_account_identity_sha256
    from app.services.trading.momentum_neural.adaptive_risk_policy import load_and_verify_adaptive_risk_decision_packet,resolve_adaptive_risk
    from app.services.trading.momentum_neural.adaptive_risk_reservation import _sha256_json,load_adaptive_risk_reservation_request

    account_id=identity(account_id);require_equity_account_locks(c)
    expected_hash=alpaca_paper_account_identity_sha256(account_id)
    claims=c.execute(text('''SELECT upper(symbol),action,owner_session_id,client_order_id,
        claim_token,phase,broker_order_id,metadata_json FROM broker_symbol_action_claims
        WHERE account_scope=:scope AND phase<>'resolved' ORDER BY symbol'''),dict(scope=ACCOUNT_SCOPE)).all()
    rows=c.execute(text(ordinary.alpaca_ledger_session_scan_sql()),
        ordinary.alpaca_ledger_session_scan_params(owner_session_id=None,
            claim_owner_session_ids=[r[2] for r in claims])).all()
    owners={int(row[0]):row for row in rows};positions=[];pending=[];claimed=set();skipped=[]
    instructions={}

    def owner(row):
        sid,symbol,family,state,snap=row
        if type(snap) is not dict:raise ValueError('native_equity_owner_snapshot_unreadable')
        live=snap.get('momentum_live_execution')
        if (family!='alpaca_spot' or snap.get('alpaca_account_scope')!=ACCOUNT_SCOPE or
                snap.get('alpaca_account_id')!=account_id or
                not ordinary._certified_long_execution_envelope(live)):
            raise ValueError('native_equity_owner_identity_or_direction_mismatch')
        return live

    def instruction(sid,symbol,cid,request,risk):
        if (not ordinary._certified_frozen_entry_request(request,symbol=symbol,client_order_id=cid) or
                request.get('alpaca_account_id')!=account_id or request.get('order_type')!='limit'):
            raise ValueError('native_equity_bounded_owned_instruction_required')
        quantity=number(request.get('base_size'));price=number(request.get('limit_price'));risk=number(risk)
        if quantity.denominator!=1:raise ValueError('native_equity_whole_share_instruction_required')
        if cid in instructions:raise ValueError('native_equity_duplicate_instruction')
        instructions[cid]=dict(owner_id=sid,symbol=symbol,quantity=decimal_text(quantity),
            limit_price=decimal_text(price),risk=decimal_text(risk),debit=decimal_text(quantity*price))
        pending.append(dict(owner_id=sid,symbol=symbol,client_order_id=cid,quantity=decimal_text(quantity),
                            limit_price=decimal_text(price),reserved_risk_usd=decimal_text(risk)))

    for row in rows:
        sid,symbol,family,state,snap=row
        live=snap.get('momentum_live_execution',{}) if type(snap) is dict else {}
        if type(live) is dict and live.get('position') is not None:
            live=owner(row);pos=live['position']
            if type(pos) is not dict:raise ValueError('native_equity_position_unreadable')
            quantity=number(pos.get('quantity'))
            if quantity.denominator!=1:raise ValueError('native_equity_whole_share_position_required')
            positions.append(dict(owner_id=int(sid),symbol=symbol,client_order_id=str(live.get('entry_client_order_id') or ''),
                quantity=pos.get('quantity'),entry_price=pos.get('avg_entry_price'),stop_price=pos.get('stop_price')))

    for symbol,action,sid,cid,token,phase,oid,meta in claims:
        meta=meta if type(meta) is dict else {}
        if action=='entry' and phase=='claimed' and cid is None and oid is None and not meta.get('order_request') and meta.get('reserved_risk_usd') is None:
            skipped.append(dict(kind='watch_only',symbol=symbol,claim_token=token));continue
        if action!='entry':raise ValueError('native_equity_unresolved_non_entry_claim')
        if sid is None or int(sid) not in owners:raise ValueError('native_equity_claim_owner_missing')
        row=owners[int(sid)];owner(row)
        frozen=meta.get('order_request');frozen=frozen if type(frozen) is dict else {}
        if row[1]!=symbol or (meta.get('alpaca_account_id') or frozen.get('alpaca_account_id'))!=account_id:
            raise ValueError('native_equity_claim_identity_mismatch')
        if not ordinary._certified_frozen_entry_request(frozen,symbol=symbol,client_order_id=str(cid or '')):
            # Adaptive economic admission can precede materialization of the
            # literal order request. Its complete immutable request/packet binds
            # the same dollars; incomplete ordinary JSON must not hide them.
            pair=ordinary._adaptive_reservation_from_container(meta)
            request=load_adaptive_risk_reservation_request(meta.get('adaptive_risk_reservation_request'))
            if pair is None:raise ValueError('native_equity_pending_instruction_unavailable')
            claim=pair[2]
            if (claim.account_identity_sha256!=expected_hash or request.inputs.account_identity_sha256!=expected_hash or
                    resolve_adaptive_risk(request.policy,request.inputs).decision_packet_sha256!=claim.decision_packet_sha256 or
                    request.account_scope!=ACCOUNT_SCOPE or request.client_order_id!=cid or claim.symbol!=symbol or
                    request.inputs.symbol!=symbol or number(frozen.get('base_size'))!=claim.quantity_shares or
                    frozen.get('side')!='buy' or frozen.get('product_id')!=symbol or frozen.get('client_order_id')!=cid):
                raise ValueError('native_equity_adaptive_pending_binding_mismatch')
            if ('limit_price' in frozen and number(frozen['limit_price'])!=number(request.entry_limit_price) or
                    'order_type' in frozen and frozen['order_type']!='limit' or
                    'position_intent' in frozen and frozen['position_intent']!='buy_to_open'):
                raise ValueError('native_equity_adaptive_partial_instruction_conflict')
            frozen=dict(product_id=symbol,client_order_id=cid,alpaca_account_id=account_id,side='buy',
                base_size=str(claim.quantity_shares),limit_price=str(request.entry_limit_price),
                order_type='limit',position_intent='buy_to_open',time_in_force='day',extended_hours=False)
        instruction(int(sid),symbol,str(cid or ''),frozen,meta.get('reserved_risk_usd'))
        claimed.add((int(sid),str(cid)))
    for row in rows:
        sid,symbol,family,state,snap=row
        live=snap.get('momentum_live_execution',{}) if type(snap) is dict else {}
        if type(live) is not dict or state!='live_pending_entry' or not live.get('entry_submitted'):continue
        cid=str(live.get('entry_client_order_id') or '')
        if (int(sid),cid) in claimed:continue
        owner(row)
        if ordinary._legacy_pending_reservation_usd(live,symbol=symbol,client_order_id=cid) is None:
            # Same legacy rule as ordinary admission. Complete broker ownership
            # is separately required by the production native admission reader.
            skipped.append(dict(kind='legacy_pending_without_certified_instruction',owner_id=int(sid),symbol=symbol));continue
        instruction(int(sid),symbol,cid,live['entry_order_request'],live['entry_inflight_risk_usd'])

    totals=ordinary_risk_totals(positions=positions,pending=pending,candidate_symbol='',exact=True)
    risk=number(totals['account_open_risk_usd'],zero=True)+number(totals['active_claim_risk_usd'],zero=True)
    funding={cid:number(item['debit']) for cid,item in instructions.items()}
    # Include every non-final state, even when a damaged projection says zero;
    # also include economic dimensions on allegedly final rows.
    adaptive=c.execute(text('''SELECT r.*,p.account_identity_sha256 AS packet_account,
        p.client_order_id AS packet_cid,p.symbol AS packet_symbol,p.execution_family AS packet_family,
        p.account_scope AS packet_scope,p.broker_environment AS packet_environment,
        p.admission_accepted,p.decision_packet_json,p.entry_limit_price,
        e.event_sha256 AS event_head,e.payload_json AS event_payload
        FROM adaptive_risk_reservations r
        LEFT JOIN adaptive_risk_decision_packets p USING(decision_packet_sha256)
        LEFT JOIN adaptive_risk_reservation_events e ON e.reservation_id=r.reservation_id AND e.sequence=r.event_sequence
        WHERE r.account_scope=:scope AND (r.state NOT IN ('released','closed') OR r.open_quantity_shares>0
          OR r.pending_structural_risk_usd>0 OR r.open_structural_risk_usd>0
          OR r.pending_gross_notional_usd>0 OR r.open_gross_notional_usd>0
          OR r.pending_buying_power_impact_usd>0 OR r.open_buying_power_impact_usd>0)
        ORDER BY r.reservation_id'''),dict(scope=ACCOUNT_SCOPE)).mappings().all()
    adaptive_receipts=[];unavailable=[];deduplicated=[];uncertain_overlap=[];adaptive_instructions={}
    for row in adaptive:
        payload=row['event_payload'];cid=row['packet_cid']
        resolved=load_and_verify_adaptive_risk_decision_packet(row['decision_packet_json'])
        if (row['packet_account']!=expected_hash or row['packet_scope']!=ACCOUNT_SCOPE or
                row['packet_family']!='alpaca_spot' or row['packet_environment']!='paper' or
                row['packet_symbol']!=row['symbol'] or row['admission_accepted'] is not True or
                resolved.decision_packet_sha256!=row['decision_packet_sha256'] or
                type(payload) is not dict or _sha256_json(payload)!=row['event_head'] or
                row['event_head']!=row['last_event_sha256'] or payload['sequence']!=row['event_sequence'] or
                payload['reservation_id']!=str(row['reservation_id']) or payload['state']!=row['state']):
            raise ValueError('native_equity_adaptive_identity_or_head_unverified')
        if resolved.input_snapshot['account_identity_sha256']!=expected_hash:
            raise ValueError('native_equity_adaptive_packet_account_mismatch')
        for key in ('planned_quantity_shares','cumulative_filled_quantity_shares','open_quantity_shares'):
            if payload[key]!=row[key]:raise ValueError('native_equity_adaptive_quantity_projection_changed')
        dimensions={}
        for kind in ('open','pending'):
            for field in ('structural_risk_usd','gross_notional_usd','buying_power_impact_usd'):
                name=kind+'_'+field
                actual=number(row[name],zero=True)
                if actual!=number(payload[kind][field],zero=True):raise ValueError('native_equity_adaptive_economic_projection_changed')
                dimensions[name]=actual
        if row['state'] in ('exposure_quarantined','flat_pending_settlement'):
            unavailable.append(dict(reservation_id=str(row['reservation_id']),reason=row['state']))
        current_risk=dimensions['open_structural_risk_usd']+dimensions['pending_structural_risk_usd']
        debit=dimensions['pending_gross_notional_usd']
        adaptive_instructions[cid]=dict(symbol=row['symbol'],quantity=str(row['planned_quantity_shares']),
            limit_price=decimal_text(number(row['entry_limit_price'])))
        if cid in instructions:
            prior=instructions[cid]
            if (prior['symbol']!=row['symbol'] or number(prior['quantity'])!=row['planned_quantity_shares'] or
                    number(prior['limit_price'])!=number(row['entry_limit_price'])):
                raise ValueError('native_equity_cross_ledger_instruction_conflict')
            # A current ordinary pending claim already covers its matched partial
            # position under ordinary_risk_totals. Keep the larger risk/debit.
            risk+=max(Fraction(0),current_risk-number(prior['risk']))
            funding[cid]=max(funding[cid],debit)
            deduplicated.append(cid)
        else:
            risk+=current_risk
            if debit:funding[cid]=debit
            if any(p['symbol']==row['symbol'] for p in positions):uncertain_overlap.append(cid)
        adaptive_receipts.append(dict(reservation_id=str(row['reservation_id']),client_order_id=cid,
            state=row['state'],event_sequence=row['event_sequence'],head_sha256=row['event_head'],
            packet_sha256=row['decision_packet_sha256'],risk=decimal_text(current_risk),debit=decimal_text(debit)))
    receipt=dict(account_id=account_id,ordinary_totals=totals,ordinary_instructions=instructions,
        ordinary_positions=positions,adaptive=adaptive_receipts,adaptive_instructions=adaptive_instructions,skipped=skipped,
        deduplicated_pending_client_ids=deduplicated,possible_held_overlap_charged_conservatively=uncertain_overlap,
        unavailable_reasons=unavailable,external_risk_upper_bound=decimal_text(risk),
        external_debit_upper_bound=decimal_text(sum(funding.values(),Fraction(0))),broker_reflection_credit='0')
    receipt['snapshot_sha256']=digest(receipt)
    return dict(external_claims=[FundingClaim('equity:'+cid,account_id,'USD',Decimal(decimal_text(debit)),Decimal(0))
                for cid,debit in sorted(funding.items())],external_risk_upper_bound=decimal_text(risk),receipt=receipt)


def verify_equity_census_bounds(snapshot,*,positions,orders):
    """Do not let legacy display-float tolerances certify a funding instruction."""
    receipt=snapshot['receipt'];expected={};observed={}
    for p in receipt['ordinary_positions']:
        expected[p['symbol']]=expected.get(p['symbol'],Fraction(0))+number(p['quantity'])
    for row in positions:
        if row.get('asset_class')=='crypto':continue
        if row.get('asset_class')!='us_equity' or row.get('side')!='long':
            raise ValueError('native_equity_broker_position_domain_unverified')
        quantity=number(row.get('qty'))
        if quantity.denominator!=1:raise ValueError('native_equity_broker_whole_share_quantity_required')
        symbol=row.get('symbol')
        observed[symbol]=observed.get(symbol,Fraction(0))+quantity
    if expected!=observed:raise ValueError('native_equity_exact_broker_position_mismatch')
    bound=dict(receipt['adaptive_instructions']);bound.update(receipt['ordinary_instructions'])
    for row in orders:
        if row.get('asset_class')=='crypto':continue
        if row.get('asset_class')!='us_equity':raise ValueError('native_equity_broker_order_domain_unverified')
        if row.get('side')=='sell':continue  # Existing exact owner handles exits; no new debit.
        instruction=bound.get(row.get('client_order_id'))
        if (row.get('side')!='buy' or instruction is None or row.get('type')!='limit' or
                row.get('symbol')!=instruction['symbol'] or row.get('notional') is not None or
                number(row.get('qty'))!=number(instruction['quantity']) or
                number(row.get('limit_price'))!=number(instruction['limit_price']) or
                row.get('replaces') is not None or row.get('replaced_by') is not None):
            raise ValueError('native_equity_broker_buy_instruction_changed')
    return dict(exact_equity_positions=True,pending_buy_instructions_bound=True)
