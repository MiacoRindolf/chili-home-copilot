"""Exact native funding evidence and currency-preserving reservation arithmetic.

These pure records are not a broker reflection proof or an atomic account lock.
The execution owner must supply its full ledger and certify any reflected amount.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from .truth import identity, value


def amount(raw, *, optional=False):
    if raw is None and optional:
        return None
    if type(raw) not in (str, Decimal):
        raise ValueError('crypto_funding_exact_decimal_required')
    try:
        parsed=Decimal(raw)
    except InvalidOperation:
        raise ValueError('crypto_funding_decimal_invalid') from None
    if not parsed.is_finite():
        raise ValueError('crypto_funding_decimal_invalid')
    return parsed


@dataclass(frozen=True)
class CryptoAccountTruth:
    account_id: str
    currency: str | None
    account_status: str | None
    crypto_status: str | None
    non_marginable_buying_power: Decimal | None
    cash: Decimal | None
    accrued_fees: Decimal | None
    pending_transfer_out: Decimal | None
    account_blocked: bool | None
    trading_blocked: bool | None
    trade_suspended_by_user: bool | None

    @property
    def entry_unavailable_reasons(self):
        reasons=[]
        if self.account_status!='ACTIVE':reasons.append('account_not_active')
        if self.crypto_status!='ACTIVE':reasons.append('crypto_not_active')
        if self.currency!='USD':reasons.append('native_account_currency_unverified')
        if self.non_marginable_buying_power is None:reasons.append('non_marginable_buying_power_unknown')
        for key in ('account_blocked','trading_blocked','trade_suspended_by_user'):
            if getattr(self,key) is not False:reasons.append(key+'_or_unknown')
        return tuple(reasons)


def crypto_account_truth(row, *, expected_account_id):
    account_id=identity(value(row,'id'))
    if account_id!=identity(expected_account_id):raise ValueError('crypto_funding_account_mismatch')
    def flag(key):
        raw=value(row,key)
        return raw if type(raw) is bool else None
    def text(key):
        raw=value(row,key)
        return raw if type(raw) is str and raw else None
    return CryptoAccountTruth(account_id,text('currency'),text('status'),text('crypto_status'),
        amount(value(row,'non_marginable_buying_power'),optional=True),amount(value(row,'cash'),optional=True),
        amount(value(row,'accrued_fees'),optional=True),amount(value(row,'pending_transfer_out'),optional=True),
        flag('account_blocked'),flag('trading_blocked'),flag('trade_suspended_by_user'))


@dataclass(frozen=True)
class FundingClaim:
    """One local debit upper bound and a certified reflected sub-amount.

    `reflected` is NOT inferred from order existence/status or a later timestamp.
    It must refer to this exact account observation. If not proven, pass zero;
    the receipt names the resulting overlap uncertainty rather than claiming
    that every broker-visible pending order was charged a second time.
    """
    claim_id: str
    account_id: str
    quote_currency: str
    debit_upper_bound: Decimal
    reflected_in_observation: Decimal
    reflection_observation_id: str | None = None


def funding_residual(account, *, quote_currency, claims, observation_id=None):
    """Bound residual native fiat funding without borrowing equity margin.

    This account endpoint certifies USD buying power only. Non-USD pairs require
    actual quote-asset inventory and their own funding evidence, never a stablecoin
    peg or a conversion of buying power by a sampled price. Pure arithmetic only:
    a caller must hold the account lock and bind each claim/reflection to the read.
    """
    if type(account) is not CryptoAccountTruth or type(quote_currency) is not str:
        raise ValueError('crypto_funding_inputs_invalid')
    if observation_id is not None:observation_id=identity(observation_id)
    reasons=list(account.entry_unavailable_reasons)
    if quote_currency!=account.currency:reasons.append('quote_asset_funding_not_in_account_endpoint')
    seen=set(); debit=Fraction(0); reflected=Fraction(0)
    for claim in claims:
        if (type(claim) is not FundingClaim or type(claim.claim_id) is not str or not claim.claim_id
                or claim.claim_id in seen or claim.quote_currency!=quote_currency
                or identity(claim.account_id)!=account.account_id):
            raise ValueError('crypto_funding_claim_identity_invalid')
        total=amount(claim.debit_upper_bound); included=amount(claim.reflected_in_observation)
        if not 0<=included<=total:raise ValueError('crypto_funding_claim_amount_invalid')
        if included>0 and (observation_id is None or claim.reflection_observation_id is None
                or identity(claim.reflection_observation_id)!=observation_id):
            raise ValueError('crypto_funding_reflection_observation_mismatch')
        seen.add(claim.claim_id); debit+=Fraction(total); reflected+=Fraction(included)
    observed=account.non_marginable_buying_power
    residual=None if reasons else max(Fraction(0),Fraction(observed)-(debit-reflected))
    return dict(quote_currency=quote_currency,observation_id=observation_id,unavailable_reasons=tuple(reasons),
        observed_non_marginable_buying_power=observed,local_debit_upper_bound=debit,
        certified_reflected_amount=reflected,unreflected_debit_upper_bound=debit-reflected,
        remaining_funding_bound=residual,claim_ids=tuple(sorted(seen)),
        reflection_proven_by_this_function=False,atomic_reservation_performed=False)
