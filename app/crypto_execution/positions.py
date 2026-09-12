"""Resolve broker position UUID aliases through the broker asset endpoint.

An observed portfolio UUID may differ from the native order/listing UUID. A
matching display symbol alone never proves ownership. Retain the raw transport
and the broker's explicit UUID resolution before interpreting canonical balance.
"""
from .truth import identity,asset_identity,crypto_position_truth


def resolved_position(raw,asset,resolution):
    aid,symbol=asset_identity(asset)
    legacy=identity(raw.get('asset_id'))
    if (raw.get('asset_class')!='crypto' or asset_identity(resolution)!=(aid,symbol) or
            raw.get('symbol') not in (symbol,symbol.replace('/',''))):
        raise ValueError('native_position_alias_resolution_mismatch')
    return dict(raw,asset_id=aid,symbol=symbol,native_position_alias=dict(
        broker_asset_id=legacy,broker_symbol=raw['symbol'],resolved_asset=resolution,
        basis='broker_asset_uuid_lookup'))


def read_owned_position(state,read,record):
    """Return a canonical position or None after the full portfolio resolves.

    read(path) uses the owner's existing PAPER authority/fence/raw recorder.
    A failed/ambiguous lookup raises; it never declares a filled leg flat.
    """
    asset=state['asset'];aid,symbol=asset_identity(asset)
    retained=(state.get('position') or {}).get('native_position_alias')
    preferred=identity(retained['broker_asset_id']) if retained else aid
    response=read('/v2/positions/'+preferred)
    if response.status not in (200,404):return response.status,None
    direct=response.status==200
    if direct:
        rows=[response.json()]
    else:
        response=read('/v2/positions')
        if response.status!=200:return response.status,None
        rows=response.json()
        if type(rows) is not list:raise ValueError('native_position_portfolio_shape')
    matches=[]
    for row in rows:
        if type(row) is not dict:raise ValueError('native_position_portfolio_row_shape')
        if row.get('asset_class')!='crypto':
            if direct:raise ValueError('native_position_direct_identity_mismatch')
            continue
        observed=identity(row.get('asset_id'))
        if observed==aid:
            crypto_position_truth(row,asset=asset)
            canonical=row
        else:
            result=read('/v2/assets/'+observed)
            if result.status!=200:raise ValueError('native_position_alias_resolution_unavailable')
            resolution=result.json()
            if asset_identity(resolution)!=(aid,symbol):
                if direct:raise ValueError('native_position_direct_identity_mismatch')
                continue
            canonical=resolved_position(row,asset,resolution)
        if 'native_position_alias' in canonical:
            record(dict(phase='position_alias_resolved',raw_position=row,
                resolution=canonical['native_position_alias'],canonical_position=canonical))
        matches.append(canonical)
    if len(matches)>1:raise ValueError('native_position_alias_ambiguous_balance')
    return 200,(matches[0] if matches else None)
