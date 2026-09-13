"""Read a previously verified V1 prefix through an explicit immutable seal.

The external seal must come from verified retained-source ownership evidence.
It is not derived or authenticated by this loader. Every sealed chain byte is
verified, but only the latest full-anchor publication is mathematically rebuilt.
Old historical decisions are not replayed or newly certified here. Unsealed
appends remain outside the transition; a new host observation is still required.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from scripts.crypto_history_pages import HistoryRequest,HistoryWalk,canonical,sha
from .history_source import QuoteHistoryWalk,complete_walks
from .source_capture import NativeSourceCapture,implementation_identity,publication


@dataclass(frozen=True)
class SealedSeed:
    batch: object
    metadata: dict
    publication_record: dict
    origin: dict


def load_sealed_seed(directory,seal):
    directory=Path(directory)
    if (not directory.is_absolute() or type(seal) is not dict or set(seal)!=
            {'metadata_sha256','record_root_sha256','record_count','journal_bytes','revision'} or
            any(type(seal[k]) is not int or seal[k]<=0 for k in ('record_count','journal_bytes','revision')) or
            any(type(seal[k]) is not str or len(seal[k])!=64 or any(c not in '0123456789abcdef' for c in seal[k])
                for k in ('metadata_sha256','record_root_sha256'))):
        raise ValueError('native_legacy_explicit_seal_required')
    with (directory/'metadata.json').open('rb') as f:raw=f.read(1048577)
    if len(raw)>1048576 or sha(raw)!=seal['metadata_sha256']:
        raise ValueError('native_legacy_metadata_seal_changed')
    metadata=json.loads(raw)
    if (metadata.get('contract')!='native_rest_source_capture_v1' or
            metadata.get('observation_mode')!='revisable_prefix' or
            metadata.get('implementation_sha256')!=implementation_identity()):
        raise ValueError('native_legacy_full_seed_or_implementation_required')
    resources=metadata['resources']
    required={'max_pages','max_trades','max_quotes','max_page_bytes','max_normalized_bytes',
        'retained_ticks','frontier_ticks','active_references','max_pending_quotes','max_journal_bytes'}
    if set(resources)!=required or any(type(n) is not int or n<=0 for n in resources.values()):
        raise ValueError('native_legacy_resource_bounds_required')
    if seal['journal_bytes']>resources['max_journal_bytes']:
        raise ValueError('native_legacy_seal_capacity')
    path=directory/'source.jsonl';bound=resources['max_page_bytes']*2+1048576
    root=sha(canonical(metadata));sequence=0;revision=0;active=None;latest=None;raw_complete=None
    symbols=tuple(sorted(a['symbol'] for a in metadata['assets']))
    with path.open('rb') as f:
        if f.seek(0,2)<seal['journal_bytes']:raise ValueError('native_legacy_sealed_bytes_missing')
        f.seek(0)
        while f.tell()<seal['journal_bytes']:
            offset=f.tell();raw=f.readline(min(bound,seal['journal_bytes']-offset)+1)
            if not raw.endswith(b'\n') or len(raw)>bound or f.tell()>seal['journal_bytes']:
                raise ValueError('native_legacy_sealed_record_torn')
            envelope=json.loads(raw);actual=envelope.pop('root')
            if actual!=sha(canonical(envelope)) or envelope['sequence']!=sequence+1 or envelope['previous']!=root:
                raise ValueError('native_legacy_sealed_chain_changed')
            root=actual;sequence+=1;b=envelope['body'];kind=b['kind']
            if kind=='observation_started':
                fields=dict(b['request']);fields['symbols']=tuple(fields['symbols']);request=HistoryRequest(**fields)
                if (request.location,request.symbols,request.start_ns,request.page_limit,request.inventory_sha256)!=(
                        metadata['location'],symbols,metadata['anchor_ns'],metadata['page_limit'],metadata['inventory_sha256']):
                    raise ValueError('native_legacy_sealed_request_changed')
                active=dict(id=b['observation'],start=offset,request=request);raw_complete=None
            elif active is None or b.get('observation')!=active['id']:
                raise ValueError('native_legacy_sealed_observation_changed')
            elif kind=='raw_batch_complete':raw_complete=b['evidence_sha256']
            elif kind=='context_published':
                if (raw_complete is None or b['evidence_sha256']!=raw_complete or
                        b['revision']!=revision+1 or b['end_ns']!=active['request'].end_ns):
                    raise ValueError('native_legacy_sealed_publication_changed')
                revision=b['revision'];latest=(active['start'],f.tell(),b);active=None
            elif kind=='observation_failed':active=None
            elif kind!='transport':raise ValueError('native_legacy_sealed_record_kind')
        if root!=seal['record_root_sha256'] or sequence!=seal['record_count'] or revision!=seal['revision']:
            raise ValueError('native_legacy_external_seal_mismatch')
        if latest is None:raise ValueError('native_legacy_committed_seed_missing')
        start,end,published=latest;f.seek(start)
        first=json.loads(f.readline(bound))['body'];fields=dict(first['request']);fields['symbols']=tuple(fields['symbols'])
        request=HistoryRequest(**fields)
        trades=HistoryWalk(request,max_pages=resources['max_pages'],max_trades=resources['max_trades'],max_page_bytes=resources['max_page_bytes'])
        quotes=QuoteHistoryWalk(request,max_pages=resources['max_pages'],max_quotes=resources['max_quotes'],max_page_bytes=resources['max_page_bytes'])
        while f.tell()<end:
            b=json.loads(f.readline(bound))['body']
            if b['kind']!='transport':continue
            v=b['value'];channel=v['path'].rsplit('/',1)[-1]
            if v.get('kind')!='http_response' or channel not in ('trades','quotes') or v['path']!='/v1beta3/crypto/'+request.location+'/'+channel:
                raise ValueError('native_legacy_seed_transport_changed')
            walk=trades if channel=='trades' else quotes
            body=bytes.fromhex(v['body_hex'])
            if (v['parameters']!=walk.parameters() or v['status']!=200 or not v['complete'] or
                    len(body)>resources['max_page_bytes'] or sha(body)!=v['body_sha256']):
                raise ValueError('native_legacy_seed_response_changed')
            walk.accept(body,received_ns=v['received_ns'])
    batch=complete_walks(request,trades,quotes)
    if batch.evidence_sha256!=published['evidence_sha256']:
        raise ValueError('native_legacy_seed_evidence_changed')
    source=object.__new__(NativeSourceCapture);source.metadata=metadata;source.resources=resources
    book=source._new_book();book.append_rest_batch(batch,published_ns=published['raw_published_ns'])
    if publication(book)!=published['view']:raise ValueError('native_legacy_seed_math_diverged')
    return SealedSeed(batch,metadata,published,dict(seal))
