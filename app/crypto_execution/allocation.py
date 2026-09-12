"""Exact native-lot allocation for concurrent PAPER participation.

Objective: include every affordable candidate. When minimum lots cannot all
fit, maximize the number of candidates by ascending minimum cost (exchange
argument); this is a funding conflict, not a momentum-quality rank. For the
chosen set solve sum(max(minimum_i, level)) = available capital, then floor each
quantity to its broker lattice. This equalizes continuous notional subject to
minimums and leaves an explicit rounding residual. It is not a profit optimum.
"""
from fractions import Fraction

from .lifecycle import decimal_text
from .truth import asset_identity,decimal,identity


def allocate_native_lots(opportunities,*,funding_available,risk_available):
    funding=Fraction(decimal(funding_available,zero=True));risk=Fraction(decimal(risk_available,zero=True))
    budget=min(funding,risk);rows=[];seen=set();assets=set()
    for opportunity in opportunities:
        cid=identity(opportunity['cycle_id']);asset=opportunity['asset'];aid,symbol=asset_identity(asset)
        if cid in seen or aid in assets:raise ValueError('native_allocation_duplicate_candidate')
        seen.add(cid);assets.add(aid)
        price=Fraction(decimal(opportunity['limit_price']));step=Fraction(decimal(asset['min_trade_increment']))
        minimum=Fraction(decimal(asset['min_order_size']));price_step=Fraction(decimal(asset['price_increment']))
        if (symbol.split('/')[1]!='USD' or asset.get('status')!='active' or asset.get('tradable') is not True or
                (price/price_step).denominator!=1):raise ValueError('native_allocation_native_usd_instruction_required')
        lots=-(-minimum//step);minimum_debit=lots*step*price
        rows.append(dict(cycle_id=cid,asset_id=aid,symbol=symbol,price=price,step=step,
            minimum_lots=lots,minimum_debit=minimum_debit,lot_cost=step*price))
    total_min=sum((r['minimum_debit'] for r in rows),Fraction(0))
    conflict=total_min>budget
    chosen=[];remaining=budget
    for row in sorted(rows,key=lambda r:(r['minimum_debit'],r['asset_id'])) if conflict else rows:
        if row['minimum_debit']<=remaining:
            chosen.append(row);remaining-=row['minimum_debit']
    # Continuous max-min notional water level with heterogeneous lower bounds.
    active=list(chosen);fixed=Fraction(0);level=Fraction(0)
    while active:
        level=(budget-fixed)/len(active)
        above=[r for r in active if r['minimum_debit']>level]
        if not above:break
        fixed+=sum((r['minimum_debit'] for r in above),Fraction(0))
        active=[r for r in active if r not in above]
    selected={r['cycle_id'] for r in chosen};allocated=[];deferred=[]
    for row in rows:
        if row['cycle_id'] not in selected:
            deferred.append(dict(cycle_id=row['cycle_id'],asset_id=row['asset_id'],symbol=row['symbol'],
                reason='minimum_lots_exceed_shared_budget',minimum_quote_debit=decimal_text(row['minimum_debit'])))
            continue
        target=max(level,row['minimum_debit']);lots=target//row['lot_cost']
        quantity=lots*row['step'];debit=quantity*row['price']
        allocated.append(dict(cycle_id=row['cycle_id'],asset_id=row['asset_id'],symbol=row['symbol'],
            quantity=decimal_text(quantity),limit_price=decimal_text(row['price']),
            quote_debit=decimal_text(debit),minimum_quote_debit=decimal_text(row['minimum_debit'])))
    used=sum((Fraction(r['quote_debit']) for r in allocated),Fraction(0))
    if used>budget:raise ValueError('native_allocation_budget_invariant')
    return dict(contract='native_concurrent_lot_allocation_v1',quote_currency='USD',
        objective='maximum_participation_then_continuous_equal_notional_floored_to_native_lots',
        requested_count=len(rows),allocated_count=len(allocated),minimum_lot_resource_conflict=conflict,
        funding_available=decimal_text(funding),risk_available=decimal_text(risk),
        usable_budget=decimal_text(budget),allocated_quote_debit=decimal_text(used),
        unallocated_rounding_or_unaffordable_residual=decimal_text(budget-used),
        continuous_notional_level=dict(numerator=level.numerator,denominator=level.denominator),
        allocated=allocated,deferred=deferred,profit_optimality_claimed=False,order_authority=False)
