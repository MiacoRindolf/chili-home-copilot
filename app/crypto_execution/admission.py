"""Concrete locked native admission: full equity ledger + fresh PAPER census."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .census import read_account_census
from .equity_claims import locked_equity_snapshot,number,require_equity_account_locks,verify_equity_census_bounds
from .funding import crypto_account_truth
from .lifecycle import decimal_text
from .truth import identity,native_payload


class LockedNativeAdmissionReader:
    """Funding/ownership evidence is durable before the caller can commit a claim.

    Raw HTTP is fsynced to its own observation file, not appended through another
    DB connection that would self-deadlock on the account lock. Its hash/path and
    the exact local arithmetic are retained in the native cycle transaction.
    The explicit risk fraction is an account policy input, not a signal/window.
    This reader does not discover candidates, derive entries or grant HTTP order
    authority. The supplied PAPER client rechecks its actual host authority.
    """
    def __init__(self,broker,*,account_risk_fraction,journal_root,max_journal_bytes,max_order_pages):
        self.broker=broker;self.account_id=identity(broker.account_id)
        self.risk_fraction=number(account_risk_fraction)
        if self.risk_fraction>1:raise ValueError('native_admission_risk_fraction_invalid')
        self.root=Path(journal_root)
        if not self.root.is_absolute() or not self.root.is_dir():
            raise ValueError('native_admission_existing_absolute_journal_directory_required')
        if (type(max_journal_bytes) is not int or max_journal_bytes<=0 or
                type(max_order_pages) is not int or max_order_pages<=0):
            raise ValueError('native_admission_resource_bounds_required')
        self.max_bytes=max_journal_bytes;self.max_pages=max_order_pages

    def __call__(self,c):
        from app.services.trading.momentum_neural.alpaca_orphan_claims import _certify_alpaca_owned_entry_posture
        require_equity_account_locks(c)
        observation_id=str(uuid4());path=self.root/(observation_id+'.jsonl')
        digest=hashlib.sha256();size=0
        with path.open('xb') as journal:
            def record(value):
                nonlocal size
                raw=(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
                if size+len(raw)>self.max_bytes:raise ValueError('native_admission_journal_resource_exhausted')
                journal.write(raw);journal.flush();os.fsync(journal.fileno())
                digest.update(raw);size+=len(raw)
            record(dict(kind='admission_started',observation_id=observation_id,account_id=self.account_id))
            equity=locked_equity_snapshot(c,account_id=self.account_id)
            record(dict(kind='equity_ledger',receipt=equity['receipt']))
            if equity['receipt']['unavailable_reasons']:
                raise ValueError('native_admission_equity_settlement_or_quarantine_unresolved')
            census=read_account_census(self.broker,record=record,
                before_transport=lambda:require_equity_account_locks(c),max_order_pages=self.max_pages)
            account=crypto_account_truth(census['account'],expected_account_id=self.account_id)
            positions=[];orders=[]
            for row in census['positions']:
                p=dict(row,product_id=row.get('symbol'))
                if row.get('asset_class')=='crypto':p['native_crypto_position']=native_payload(row,position=True)
                positions.append(p)
            for row in census['orders']:
                raw=dict(row)
                if row.get('asset_class')=='crypto':raw['native_crypto_order']=native_payload(row)
                orders.append(dict(row,raw=raw))
            ownership=_certify_alpaca_owned_entry_posture(c,broker_positions=positions,broker_orders=orders,
                account_scope='alpaca:paper',alpaca_account_id=self.account_id)
            record(dict(kind='broker_ownership',receipt=ownership))
            if ownership.get('ok') is not True:raise ValueError('native_admission_broker_ownership_unverified')
            exact=verify_equity_census_bounds(equity,positions=census['positions'],orders=census['orders'])
            # This is the current broker equity; no local stale equity fallback.
            budget=number(census['account'].get('equity'))*self.risk_fraction
            evidence=dict(observation_id=observation_id,account_id=self.account_id,
                account_risk_fraction=decimal_text(self.risk_fraction),account_risk_budget=decimal_text(budget),
                equity_ledger=equity['receipt'],broker_ownership=ownership,exact_equity_inventory=exact,
                order_pages=census['order_pages'],order_pagination_exhausted=True,broker_snapshot_atomic=False)
            record(dict(kind='admission_evidence_complete',receipt=evidence))
        evidence['journal']=dict(path=str(path),sha256=digest.hexdigest(),bytes=size)
        return dict(account=account,observation_id=observation_id,external_claims=equity['external_claims'],
            account_risk_budget=decimal_text(budget),external_risk_upper_bound=equity['external_risk_upper_bound'],
            evidence_receipt=evidence)
