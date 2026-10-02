"""本项目原创。卖方按自己商品核对历史API收货声明；不读取结果正文或执行结算。"""
from .api_quota import canonical,checksum
from .common import MarketError,compact,fields,identifier,integer,parse_json
from .purchase_download import digest


def _view(row,resource,version,provider):
    record=parse_json(row['record']);manifest=parse_json(row['manifest'])
    fields(record,('offer','receipt_id','acknowledged_at'))
    offer=record['offer'];fields(offer,('scope','request_id','usage_id','manifest_sha256','input_sha256'))
    scope=offer['scope'];fields(scope,('principal','key_id','root_id','task_id'))
    for name,value in scope.items():identifier(value,name)
    identifier(offer['request_id'],'request_id');identifier(offer['usage_id'],'usage_id')
    for name in ('manifest_sha256','input_sha256'):digest(offer[name])
    fields(manifest,('format','id','version','acquisition_id','release_digest','task_id','request_id','purpose',
        'provider','operation_id','sha256','bytes','media_type','license_sha256','expires_at'))
    for name in ('release_digest','sha256','license_sha256'):digest(manifest[name])
    identifier(manifest['operation_id'],'operation_id');integer(manifest['bytes'],1,65536,'bytes')
    expected_key='api_client_receipt:'+checksum(canonical([scope,offer['request_id']],4096))
    if (row['key']!=expected_key or record['receipt_id']!='ar_'+checksum(canonical(offer,8192))
            or scope['principal']!=row['buyer'] or scope['principal']!=row['usage_principal']
            or scope['task_id']!=row['task_id'] or offer['usage_id']!=row['usage_id']
            or row['usage_request']!=checksum(compact(['api-call',offer['request_id']]))
            or manifest['id']!=resource or manifest['version']!=version
            or manifest['acquisition_id']!=row['acquisition_id'] or manifest['provider']!=provider
            or manifest['task_id']!=scope['task_id'] or manifest['request_id']!=offer['request_id']
            or manifest['purpose']!=row['purpose'] or manifest['format']!='market-api-result-v1'
            or manifest['media_type']!='application/json'
            or checksum(canonical(manifest,8192))!=offer['manifest_sha256']
            or type(record['acknowledged_at']) not in (int,float) or not 0<=record['acknowledged_at']<10**12):
        raise MarketError('receipt_corrupt','原API收货与交付记录不一致，不能用于核对。')
    # Intentionally exclude buyer, key, task, input, entitlement and order ids.
    return {'receipt_id':record['receipt_id'],'operation_id':manifest['operation_id'],
            'provider':provider,'release_digest':manifest['release_digest'],
            'manifest_sha256':offer['manifest_sha256'],'result_sha256':manifest['sha256'],
            'acknowledged_at':record['acknowledged_at'],'evidence':'authenticated_client_statement'}


def api_receipts(market,id,version,after=0,limit=20):
    market._require('seller')
    integer(after,0,9223372036854775807,'after');integer(limit,1,50,'limit')
    with market.store.connect() as db:
        db.execute('BEGIN')
        submission=market._submission(db,id,version)
        metadata=parse_json(submission['metadata'])
        if metadata.get('kind')!='api' or 'api' not in metadata:
            raise MarketError('not_api','此商品不是API套餐。')
        # Existing immutable meta rows provide a monotonic local scan position;
        # joins bind ownership through actual usage and acquisition records.
        # Malformed receipt JSON fails closed rather than silently losing a row.
        rows=db.execute('''SELECT r.rowid AS position,r.key,r.value AS record,m.value AS manifest,
                u.id AS usage_id,u.principal AS usage_principal,u.task_id,u.request_id AS usage_request,
                a.id AS acquisition_id,a.principal AS buyer,a.purpose
            FROM meta r
            JOIN usage_events u ON u.id=CASE WHEN r.key GLOB 'api_client_receipt:*'
                THEN json_extract(r.value,'$.offer.usage_id') ELSE NULL END
            JOIN acquisitions a ON a.id=u.acquisition_id
            LEFT JOIN meta m ON m.key='api_usage:'||u.id
            WHERE r.key GLOB 'api_client_receipt:*' AND r.rowid>?
                AND a.resource_id=? AND a.version=?
            ORDER BY r.rowid LIMIT ?''',(after,id,version,limit+1)).fetchall()
        page=rows[:limit];items=[]
        for row in page:
            if row['manifest'] is None:raise MarketError('receipt_corrupt','缺少原API交付清单。')
            items.append(_view(row,id,version,metadata['api']['provider']))
    return {'status':'ok','id':id,'version':version,'items':items,
            'next_after':page[-1]['position'] if page else after,'has_more':len(rows)>limit,
            'scope':'historical_client_statements','package_fulfillment_confirmed':False,
            'channel_fulfillment_confirmed':False,'payment_executed':False}
