"""Explicit resource and catalog inputs for the automatically owned PAPER host."""
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import zipfile

from scripts.iqfeed_equity_catalog import read_catalog
from .structural_context_publisher import PublisherBudget
from .structural_tape_prefix import Limits
from .native_tick_enrollment import digest


@dataclass(frozen=True)
class CatalogInput:
    archive_path: str
    archive_sha256: str
    observed_ns: int
    max_archive_bytes: int
    max_source_bytes: int
    max_equities: int


@dataclass(frozen=True)
class PaperContextHostConfig:
    publisher: PublisherBudget
    catalog: CatalogInput
    native_checkpoint_bytes: int
    http_response_bytes: int
    process_rss_limit_bytes: int
    native_refresh_wait_seconds: float
    publisher_idle_wait_seconds: float
    http_timeout_seconds: float
    shutdown_wait_seconds: float
    resource_bindings: tuple[tuple[str, str], ...]
    config_sha256: str

    def receipt(self):
        return asdict(self)


def load_config(path):
    if not path:
        raise ValueError('context_host_resource_config_missing')
    with Path(path).open('rb') as f:
        payload = f.read(64*1024+1)
    if len(payload) > 64*1024:
        raise ValueError('context_host_config_byte_capacity')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('context_host_config_duplicate_key')
            result[key] = value
        return result
    value = json.loads(payload, object_pairs_hook=unique)
    required = {'contract', 'publisher', 'catalog', 'native_checkpoint_bytes', 'http_response_bytes',
        'process_rss_limit_bytes', 'native_refresh_wait_seconds', 'publisher_idle_wait_seconds',
        'http_timeout_seconds', 'shutdown_wait_seconds', 'resource_bindings'}
    if type(value) is not dict or set(value) != required or value['contract'] != 'paper_shared_tick_host_v1':
        raise ValueError('context_host_config_contract_invalid')
    publisher = dict(value['publisher'])
    publisher['limits'] = Limits(**publisher['limits'])
    budget = PublisherBudget(**publisher)
    catalog = CatalogInput(**value['catalog'])
    if (not Path(catalog.archive_path).is_absolute() or not digest(catalog.archive_sha256) or
            any(type(v) is not int or v <= 0 for v in (catalog.observed_ns, catalog.max_archive_bytes,
                catalog.max_source_bytes, catalog.max_equities))):
        raise ValueError('context_host_catalog_input_invalid')
    for key in ('native_checkpoint_bytes', 'http_response_bytes', 'process_rss_limit_bytes'):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError('context_host_resource_budget_invalid')
    for key in ('native_refresh_wait_seconds', 'publisher_idle_wait_seconds', 'http_timeout_seconds', 'shutdown_wait_seconds'):
        if type(value[key]) not in (int, float) or not 0 < value[key] < float('inf'):
            raise ValueError('context_host_transport_budget_invalid')
    bindings = value['resource_bindings']
    if type(bindings) is not dict or not bindings or any(
            type(k) is not str or not k or type(v) is not str or not v for k, v in bindings.items()):
        raise ValueError('context_host_resource_bindings_required')
    return PaperContextHostConfig(budget, catalog, **{k:value[k] for k in required - {
        'contract', 'publisher', 'catalog', 'resource_bindings'}},
        resource_bindings=tuple(sorted(bindings.items())), config_sha256=hashlib.sha256(payload).hexdigest())


def load_provider_catalog(config: CatalogInput, stop_event):
    path = Path(config.archive_path)
    sha = hashlib.sha256()
    with path.open('rb') as f:
        count = 0
        while chunk := f.read(64*1024):
            if stop_event.is_set():
                raise ValueError('context_host_stopped')
            count += len(chunk)
            if count > config.max_archive_bytes:
                raise ValueError('context_host_catalog_archive_capacity')
            sha.update(chunk)
        if sha.hexdigest() != config.archive_sha256:
            raise ValueError('context_host_catalog_archive_changed')
        f.seek(0)
        with zipfile.ZipFile(f) as z:
            entries = z.infolist()
            if len(entries) != 1 or entries[0].is_dir() or entries[0].file_size > config.max_source_bytes:
                raise ValueError('context_host_catalog_archive_shape_or_capacity')
            with z.open(entries[0]) as member:
                class StoppableReader:
                    def readline(self, limit):
                        if stop_event.is_set():
                            raise ValueError('context_host_stopped')
                        return member.readline(limit)
                result = read_catalog(StoppableReader(), observed_ns=config.observed_ns,
                    max_bytes=config.max_source_bytes, max_equities=config.max_equities)
        # Same handle also detects an in-place rewrite during parsing.
        f.seek(0)
        verify, count = hashlib.sha256(), 0
        while chunk := f.read(64*1024):
            if stop_event.is_set():
                raise ValueError('context_host_stopped')
            count += len(chunk)
            if count > config.max_archive_bytes:
                raise ValueError('context_host_catalog_archive_capacity')
            verify.update(chunk)
        if verify.digest() != sha.digest():
            raise ValueError('context_host_catalog_archive_changed')
        return result
