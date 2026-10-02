"""来源：本项目原创。已认证任务的Machine Pay头交接；不调用钱包。

HTTP层先验证身份，任务范围和原订单再约束支付凭证。秘密仅在当前调用使用。
"""
import base64
import binascii
import hashlib
from pathlib import Path
import re

from .alipay_proof import request_digest
from .common import MarketError, compact, fields, identifier, parse_json
from .contracts import HostPolicy
from .payment_orders import _digest, _hash
from .purchase_host import PurchasingTaskResources
from .task_budget_gateway import TaskBudgetGateway


def decode_payment_proof(value):
    try:
        if isinstance(value,bytes):value=value.decode('ascii')
        if (not isinstance(value,str) or not 1<=len(value)<=8192
                or not re.fullmatch(r'[A-Za-z0-9+/_-]+={0,2}',value)
                or (any(c in value for c in '+/') and any(c in value for c in '-_'))):
            raise ValueError
        urlsafe=any(c in value for c in '-_')
        raw=base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_' if urlsafe else None,validate=True)
        canonical=(base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode('ascii')
        if value not in (canonical,canonical.rstrip('=')) or len(raw)>6144:
            raise ValueError
        outer=parse_json(raw);fields(outer,('protocol',),('method',))
        fields(outer['protocol'],('payment_proof','trade_no'))
        method=outer.get('method',{});fields(method,(),('client_session',))
        request={**outer['protocol'],**method}
        request_digest(request,{})  # Reuse exact channel bounds; this does not validate payment.
        return request
    except (ValueError,UnicodeError,binascii.Error):
        raise MarketError('invalid_payment_header','Payment-Proof格式或字段无效，未开始渠道核验。') from None


class MachinePayGateway:
    def __init__(self,gateway,hosts,*,configuration_check=None):
        if type(gateway) is not TaskBudgetGateway or not isinstance(hosts,dict) or not hosts:
            raise MarketError('purchase_host_mismatch','采购HTTP必须显式绑定已有身份、任务预算和采购宿主。')
        self.gateway,self.hosts=gateway,dict(hosts)
        if configuration_check is not None and not callable(configuration_check):
            raise MarketError('purchase_host_mismatch','采购配置检查必须由可信宿主提供。')
        self._configuration_check=configuration_check
        for key,value in self.hosts.items():
            if not isinstance(key,str) or not re.fullmatch(r'ak_[0-9a-f]{32}',key):
                raise MarketError('purchase_host_mismatch','采购宿主映射需要凭据编号，不接受Token秘密。')
            self._group(value)

    @staticmethod
    def _group(value):
        group=({value.orders.merchant['market_seller']:value} if type(value) is PurchasingTaskResources else value)
        if not isinstance(group,dict) or not group or len(group)>32:
            raise MarketError('purchase_host_mismatch','需要有界的原卖家采购宿主。')
        first=next(iter(group.values()))
        if type(first) is not PurchasingTaskResources:
            raise MarketError('purchase_host_mismatch','采购宿主类型不符。')
        for seller,host in group.items():
            if (type(host) is not PurchasingTaskResources or seller!=host.orders.merchant['market_seller']
                    or host.billing._scope!=first.billing._scope or host.purpose!=first.purpose
                    or host.market.host_policy!=first.market.host_policy):
                raise MarketError('purchase_host_mismatch','多卖家采购必须共用原任务、用途、预算和市场范围。')
        return group

    def _host(self,token,active=True,*,request_id=None,resource=None):
        if self._configuration_check is not None:self._configuration_check()
        identity=self.gateway.identities.authenticate(token)
        value=self.hosts.get(identity['key_id'])
        if identity['role']!='buyer' or value is None:
            raise MarketError('forbidden','此凭据未绑定采购宿主。')
        group=self._group(value)
        host=next(iter(group.values()))
        with self.gateway.budgets.store.connect() as db:
            binding=self.gateway._binding(db,identity)
            if (host.market.principal!=identity['principal'] or host.market.host_policy!=HostPolicy.from_json(identity['policy'])
                    or host.task_id!=binding['task_id'] or host.budget.root_id!=binding['root_id']
                    or Path(host.market.store.path).resolve()!=Path(self.gateway.database).resolve()
                    or Path(host.budget.ledger.store.path).resolve()!=Path(self.gateway.budgets.store.path).resolve()):
                raise MarketError('budget_scope_mismatch','HTTP身份、策略、市场或任务与采购宿主不一致。')
            host.budget._bound(db,active=active)
        if len(group)>1 and request_id is not None:
            from .purchase_routing import select_host
            host=select_host(group,identity['key_id'],request_id,resource)
        return host

    def call(self,token,action,value,proof=None,want_file=False):
        history_action=action in ('status','receipt','receipt_status')
        host=self._host(token,active=not history_action)
        expected_release=None
        if action=='resource':
            fields(value,('id','version','request_id'),('expected_release_digest',))
            expected_release=value.get('expected_release_digest')
            if expected_release is not None:_digest(expected_release,'expected_release_digest')
            value={k:value[k] for k in ('id','version','request_id')}
            for name in value:identifier(value[name],name)
            host=self._host(token,request_id=value['request_id'],resource=(value['id'],value['version']))
            if expected_release is not None:
                with host.market.store.connect() as db:
                    if host.market._release_digest(db,value['id'],value['version'])!=expected_release:
                        raise MarketError('release_changed','资源发行条件与客户端原核对结果不符。')
            if proof is None:
                result=host.use(value['id'],value['version'],value['request_id'])
            else:
                request=decode_payment_proof(proof)
                flow=host._required(value['request_id'])
                if [flow['terms']['resource'],flow['terms']['version']]!=[value['id'],value['version']]:
                    raise MarketError('purchase_binding_mismatch','支付凭证只能用于原任务请求的资源。')
                if flow['order_id'] is None:
                    raise MarketError('handoff_required','先恢复原账单交接，不凭支付头另开订单。')
                # Equivalent JSON/Base64 encodings recover the same verification attempt.
                attempt='http-'+_hash([flow['order_id'],request])
                result=host.verify_payment(value['request_id'],attempt,request)
        elif action in ('status','handoff'):
            fields(value,('request_id',));identifier(value['request_id'],'request_id')
            if proof is not None:
                raise MarketError('invalid_payment_header','此入口不接收支付凭证。')
            host=self._host(token,active=not history_action,request_id=value['request_id'])
            result=(host.status if action=='status' else host.handoff)(value['request_id'])
        elif action in ('receipt','receipt_status'):
            from .client_receipts import ClientReceipts
            fields(value,('request_id','bundle_sha256') if action=='receipt' else ('request_id',))
            if proof is not None:raise MarketError('invalid_payment_header','收货入口不接收付款凭证。')
            host=self._host(token,active=False,request_id=value['request_id'])
            identity=self.gateway.identities.authenticate(token)
            receipts=ClientReceipts(host,identity['key_id'])
            result=(receipts.acknowledge(value['request_id'],value['bundle_sha256']) if action=='receipt'
                    else receipts.status(value['request_id']))
        else:
            raise MarketError('not_found','采购入口不存在。')
        bundle=None
        if action=='resource' and want_file:
            from .resource_bundle import resource_bundle
            self._host(token)
            bundle=resource_bundle(host,result,value)
        # Observe identity/binding revocation during a channel wait before returning content.
        self._host(token,active=not history_action)
        if expected_release is not None:
            with host.market.store.connect() as db:
                if host.market._release_digest(db,value['id'],value['version'])!=expected_release:
                    raise MarketError('release_changed','交付期间资源发行条件发生变化，停止返回。')
            if result.get('status')=='read':result={**result,'release_digest':expected_release}
        if result.get('status')=='payment_needed':
            # The receiving host checks its selected release before passing a bill to a wallet.
            result={**result,'release_digest':host._required(value['request_id'])['terms']['release_digest']}
        validation=[]
        if action=='resource' and result.get('paid_verified') is True and (bundle is not None or result.get('status')=='read'):
            # This header describes the original market's verified payment. It
            # is not a wallet proof, a channel fulfillment acknowledgement or
            # permission for another merchant/resource to release content.
            flow=host._required(value['request_id'])
            if result.get('order_id')!=flow['order_id']:
                raise MarketError('payment_binding_mismatch','交付响应没有对应原付款订单。')
            record=host.orders.get(flow['order_id'])
            from .payment_fulfillment import validate_confirmation
            confirmation=validate_confirmation(record)
            if confirmation is None:
                raise MarketError('payment_evidence_missing','交付缺少原订单的已验签付款记录。')
            evidence=confirmation['evidence']
            resource_id=record['prepared']['channel_identifiers']['resource_id']
            if evidence.get('resource_id')!=resource_id:
                raise MarketError('payment_binding_mismatch','付款核验没有对应原账单资源。')
            validation_value={'validated':True,'trade_no':identifier(evidence.get('trade_no'),'trade_no'),
                              'resource_id':resource_id}
            validation=[(b'payment-validation',base64.urlsafe_b64encode(compact(validation_value).encode()).rstrip(b'='))]
            if result.get('status')=='read':result={**result,'resource_id':resource_id}
        if bundle is not None:
            from .client_receipts import ClientReceipts
            identity=self.gateway.identities.authenticate(token)
            ClientReceipts(host,identity['key_id']).prepare(value,bundle)
            self._host(token)
            return 200,bundle,[(b'content-disposition',b'attachment; filename="resource.zip"'),
                              (b'x-bundle-sha256',hashlib.sha256(bundle).hexdigest().encode('ascii')),*validation]
        code,body,headers=self._reply(result)
        return code,body,[*headers,*validation]

    def describe(self,token):
        identity=self.gateway.identities.authenticate(token)
        if identity['key_id'] not in self.hosts or identity['role']!='buyer':
            return None
        try:
            host=self._host(token,active=False)
        except MarketError:
            return {'status':'unavailable'}
        return {'status':'host_configured','task_id':host.task_id,'purpose':host.purpose,
                'resource':'/v1/purchases/resource','status_route':'/v1/purchases/status',
                'handoff':'/v1/purchases/handoff','method':'POST',
                'receipt':'/v1/purchases/receipt','receipt_status':'/v1/purchases/receipt_status',
                'resource_fields':['id','version','request_id'],
                'proof_header':'Payment-Proof','wallet_execution_available':False,
                'file_accept':'application/zip','file_format':'market-resource-zip-v1',
                'file_includes':['resource','LICENSE.txt','manifest.json'],
                'file_retry':'same_resource_version_request_id_no_new_payment'}

    @staticmethod
    def _reply(result):
        result=dict(result);headers=[]
        if result['status']=='payment_needed':
            bill=result.pop('headers')['Payment-Needed']
            headers=[(b'payment-needed',bill.encode('ascii'))]
            return 402,result,headers
        if result['status']=='delivered' and 'path' in result:
            # Files are on the host, not this remote client's filesystem. Do not leak paths
            # or report that an HTTP caller received the bytes when only metadata was sent.
            result={k:result[k] for k in ('order_id','acquisition_id','request_id','paid_verified','sha256','size','usage_id') if k in result}
            result.update(status='host_transfer_required',next_action='trusted_host_transfer_file',task_completed=False)
        if result['status'] in ('purchase_pending','host_transfer_required'):
            return 202,result,headers
        from .http_app import response_status
        return response_status(result),result,headers
