"""Dedicated, cancel-aware PAPER GET transport for native observation workers."""
import json
import time

import requests

from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .broker_asset_inventory import InventoryProbe, build_inventory
from .broker_coverage_inventory import (
    OPEN_ORDER_RESPONSE_LIMIT, REASONS, bracket_pending_reads, coverage_probe, coverage_read,
)

PAPER_BASE = 'https://paper-api.alpaca.markets/v2/'
PATHS = frozenset({'account', 'assets', 'positions', 'orders'})


class PaperNativeInventoryReader:
    """No order methods, SDK fallback, redirect following or unbounded response.

    The owning worker closes this session after its last in-flight request. A
    stop request prevents each subsequent GET and checkpoint publication.
    """
    def __init__(self, *, expected_account_id, api_key, api_secret, paper,
                 timeout_seconds, max_response_bytes, stop_event, session=None, clock_ns=time.time_ns):
        self.account_sha256 = alpaca_paper_account_identity_sha256(expected_account_id)
        if paper is not True or not api_key or not api_secret:
            raise ValueError('paper_native_reader_configuration_required')
        if (type(timeout_seconds) not in (float, int) or not 0 < timeout_seconds < float('inf')
                or type(max_response_bytes) is not int or max_response_bytes <= 0):
            raise ValueError('paper_native_reader_resource_limits_required')
        self._expected, self._stop, self._clock = expected_account_id, stop_event, clock_ns
        self._timeout, self._max_bytes = timeout_seconds, max_response_bytes
        self._session = session or requests.Session()
        self._headers = {'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': api_secret}

    def close(self):
        self._session.close()

    def _get(self, path, **params):
        if path not in PATHS:
            raise ValueError('paper_native_reader_path_forbidden')
        if self._stop.is_set():
            raise ValueError('paper_native_reader_stopped')
        with self._session.get(PAPER_BASE+path, params=params, headers=self._headers,
                timeout=self._timeout, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('paper_native_reader_http_unavailable')
            chunks, count = [], 0
            for chunk in response.iter_content(chunk_size=64*1024):
                if self._stop.is_set():
                    raise ValueError('paper_native_reader_stopped')
                count += len(chunk)
                if count > self._max_bytes:
                    raise ValueError('paper_native_reader_response_capacity')
                chunks.append(chunk)
            return json.loads(b''.join(chunks))

    def _account(self):
        account = self._get('account')
        if type(account) is not dict or account.get('id') != self._expected:
            raise ValueError('paper_native_reader_account_mismatch')
        return account['id']

    def get_asset_inventory_probe(self):
        started = self._clock()
        try:
            before = self._account()
            catalogs = {}
            for asset_class in ('us_equity', 'crypto'):
                begin = self._clock()
                rows = self._get('assets', status='active', asset_class=asset_class)
                catalogs[asset_class] = (begin, self._clock(), rows)
            after = self._account()
            return InventoryProbe(build_inventory(expected_account_id=self._expected,
                account_before=before, account_after=after, started_ns=started, completed_ns=self._clock(),
                catalog_responses=catalogs), None)
        except Exception as exc:
            return InventoryProbe(None, 'paper_native_inventory_unavailable:'+type(exc).__name__)

    def get_coverage_inventory_probe(self):
        started = self._clock()
        try:
            self._account()
            def observe(reason):
                begin, response, error = self._clock(), None, None
                try:
                    response = (self._get('positions') if reason == 'held' else self._get('orders',
                        status='open', limit=OPEN_ORDER_RESPONSE_LIMIT, direction='asc', nested='false'))
                except Exception as exc:
                    error = 'paper_native_coverage_unavailable:'+type(exc).__name__
                return coverage_read(reason, started_ns=begin, completed_ns=self._clock(),
                    response=response, error=error, expected_account_id=self._expected)
            before = observe('pending')
            held = observe('held')
            after = observe('pending')
            self._account()
            reads = (held, bracket_pending_reads(before, after))
        except Exception as exc:
            reads = tuple(coverage_read(reason, started_ns=started, completed_ns=self._clock(),
                error='paper_native_account_unavailable:'+type(exc).__name__,
                expected_account_id=self._expected) for reason in REASONS)
        return coverage_probe(expected_account_id=self._expected, started_ns=started,
                              completed_ns=self._clock(), reads=reads)
