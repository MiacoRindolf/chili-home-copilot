"""Explicit PAPER host resources and evidence; no strategy window defaults."""
import hashlib
import json
import math
from pathlib import Path
import re


def read_json(path, limit):
    p=Path(path)
    if not p.is_absolute():raise ValueError('native_host_absolute_path_required')
    with p.open('rb') as f:raw=f.read(limit+1)
    if len(raw)>limit:raise ValueError('native_host_config_capacity')
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def load_config(path):
    # File-parser capacity only; it is not a market-data or strategy window.
    c,sha=read_json(path,1048576)
    required={'contract','directory','receipt_log','receipt_directory','supervisor_path','env_path',
        'fee_evidence_path','fee_evidence_sha256','location','source_resources',
        'poll_seconds','reconcile_seconds','broker_min_interval_seconds','http_timeout_seconds','max_http_bytes',
        'max_receipt_bytes','max_log_bytes','max_event_bytes','lock_timeout_ms',
        'max_admission_journal_bytes','max_order_pages','max_host_journal_bytes'}
    if type(c) is not dict or set(c)!=required or c['contract']!='native_paper_host_v1':
        raise ValueError('native_host_config_contract_invalid')
    for k in ('directory','receipt_log','receipt_directory','supervisor_path','env_path','fee_evidence_path'):
        if type(c[k]) is not str or not Path(c[k]).is_absolute():
            raise ValueError('native_host_absolute_path_required')
    for k in ('poll_seconds','reconcile_seconds','broker_min_interval_seconds','http_timeout_seconds'):
        if type(c[k]) not in (int,float) or not math.isfinite(c[k]) or c[k]<=0:
            raise ValueError('native_host_transport_budget_invalid')
    for k in ('max_http_bytes','max_receipt_bytes','max_log_bytes','max_event_bytes','lock_timeout_ms',
              'max_admission_journal_bytes','max_order_pages','max_host_journal_bytes'):
        if type(c[k]) is not int or c[k]<=0:raise ValueError('native_host_capacity_invalid')
    if c['location'] not in ('us','us-1','eu-1') or not re.fullmatch('[0-9a-f]{64}',c['fee_evidence_sha256']):
        raise ValueError('native_host_source_or_fee_binding_invalid')
    return c,sha


def accepted_receipt(c):
    path=Path(c['receipt_log'])
    # Read bounded tail; reject an incomplete final line, never accept a guessed receipt.
    with path.open('rb') as f:
        f.seek(0,2);size=f.tell();start=max(0,size-c['max_log_bytes']);f.seek(start);raw=f.read()
    if start:raw=raw.partition(b'\n')[2]
    if not raw.endswith(b'\n'):raise ValueError('native_host_receipt_log_incomplete')
    matches=re.findall(r'ACCEPTED receipt: (.+?) sha=([0-9a-f]{64}) clean=True',raw.decode('utf-8'))
    if not matches:raise ValueError('native_host_accepted_receipt_missing')
    name,sha=matches[-1];p=Path(name).resolve(strict=True)
    if p.parent!=Path(c['receipt_directory']).resolve(strict=True):
        raise ValueError('native_host_receipt_directory_changed')
    return p,sha
