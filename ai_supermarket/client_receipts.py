"""来源：本项目原创。已认证客户端对原文件包的收货声明；不证明任务完成或支付渠道确认。"""
import hashlib
import io
import re
import zipfile

from .common import MarketError, fields, identifier, parse_json
from .payment_orders import _digest, _get, _hash, _put


PREFIX='client_delivery:'


def _key(scope,request_id):
    identifier(request_id,'request_id')
    return PREFIX+_hash([scope,request_id])


def _validate(market,db,key,record,*,historical=False):
    fields(record,('scope','request_id','offer','offer_sha256','acknowledgement'))
    scope,offer=record['scope'],record['offer']
    fields(scope,('principal','root_id','task_id','key_id'))
    if scope['principal']!=market.principal or key!=_key(scope,record['request_id']):
        raise MarketError('receipt_scope_mismatch','收货记录不属于当前买方或原请求。')
    fields(offer,('id','version','acquisition_id','usage_id','delivery_request_id','release_digest',
                  'bundle_sha256','manifest_sha256','resource_sha256','license_sha256','order_id'))
    if record['offer_sha256']!=_hash(offer):
        raise MarketError('receipt_corrupt','原交付包记录已变化。')
    for name in ('release_digest','bundle_sha256','manifest_sha256','resource_sha256','license_sha256'):
        _digest(offer[name],name)
    if historical:
        # Channel bookkeeping refers to an already completed delivery. A later
        # refund blocks new access, but cannot erase that delivery's receipt.
        from .exchange import Exchange
        from .payment_fulfillment import check_paid_history
        acquired=db.execute('SELECT * FROM acquisitions WHERE id=?',(offer['acquisition_id'],)).fetchone()
        if not acquired or acquired['principal']!=market.principal:
            raise MarketError('receipt_scope_mismatch','原权益不属于该历史买方。')
        audit=db.execute('SELECT * FROM exchange_audit WHERE id=?',('e_'+acquired['id'],)).fetchone()
        if (not acquired['id'].startswith('ap_') or not audit or audit['actor']!=market.principal
                or audit['action']!='acquire' or audit['resource_id']!=acquired['resource_id']
                or audit['version']!=acquired['version']):
            raise MarketError('receipt_binding_mismatch','原权益发行记录缺失。')
        detail=parse_json(audit['detail'])
        release_digest=Exchange._release_digest(db,acquired['resource_id'],acquired['version'])
        if (not isinstance(detail,dict) or detail.get('acquisition_id')!=acquired['id']
                or detail.get('purpose')!=acquired['purpose'] or detail.get('release_digest')!=release_digest):
            raise MarketError('receipt_binding_mismatch','原权益发行记录不符。')
        check_paid_history(db,acquired)
    else:
        acquired=market._owned(db,offer['acquisition_id'])
        release_digest=market._check_release_binding(db,acquired)
    usage=db.execute('SELECT * FROM usage_events WHERE id=?',(offer['usage_id'],)).fetchone()
    if (not usage or [acquired['resource_id'],acquired['version']]!=[offer['id'],offer['version']]
            or usage['acquisition_id']!=offer['acquisition_id'] or usage['principal']!=scope['principal']
            or usage['task_id']!=scope['task_id'] or usage['request_id']!=offer['delivery_request_id']
            or release_digest!=offer['release_digest']):
        raise MarketError('receipt_binding_mismatch','收货记录与原权益、任务或使用事件不符。')
    if offer['order_id'] is not None:
        link=_get(db,'paid_entitlement:'+offer['acquisition_id'])
        if not isinstance(link,dict) or link.get('order_id')!=offer['order_id']:
            raise MarketError('receipt_binding_mismatch','收货记录没有绑定原付费权益。')
    ack=record['acknowledgement']
    if ack is not None:
        fields(ack,('receipt_id','offer_sha256','bundle_sha256','acknowledged_at','evidence'))
        if (ack['receipt_id']!='cr_'+_hash([key,record['offer_sha256']])
                or ack['offer_sha256']!=record['offer_sha256'] or ack['bundle_sha256']!=offer['bundle_sha256']
                or ack['evidence']!='authenticated_client_statement'
                or type(ack['acknowledged_at']) not in (int,float) or not 0<=ack['acknowledged_at']<10**12):
            raise MarketError('receipt_corrupt','客户端收货声明结构或原包绑定无效。')
    return record


def _view(record):
    ack=record['acknowledgement'];offer=record['offer']
    return {'status':'client_receipt_recorded' if ack else 'awaiting_client_receipt',
        'request_id':record['request_id'],'bundle_sha256':offer['bundle_sha256'],
        'acquisition_id':offer['acquisition_id'],'usage_id':offer['usage_id'],
        'receipt_id':ack['receipt_id'] if ack else None,'acknowledged_at':ack['acknowledged_at'] if ack else None,
        'evidence':'authenticated_client_statement' if ack else 'bundle_prepared_only',
        'server_acknowledged':ack is not None,'channel_fulfillment_confirmed':False,'task_completed':False}


class ClientReceipts:
    def __init__(self,host,key_id):
        if not isinstance(key_id,str) or not re.fullmatch('ak_[0-9a-f]{32}',key_id):
            raise MarketError('invalid_params','收货记录需要已认证凭据编号。')
        self.host=host;self.market=host.market
        self.scope={'principal':host.market.principal,'root_id':host.budget.root_id,
                    'task_id':host.task_id,'key_id':key_id}

    def prepare(self,value,bundle):
        # Input is the just-built server bundle, never an uploaded archive.
        from .purchase_download import MAX_ZIP, validate_download
        if not isinstance(bundle,bytes) or len(bundle)>MAX_ZIP:
            raise MarketError('invalid_download','待发送文件包无效。')
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            manifest_raw=archive.read('manifest.json')
        manifest=parse_json(manifest_raw)
        expected={**value,'task_id':self.host.task_id,'purpose':self.host.purpose,
                  'release_digest':manifest['release_digest']}
        validate_download(bundle,hashlib.sha256(bundle).hexdigest(),expected)
        key=_key(self.scope,value['request_id'])
        with self.market.store.connect(write=True) as db:
            acquired=self.market._owned(db,manifest['acquisition_id'])
            usage=db.execute('SELECT * FROM usage_events WHERE id=?',(manifest['usage_id'],)).fetchone()
            if not usage:raise MarketError('delivery_required','原文件没有宿主交付事件。')
            link=_get(db,'paid_entitlement:'+acquired['id']) if acquired['id'].startswith('ap_pay_') else None
            order_id=link['order_id'] if link else None
            offer={k:manifest[k] for k in ('id','version','acquisition_id','usage_id','release_digest')}
            offer.update(delivery_request_id=usage['request_id'],order_id=order_id,
                         bundle_sha256=hashlib.sha256(bundle).hexdigest(),manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
                         resource_sha256=manifest['resource']['sha256'],license_sha256=manifest['license']['sha256'])
            record={'scope':self.scope,'request_id':value['request_id'],'offer':offer,
                    'offer_sha256':_hash(offer),'acknowledgement':None}
            _validate(self.market,db,key,record)
            old=_get(db,key)
            if old is not None:
                _validate(self.market,db,key,old)
                if old['offer']!=offer:raise MarketError('idempotency_conflict','原收货请求的文件包发生变化。')
                record=old
            else:_put(db,key,record)
            if order_id is not None:
                delivery=_get(db,'payment_delivery:'+order_id)
                # Only the original paid delivery gates that order's channel receipt.
                # A later reuse in another task cannot retroactively replace it.
                if delivery and delivery.get('usage_id')==offer['usage_id']:
                    required={'acquisition_id':offer['acquisition_id'],'usage_id':offer['usage_id'],
                              'delivery_sha256':_hash(delivery)}
                    marker='remote_file_delivery:'+order_id
                    previous=_get(db,marker)
                    if previous is not None and previous!=required:
                        raise MarketError('receipt_binding_mismatch','原订单远程交付要求发生变化。')
                    channel=_get(db,'channel_fulfillment:'+order_id)
                    # A later HTTP download must not rewrite the evidence requirements
                    # of a channel action that already started under the local-delivery flow.
                    if previous is not None or channel is None or (channel.get('state')=='pending' and not channel.get('attempts')):
                        _put(db,marker,required)
        return _view(record)

    def _load(self,db,request_id):
        key=_key(self.scope,request_id);record=_get(db,key)
        if record is None:raise MarketError('receipt_not_found','当前凭据和任务尚无该文件包记录。')
        return key,_validate(self.market,db,key,record)

    def status(self,request_id):
        with self.market.store.connect() as db:_,record=self._load(db,request_id)
        return _view(record)

    def acknowledge(self,request_id,bundle_sha256):
        _digest(bundle_sha256,'bundle_sha256')
        with self.market.store.connect(write=True) as db:
            key,record=self._load(db,request_id);offer=record['offer']
            if bundle_sha256!=offer['bundle_sha256']:
                raise MarketError('receipt_digest_mismatch','客户端声明的包摘要与原交付不符。')
            if record['acknowledgement'] is None:
                record['acknowledgement']={'receipt_id':'cr_'+_hash([key,record['offer_sha256']]),
                    'offer_sha256':record['offer_sha256'],'bundle_sha256':bundle_sha256,
                    'acknowledged_at':self.market.clock(),'evidence':'authenticated_client_statement'}
                _validate(self.market,db,key,record);_put(db,key,record)
            if offer['order_id'] is not None:
                required=_get(db,'remote_file_delivery:'+offer['order_id'])
                if required and required['usage_id']==offer['usage_id']:
                    pointer='order_client_receipt:'+offer['order_id']
                    if _get(db,pointer) is None:_put(db,pointer,key)
        return _view(record)


def require_client_receipt(market,db,order_id,delivery):
    """Guard channel preparation for an order marked as delivered over file HTTP."""
    required=_get(db,'remote_file_delivery:'+order_id)
    if required is None:return
    if required!={'acquisition_id':delivery['acquisition_id'],'usage_id':delivery['usage_id'],
                  'delivery_sha256':_hash(delivery)}:
        raise MarketError('receipt_binding_mismatch','原订单的远程交付绑定已变化。')
    key=_get(db,'order_client_receipt:'+order_id)
    record=_get(db,key) if isinstance(key,str) else None
    if record is None:raise MarketError('client_receipt_required','此远程文件订单尚无客户端收货声明。')
    _validate(market,db,key,record,historical=True)
    if (record['acknowledgement'] is None or record['offer']['order_id']!=order_id
            or record['offer']['usage_id']!=delivery['usage_id']):
        raise MarketError('receipt_binding_mismatch','客户端收货声明没有对应原订单交付。')
