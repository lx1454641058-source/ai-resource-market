"""本项目原创。可信宿主到供应商的固定HTTP连接；无付款、安装或自动重试。"""
import hashlib
import hmac
import http.client
import ipaddress
import math
import re
import time
from urllib.parse import urlsplit

from .api_quota import MAX_INPUT, MAX_RESULT, canonical, checksum
from .common import MarketError, fields, identifier, integer, parse_json

FORMAT='market-provider-api-v1'
MAX_WIRE=MAX_RESULT+4096


def _key(key):
    if not isinstance(key,bytes) or not 32<=len(key)<=4096:
        raise MarketError('invalid_configuration','供应商专用连接密钥不可用。')


def _signature(raw,key):
    _key(key)
    return hmac.new(key,raw,hashlib.sha256).hexdigest()


def _verify(raw,signature,key):
    if (not isinstance(signature,str) or not re.fullmatch('[0-9a-f]{64}',signature)
            or not hmac.compare_digest(_signature(raw,key),signature)):
        raise MarketError('provider_signature_mismatch','供应商连接签名不符。')


def _request(value):
    fields(value,('format','provider','binding_id','action','operation_id','issued_at','expires_at'),
           ('operation','input'))
    if value['format']!=FORMAT or value['action'] not in ('execute','lookup'):
        raise MarketError('invalid_request','供应商请求版本或操作无效。')
    for name in ('provider','binding_id','operation_id'):identifier(value[name],name)
    for name in ('issued_at','expires_at'):integer(value[name],0,9999999999,name)
    if not 0<value['expires_at']-value['issued_at']<=60:
        raise MarketError('invalid_request','请求有效期无效。')
    if value['action']=='execute':
        if set(value)!={'format','provider','binding_id','action','operation_id','issued_at','expires_at','operation','input'}:
            raise MarketError('invalid_request','执行请求缺少固定字段。')
        identifier(value['operation'],'operation')
        if not isinstance(value['input'],dict):raise MarketError('invalid_request','API输入需要对象。')
        canonical(value['input'],MAX_INPUT)
    elif 'operation' in value or 'input' in value:
        raise MarketError('invalid_request','原调用查询不得携带新输入。')


def verify_provider_request(raw,signature,key,provider,binding_id,authorize,*,now=None):
    """Provider ingress: authenticate AND independently approve the saved operation.

    authorize(request) must consult provider-approved connection/operation records,
    enforce operation and data scope, and return True. A MAC is never payment proof.
    Return a stable fingerprint for durable provider deduplication (timestamps omitted).
    """
    if not isinstance(raw,bytes) or len(raw)>MAX_INPUT+4096:
        raise MarketError('invalid_request','供应商请求过大或类型无效。')
    _verify(raw,signature,key)
    value=parse_json(raw);_request(value)
    if value['provider']!=provider or value['binding_id']!=binding_id:
        raise MarketError('provider_scope_mismatch','供应商或连接范围不符。')
    now=time.time() if now is None else now
    if type(now) not in (int,float) or not value['issued_at']<=now<=value['expires_at']:
        raise MarketError('provider_request_expired','供应商请求已过期。')
    if not callable(authorize) or authorize(parse_json(canonical(value,MAX_INPUT+4096))) is not True:
        raise MarketError('entitlement_denied','供应商未批准此原操作。')
    fingerprint=checksum(canonical({k:v for k,v in value.items() if k not in ('issued_at','expires_at')},MAX_INPUT+4096))
    return value,fingerprint


def _response(value,request,request_sha256):
    fields(value,('format','provider','binding_id','operation_id','request_sha256','state'),
           ('result','evidence_ref','fenced'))
    for name in ('format','provider','binding_id','operation_id'):
        if value[name]!=request[name]:raise MarketError('provider_scope_mismatch','原站结果不属于本次请求。')
    if value['request_sha256']!=request_sha256:
        raise MarketError('provider_scope_mismatch','原站响应没有绑定原请求。')
    extras=set(value)-{'format','provider','binding_id','operation_id','request_sha256','state'}
    if value['state']=='completed' and extras=={'result','evidence_ref'}:
        identifier(value['evidence_ref'],'evidence_ref');canonical(value['result'],MAX_RESULT)
    elif value['state']=='unknown' and not extras:pass
    elif (request['action']=='lookup' and value['state']=='not_executed'
            and extras=={'evidence_ref','fenced'} and value['fenced'] is True):
        identifier(value['evidence_ref'],'evidence_ref')
    else:raise MarketError('invalid_provider_response','原站结果状态或证据无效；保留原调用。')


def provider_response(request_raw,key,evidence):
    """Serialize an already-authorized provider's original record, never execute work.

    not_executed requires durable cancellation/fencing, not a missing lookup row.
    This helper does not implement authorization, deduplication or cancellation.
    """
    if not isinstance(request_raw,bytes) or len(request_raw)>MAX_INPUT+4096:
        raise MarketError('invalid_request','原请求无效。')
    request=parse_json(request_raw);_request(request)
    fields(evidence,('state',),('result','evidence_ref','fenced'))
    value={k:request[k] for k in ('format','provider','binding_id','operation_id')}
    value.update(request_sha256=hashlib.sha256(request_raw).hexdigest(),**evidence)
    _response(value,request,value['request_sha256'])
    raw=canonical(value,MAX_WIRE).encode()
    return raw,_signature(raw,key)


class ApiProviderClient:
    """Configure outside model tools; attach execute/lookup to ApiDeliveryHost.

    A separate endpoint/key/binding per provider, fixed operation allowlist, no
    environment proxy, redirects or retry. Provider must durably deduplicate IDs.
    """
    def __init__(self,provider,endpoint,key,binding_id,operations,*,timeout=10,clock=None):
        self.provider=identifier(provider,'provider');self.binding_id=identifier(binding_id,'binding_id')
        _key(key)
        if (not isinstance(endpoint,str) or any(ord(c)<=32 or ord(c)==127 for c in endpoint)):
            raise MarketError('invalid_endpoint','供应商端点无效。')
        try:
            parsed=urlsplit(endpoint)
            if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment or parsed.port==0):raise ValueError
            if parsed.scheme=='http' and not ipaddress.ip_address(parsed.hostname).is_loopback:raise ValueError
            parsed.hostname.encode('ascii');(parsed.path or '/').encode('ascii')
        except (ValueError,UnicodeError):
            raise MarketError('invalid_endpoint','固定端点需为HTTPS，HTTP仅限本机IP，禁止凭据和查询串。') from None
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=30:
            raise MarketError('invalid_configuration','单次网络等待上限需在0至30秒之间。')
        if not isinstance(operations,(list,tuple)) or not 1<=len(operations)<=32:
            raise MarketError('invalid_configuration','需要固定的供应商操作清单。')
        for operation in operations:identifier(operation,'operation')
        self.operations=frozenset(operations);self.endpoint=endpoint
        self._parsed=parsed;self._key=key;self.timeout=timeout;self.clock=clock or time.time

    def _exchange(self,action,operation_id,operation=None,inputs=None):
        identifier(operation_id,'operation_id')
        now=self.clock()
        if type(now) not in (int,float) or not math.isfinite(now) or not 0<=now<=9999999939:
            raise MarketError('invalid_clock','连接请求时间无效。')
        request={'format':FORMAT,'provider':self.provider,'binding_id':self.binding_id,'action':action,
                 'operation_id':operation_id,'issued_at':int(now),'expires_at':int(now)+60}
        if action=='execute':
            if operation not in self.operations:raise MarketError('provider_operation_denied','供应商连接未批准此操作。')
            request.update(operation=operation,input=inputs)
        _request(request);raw=canonical(request,MAX_INPUT+4096).encode()
        body,signature=self._post(raw)
        _verify(body,signature,self._key)
        reply=parse_json(body);_response(reply,request,hashlib.sha256(raw).hexdigest())
        return reply

    def _post(self,raw):
        parsed=self._parsed
        connection=(http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection)(
            parsed.hostname,parsed.port,timeout=self.timeout)
        deadline=time.monotonic()+self.timeout;reply=None
        try:
            connection.request('POST',parsed.path or '/',body=raw,headers={
                'Content-Type':'application/json','Accept':'application/json','Connection':'close',
                'X-Market-Signature':_signature(raw,self._key)})
            sock=connection.sock
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError
            sock.settimeout(remaining);reply=connection.getresponse()
            for name in ('Content-Type','Content-Encoding','Content-Length','X-Market-Response-Signature'):
                if len(reply.headers.get_all(name,[]))>1:raise ValueError
            if (reply.status!=200 or reply.headers.get_content_type()!='application/json'
                    or reply.headers.get('Content-Encoding','identity')!='identity'):raise ValueError
            length=reply.headers.get('Content-Length')
            if length is not None and (not re.fullmatch('[0-9]+',length) or int(length)>MAX_WIRE):raise ValueError
            body=bytearray()
            while not reply.isclosed():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError
                sock.settimeout(remaining)
                chunk=reply.read1(min(4096,MAX_WIRE+1-len(body)))
                if not chunk:break
                body.extend(chunk)
                if len(body)>MAX_WIRE:raise ValueError
            if length is not None and len(body)!=int(length):raise ValueError
            return bytes(body),reply.headers.get('X-Market-Response-Signature')
        except (OSError,ValueError,http.client.HTTPException):
            raise MarketError('provider_result_unknown','供应商响应未知或无效；保留原操作，不自动重试。') from None
        finally:
            if reply is not None:reply.close()
            connection.close()

    def execute(self,operation_id,operation,inputs):
        reply=self._exchange('execute',operation_id,operation,inputs)
        if reply['state']!='completed':
            raise MarketError('provider_result_unknown','原调用未确认完成；只能查询原操作。')
        return reply['result']

    def lookup(self,operation_id):
        reply=self._exchange('lookup',operation_id)
        return {k:reply[k] for k in ('state','result','evidence_ref') if k in reply}
