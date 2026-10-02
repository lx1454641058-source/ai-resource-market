"""来源：本项目原创。可信宿主的采购文件接收器；固定服务、原请求恢复、不执行钱包。"""
from dataclasses import dataclass, field
import hashlib
from http.client import HTTPException
import io
import os
from pathlib import Path
import re
import tempfile
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .common import MarketError, choice, compact, fields, identifier, integer, parse_json, text
from .http_client import RemoteMarket
from .origin import _safe_path


MAX_FILE = 2 * 1024 * 1024
MAX_META = 131072
MAX_ZIP = MAX_FILE + 2 * MAX_META + 1024
SUFFIXES = {'text/markdown': '.md', 'text/csv': '.csv', 'application/pdf': '.pdf',
            'image/png': '.png', 'audio/wav': '.wav'}


def sha(raw):return hashlib.sha256(raw).hexdigest()


def digest(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise MarketError('invalid_digest','需要原任务保存的SHA256摘要。')
    return value


def validate_download(raw, expected_sha256, expected):
    """Check the entire bounded v1 envelope before creating any resource file.

    A digest from the authenticated server detects corruption, not independent
    authorship/rights. Expected task/release values come from the trusted host.
    """
    try:
        digest(expected_sha256)
        if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_ZIP or sha(raw)!=expected_sha256:
            raise ValueError
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=archive.infolist()
            names=[entry.filename for entry in entries]
            if (len(entries)!=3 or len(set(names))!=3 or names[1:]!=['LICENSE.txt','manifest.json']
                    or names[0] not in ['resource'+suffix for suffix in SUFFIXES.values()] or archive.comment):
                raise ValueError
            contents={}
            for index,entry in enumerate(entries):
                maximum=MAX_FILE if index==0 else MAX_META
                if (entry.compress_type!=zipfile.ZIP_STORED or entry.compress_size!=entry.file_size
                        or not 0<entry.file_size<=maximum or entry.flag_bits!=0 or entry.extra or entry.comment
                        or entry.create_system!=3 or entry.external_attr>>16!=0o100644):
                    raise ValueError
                contents[entry.filename]=archive.read(entry)
        manifest=parse_json(contents['manifest.json'])
        fields(manifest,('format','id','version','acquisition_id','task_id','request_id','usage_id','purpose',
                         'release_digest','resource','license','source_statement','task_completed',
                         'execution_authorized','client_receipt_confirmed'),('rights',))
        if (manifest['format']!='market-resource-zip-v1' or any(manifest[k] is not False for k in
                ('task_completed','execution_authorized','client_receipt_confirmed'))):
            raise ValueError
        for key in ('id','version','acquisition_id','task_id','request_id','usage_id'):
            identifier(manifest[key],key)
        fields(expected,('id','version','task_id','request_id','purpose','release_digest'))
        if any(manifest[k]!=v for k,v in expected.items()):
            raise MarketError('download_scope_mismatch','资源包不属于当前任务、请求、用途或选定发行版本。')
        digest(manifest['release_digest'])
        fields(manifest['resource'],('path','media_type','sha256','size'))
        fields(manifest['license'],('path','name','sha256','size'))
        resource,license_info=manifest['resource'],manifest['license']
        choice(resource['media_type'],SUFFIXES,'media_type')
        if resource['path']!='resource'+SUFFIXES[resource['media_type']] or resource['path']!=names[0]:
            raise ValueError
        if license_info['path']!='LICENSE.txt':raise ValueError
        text(license_info['name'],'license_name',100)
        text(manifest['source_statement'],'source_statement',1200)
        if 'rights' in manifest:
            rights=manifest['rights']
            fields(rights,('rights_holder','attribution','modification','source_redistribution','training','restrictions'))
            for key in ('rights_holder','restrictions'):text(rights[key],key,1200)
            for key in ('modification','source_redistribution','training'):
                choice(rights[key],('allowed','forbidden','unknown'),key)
            attr=rights['attribution'];fields(attr,('required','text'))
            if type(attr['required']) is not bool or (attr['required'] is False and attr['text']!=''):
                raise ValueError
            if attr['required']:text(attr['text'],'attribution.text',1200)
        for part,maximum in ((resource,MAX_FILE),(license_info,MAX_META)):
            integer(part['size'],1,maximum,'size');digest(part['sha256'])
            body=contents[part['path']]
            if len(body)!=part['size'] or sha(body)!=part['sha256']:raise ValueError
        contents['LICENSE.txt'].decode('utf-8')
        # Require the exact canonical v1 container, rejecting hidden/trailing data,
        # conflicting local/central ZIP headers and non-standard member attributes.
        canonical=io.BytesIO()
        with zipfile.ZipFile(canonical,'w',compression=zipfile.ZIP_STORED) as rebuilt:
            for name in names:
                info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
                info.create_system=3;info.external_attr=0o100644<<16
                rebuilt.writestr(info,contents[name])
        if canonical.getvalue()!=raw:raise ValueError
        return manifest,contents
    except MarketError as exc:
        if exc.code=='download_scope_mismatch':raise
        raise MarketError('invalid_download','文件包或许可证未通过完整性核验，未交付。') from None
    except (ValueError,UnicodeError,KeyError,TypeError,RuntimeError,zipfile.BadZipFile,NotImplementedError):
        raise MarketError('invalid_download','文件包或许可证未通过完整性核验，未交付。') from None


def _read(path,limit):
    target=_safe_path(path)
    with target.open('rb') as handle:raw=handle.read(limit+1)
    if len(raw)>limit:raise MarketError('output_conflict','已有交付文件超限，保留原文件。')
    return raw


def _save(path,raw):
    """Atomically create verified bytes; never replace or append to an existing file.

    The temporary file and target share a filesystem. Hard-link creation exposes
    only a complete file and fails if another process already wrote the target.
    """
    target=_safe_path(path)
    created=False
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='.download-',dir=target.parent) as staging:
            candidate=Path(staging)/'verified'
            with candidate.open('xb') as handle:
                handle.write(raw);handle.flush();os.fsync(handle.fileno())
            try:
                os.link(candidate,_safe_path(target));created=True
            except FileExistsError:pass
    if _read(target,len(raw))!=raw:
        raise MarketError('output_conflict','文件写入结果不一致；保留原请求和已有文件。')
    return created


@dataclass(frozen=True, repr=False)
class PurchaseRequestSnapshot:
    """Trusted-host memory only. Never serialize or expose to model tools/logs.

    Immutable copies of the request that produced the first resource 402, not a
    reconstructed handoff request. This is evidence, not payment authorization.
    """
    url: str
    method: str
    body: bytes
    headers: tuple[tuple[str, str], ...]
    payment_needed: bytes
    task_id: str
    purpose: str
    order_id: str
    request_id: str
    release_digest: str
    resource_id: str
    version: str
    key_id: str


@dataclass(frozen=True)
class PurchaseResponse:
    result: dict
    # Do not include a possibly sensitive wallet bill in logs/repr or model summaries.
    payment_needed: str | None = field(default=None,repr=False)
    request_snapshot: PurchaseRequestSnapshot | None = field(default=None,repr=False)


class PurchaseDownloader:
    """Construct in trusted host code; models choose goods, never endpoints/credentials/output roots."""
    def __init__(self,remote,task_id,purpose,output_root):
        if type(remote) is not RemoteMarket:raise MarketError('invalid_client','需要已配置的机器客户端。')
        self.remote=remote
        self.task_id=identifier(task_id,'task_id')
        self.purpose=choice(purpose,('personal_use','commercial_use'),'purpose')
        self.root=_safe_path(output_root)
        if not self.root.is_dir():raise MarketError('invalid_path','交付根目录需由宿主预先准备。')
        self._endpoint=remote.base_url

    @staticmethod
    def _header(response,name,required=False):
        values=response.headers.get_all(name,[])
        if len(values)>1 or (required and not values):
            raise MarketError('invalid_response','必要响应头缺失或重复。')
        return values[0] if values else None

    def acknowledge(self,resource_id,version,request_id,expected_release_digest):
        """Explicit host action: recheck local bytes, then report the same bundle.

        Unknown responses are not retried here. Repeating this method is safe for
        the same receipt; it never invokes a wallet or redownloads a resource.
        """
        expected={'id':identifier(resource_id,'id'),'version':identifier(version,'version'),
                  'request_id':identifier(request_id,'request_id'),'task_id':self.task_id,
                  'purpose':self.purpose,'release_digest':digest(expected_release_digest)}
        if self.remote.base_url!=self._endpoint:
            raise MarketError('invalid_endpoint','固定服务地址发生变化，停止收货声明。')
        token=self.remote.credential()
        binding={'format':'purchase-download-intent-v1','endpoint':self._endpoint,
                 'key_id':token.split('.')[0],'expected':expected}
        folder=_safe_path(self.root/sha(compact([self._endpoint,binding['key_id'],self.task_id,request_id]).encode()))
        intent=_read(folder/'intent.json',16384)
        if parse_json(intent)!=binding:
            raise MarketError('download_scope_mismatch','本地文件不属于当前任务或原请求。')
        receipt=parse_json(_read(folder/'receipt.json',16384))
        fields(receipt,('format','binding_sha256','bundle_sha256','acquisition_id','usage_id',
                        'local_files_verified','server_acknowledged','task_completed'))
        if (receipt['format']!='purchase-download-receipt-v1' or receipt['binding_sha256']!=sha(intent)
                or receipt['local_files_verified'] is not True or receipt['server_acknowledged'] is not False
                or receipt['task_completed'] is not False):
            raise MarketError('invalid_download','本地收货证据无效。')
        manifest_raw=_read(folder/'manifest.json',MAX_META);manifest=parse_json(manifest_raw)
        # Validate the filename before it can select any filesystem path.
        if not isinstance(manifest,dict) or not isinstance(manifest.get('resource'),dict):
            raise MarketError('invalid_download','本地资源清单无效。')
        name=manifest.get('resource',{}).get('path')
        if name not in ['resource'+suffix for suffix in SUFFIXES.values()]:
            raise MarketError('invalid_download','资源文件名无效。')
        contents={name:_read(folder/name,MAX_FILE),'LICENSE.txt':_read(folder/'LICENSE.txt',MAX_META),
                  'manifest.json':manifest_raw}
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
            for filename,raw in contents.items():
                info=zipfile.ZipInfo(filename,(1980,1,1,0,0,0));info.create_system=3;info.external_attr=0o100644<<16
                archive.writestr(info,raw)
        manifest,_=validate_download(output.getvalue(),receipt['bundle_sha256'],expected)
        if any(receipt[key]!=manifest[key] for key in ('acquisition_id','usage_id')):
            raise MarketError('download_scope_mismatch','本地收货记录与实际资源权益不符。')
        payload={'request_id':request_id,'bundle_sha256':receipt['bundle_sha256']}
        # Persist only the exact non-secret declaration, before any network side effect.
        _save(folder/'ack-request.json',compact(payload).encode())
        try:result=self.remote.request('/v1/purchases/receipt',payload)
        except HTTPException:
            raise MarketError('transport_unknown','收货声明结果未知；按原声明核对，不重新付款。') from None
        if result.get('status')=='error':return result
        fields(result,('status','request_id','bundle_sha256','acquisition_id','usage_id','receipt_id','acknowledged_at',
                       'evidence','server_acknowledged','channel_fulfillment_confirmed','task_completed'))
        if (result['status']!='client_receipt_recorded' or result['request_id']!=request_id
                or result['bundle_sha256']!=receipt['bundle_sha256']
                or any(result[key]!=receipt[key] for key in ('acquisition_id','usage_id'))
                or result['evidence']!='authenticated_client_statement' or result['server_acknowledged'] is not True
                or result['channel_fulfillment_confirmed'] is not False or result['task_completed'] is not False
                or not isinstance(result['receipt_id'],str) or not re.fullmatch('cr_[0-9a-f]{64}',result['receipt_id'])
                or type(result['acknowledged_at']) not in (int,float) or not 0<=result['acknowledged_at']<10**12):
            raise MarketError('invalid_response','服务端回执与原收货声明不符。')
        _save(folder/'server-receipt.json',compact(result).encode())
        return result

    def resource(self,resource_id,version,request_id,expected_release_digest,*,payment_proof=None):
        return self._receive(resource_id,version,request_id,expected_release_digest,'resource',payment_proof)

    def handoff(self,resource_id,version,request_id,expected_release_digest):
        """Trusted host explicitly recovers the original wallet bill; no wallet execution."""
        return self._receive(resource_id,version,request_id,expected_release_digest,'handoff',None)

    def _receive(self,resource_id,version,request_id,expected_release_digest,action,payment_proof):
        value={'id':identifier(resource_id,'id'),'version':identifier(version,'version'),
               'request_id':identifier(request_id,'request_id')}
        expected={**value,'task_id':self.task_id,'purpose':self.purpose,
                  'release_digest':digest(expected_release_digest)}
        if self.remote.base_url!=self._endpoint:
            raise MarketError('invalid_endpoint','固定服务地址发生变化，停止原请求。')
        if payment_proof is not None:
            from .machine_pay import decode_payment_proof
            decode_payment_proof(payment_proof)
            if isinstance(payment_proof,bytes):payment_proof=payment_proof.decode('ascii')
        token=self.remote.credential()
        binding={'format':'purchase-download-intent-v1','endpoint':self._endpoint,
                 'key_id':token.split('.')[0],'expected':expected}
        # Request namespace excludes resource/version: a reused logical ID cannot hide another purchase.
        namespace=[self._endpoint,binding['key_id'],self.task_id,request_id]
        folder=_safe_path(self.root/sha(compact(namespace).encode()))
        intent=compact(binding).encode()
        folder.mkdir(exist_ok=True)
        first_intent=False
        if (folder/'intent.json').exists():
            if _read(folder/'intent.json',16384)!=intent:
                raise MarketError('idempotency_conflict','原请求已绑定其他商品或用途。')
        else:
            if any(folder.iterdir()):
                raise MarketError('output_conflict','目录缺少原请求绑定，保留已有内容。')
            # Only the atomic creator may capture a first payment request; two
            # concurrent callers must not both infer "first" from exists().
            first_intent=_save(folder/'intent.json',intent)
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/zip'}
        if payment_proof is not None:headers['Payment-Proof']=payment_proof
        payload={**value,'expected_release_digest':expected_release_digest} if action=='resource' else {'request_id':request_id}
        request=Request(self._endpoint+'/v1/purchases/'+action,data=compact(payload).encode(),headers=headers,method='POST')
        # Copy before transport handlers can mutate Request. Only custom headers
        # are replay arguments; implicit HTTP framing is managed by transport.
        request_copy=(request.full_url,request.get_method(),bytes(request.data),tuple(request.header_items()))
        try:
            try:response=self.remote.opener.open(request,timeout=self.remote.timeout)
            except HTTPError as exc:response=exc
            with response:
                if 300<=response.code<400:raise MarketError('redirect_denied','拒绝重定向，未转发凭据。')
                media=self._header(response,'Content-Type',True).split(';',1)[0].strip().lower()
                if self._header(response,'Content-Encoding') is not None:
                    raise MarketError('invalid_response','不接受编码后的响应。')
                maximum=MAX_ZIP if media=='application/zip' else 1048576
                length=self._header(response,'Content-Length')
                if length is not None and (not re.fullmatch('[0-9]{1,9}',length) or int(length)>maximum):
                    raise MarketError('invalid_response','响应长度无效或超限。')
                raw=response.read(maximum+1)
                if len(raw)>maximum or (length is not None and len(raw)!=int(length)):
                    raise MarketError('invalid_response','响应不完整或超限，保留原请求。')
                if media=='application/zip':
                    if response.code!=200:raise MarketError('invalid_response','文件响应状态无效。')
                    bundle_sha=self._header(response,'X-Bundle-SHA256',True)
                    manifest,contents=validate_download(raw,bundle_sha,expected)
                elif media=='application/json':
                    result=parse_json(raw)
                    if not isinstance(result,dict) or not isinstance(result.get('status'),str):
                        raise MarketError('invalid_response','服务响应缺少业务状态。')
                    bill=self._header(response,'Payment-Needed')
                    if response.code==402:
                        if (result['status']!='payment_needed' or result.get('request_id')!=request_id
                                or result.get('release_digest')!=expected_release_digest or not bill
                                or not re.fullmatch('[A-Za-z0-9+/_=-]{1,16384}',bill)):
                            raise MarketError('invalid_response','账单不符合原选定发行版本，未交给钱包。')
                        snapshot=None
                        if action=='resource' and payment_proof is None and first_intent:
                            order_id=identifier(result.get('order_id'),'order_id')
                            snapshot=PurchaseRequestSnapshot(*request_copy,bill.encode('ascii'),
                                self.task_id,self.purpose,order_id,request_id,expected_release_digest,
                                resource_id,version,binding['key_id'])
                        return PurchaseResponse(result,bill,snapshot)
                    if bill is not None or result['status']=='payment_needed':
                        raise MarketError('invalid_response','账单与HTTP状态不一致。')
                    if result['status']=='read' and result.get('release_digest')!=expected_release_digest:
                        raise MarketError('download_scope_mismatch','文本资源没有匹配原核对发行条件。')
                    if ((response.code==200 and result['status']!='read') or
                            (response.code==202 and result['status'] not in ('purchase_pending','host_transfer_required')) or
                            (response.code>=400 and result['status']!='error') or
                            (response.code<400 and response.code not in (200,202))):
                        raise MarketError('invalid_response','HTTP与业务状态不一致。')
                    return PurchaseResponse(result)
                else:raise MarketError('invalid_response','服务返回了不支持的内容类型。')
        except MarketError:raise
        except (URLError,OSError,ValueError,HTTPException):
            raise MarketError('transport_unknown','请求结果可能未知；保留原编号，不自动重试或再次付款。') from None
        # All input was verified before exposing any resource file. Saving is resumable
        # and the completion receipt is written only after every byte is rechecked.
        receipt={'format':'purchase-download-receipt-v1','binding_sha256':sha(intent),'bundle_sha256':bundle_sha,
                 'acquisition_id':manifest['acquisition_id'],'usage_id':manifest['usage_id'],
                 'local_files_verified':True,'server_acknowledged':False,'task_completed':False}
        try:
            for name,body in contents.items():_save(folder/name,body)
            _save(folder/'receipt.json',compact(receipt).encode())
        except OSError:
            raise MarketError('download_not_saved','本地保存中断；保留原请求和已核验文件，不再次付款。') from None
        return PurchaseResponse({'status':'downloaded','path':str(folder/manifest['resource']['path']),
            'license_path':str(folder/'LICENSE.txt'),'manifest_path':str(folder/'manifest.json'),
            'receipt_path':str(folder/'receipt.json'),'bundle_sha256':bundle_sha,
            'acquisition_id':manifest['acquisition_id'],'usage_id':manifest['usage_id'],
            'task_id':self.task_id,'request_id':request_id,'local_files_verified':True,
            'server_acknowledged':False,'task_completed':False,'execution_authorized':False})
