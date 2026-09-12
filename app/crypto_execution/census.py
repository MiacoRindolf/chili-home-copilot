"""Complete native broker account reads, without timestamps as pagination cursors.

Order ID cursors are documented by Alpaca GET /v2/orders. A short nonempty
page is not a completion receipt: keep reading until an empty page. This is
broker inventory evidence, not an atomic market snapshot or trading authority.
https://docs.alpaca.markets/us/reference/getallorders-1
"""
from .truth import identity

ORDER_PAGE='/v2/orders?status=open&limit=500&direction=desc&nested=false'


def read_account_census(broker,*,record,before_transport,max_order_pages):
    """Read all positions/orders, then current account funding on the same client.

    The caller retains raw transport evidence through record and owns the shared
    account lock when using this in an admission. No filtering by symbols, rank,
    side or asset class. Explicit page/byte bounds are operational resources;
    exceeding them returns no partial census as if it were complete.
    """
    if type(max_order_pages) is not int or max_order_pages<=0:
        raise ValueError('native_census_page_resource_bound_required')
    def read(path):
        response=broker.request('GET',path,None,record=record,before_transport=before_transport)
        if response.status!=200:raise ValueError('native_census_broker_read_unavailable')
        return response.json()
    positions=read('/v2/positions')
    if type(positions) is not list:raise ValueError('native_census_position_shape_invalid')
    assets=set()
    for row in positions:
        if type(row) is not dict:raise ValueError('native_census_position_shape_invalid')
        aid=identity(row.get('asset_id'))
        if aid in assets:raise ValueError('native_census_duplicate_position')
        assets.add(aid)
    orders=[];seen=set();cursor=None;complete=False
    for page_index in range(max_order_pages):
        path=ORDER_PAGE if cursor is None else ORDER_PAGE+'&before_order_id='+cursor
        page=read(path)
        if type(page) is not list or len(page)>500:raise ValueError('native_census_order_page_invalid')
        if not page:
            complete=True;break
        for row in page:
            if type(row) is not dict or row.get('legs'):
                raise ValueError('native_census_flat_order_shape_required')
            oid=identity(row.get('id'))
            if oid in seen:raise ValueError('native_census_order_cursor_did_not_advance')
            seen.add(oid);orders.append(row)
        cursor=identity(page[-1]['id'])
    if not complete:raise ValueError('native_census_order_page_resource_exhausted')
    account=read('/v2/account')
    if type(account) is not dict or identity(account.get('id'))!=identity(broker.account_id):
        raise ValueError('native_census_account_identity_changed')
    return dict(account=account,positions=positions,orders=orders,order_pages=page_index+1,
                order_pagination_exhausted=True,broker_snapshot_atomic=False)
