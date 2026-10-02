"""来源：本项目原创。官方 Agent 支付的可信宿主准备器和显式一次执行器。

The host must supply an original downloader snapshot, its real user goal and a
new directory under a pre-provisioned private parent. On Windows directory mode
does not establish an ACL; privacy remains the caller's existing responsibility.
No API here is a model-callable tool. Never log/serialize the returned argv.
"""
from dataclasses import dataclass, field
import hashlib
import ipaddress
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .common import MarketError, compact, fields, identifier, integer, parse_json, text
from .origin import _safe_path
from .purchase_download import PurchaseRequestSnapshot, digest, sha

CLI_VERSION='0.4.5'
CLI_SHA256='da2b632e275ee2f4af7ba714ee5f5238738931e637e83a8764022f091a08fa28'


@dataclass(frozen=True)
class AlipayAgentPreparation:
    status: str
    manifest_path: str
    # Includes bearer credential, original goal and bill file. Trusted memory only.
    argv: tuple[str, ...] = field(repr=False)
    cwd: str = field(repr=False)
    execution_authorized: bool = False
    payment_executed: bool = False


def _validate_snapshot(snapshot):
    if type(snapshot) is not PurchaseRequestSnapshot:
        raise MarketError('original_request_required','需要首次资源请求的内存快照；不能以handoff补造付款。')
    try:
        url=snapshot.url
        if not isinstance(url,str) or any(ord(c)<=32 or ord(c)==127 for c in url):raise ValueError
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path!='/v1/purchases/resource'
                or parsed.query or parsed.fragment or parsed.port==0):raise ValueError
        host=parsed.hostname.lower()
        if host=='localhost' or host.endswith(('.localhost','.local')):raise ValueError
        try:address=ipaddress.ip_address(host)
        except ValueError:address=None
        if address is not None and not address.is_global:raise ValueError
        if snapshot.method!='POST' or type(snapshot.body) is not bytes or not 0<len(snapshot.body)<=16384:
            raise ValueError
        if type(snapshot.headers) is not tuple or not snapshot.headers:raise ValueError
        headers={}
        for pair in snapshot.headers:
            if type(pair) is not tuple or len(pair)!=2:raise ValueError
            name,value=pair
            if (not isinstance(name,str) or not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+',name)
                    or not isinstance(value,str) or any(ord(c)<32 or ord(c)==127 for c in value)
                    or name.lower() in headers):raise ValueError
            headers[name.lower()]=value
        if ('payment-proof' in headers or headers.get('content-type')!='application/json'
                or headers.get('accept')!='application/zip'):raise ValueError
        authorization=headers.get('authorization','')
        if not re.fullmatch(r'Bearer ak_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}',authorization):raise ValueError
        if authorization.removeprefix('Bearer ').split('.')[0]!=snapshot.key_id:raise ValueError
        for key in ('task_id','order_id','request_id','resource_id','version','key_id'):
            identifier(getattr(snapshot,key),key)
        if snapshot.purpose not in ('personal_use','commercial_use'):raise ValueError
        digest(snapshot.release_digest)
        expected={'id':snapshot.resource_id,'version':snapshot.version,'request_id':snapshot.request_id,
                  'expected_release_digest':snapshot.release_digest}
        if parse_json(snapshot.body)!=expected:raise ValueError
        # UTF-8 round-trip is required for the CLI's string -d argument. No JSON rewriting.
        body=snapshot.body.decode('utf-8')
        if '\x00' in body or body.encode('utf-8')!=snapshot.body:raise ValueError
        if (type(snapshot.payment_needed) is not bytes
                or not re.fullmatch(rb'[A-Za-z0-9+/_=-]{1,16384}',snapshot.payment_needed)):raise ValueError
        return body
    except (ValueError,TypeError,AttributeError,UnicodeError):
        raise MarketError('invalid_original_request','原请求地址、正文、头或绑定不符合首次采购交接条件。') from None


def prepare_alipay_agent(snapshot, *, user_goal, session_id, directory,
                         bill_basename='402_needed.txt'):
    """Prepare the official 402-buyer-pay arguments without executing them.

    Preparation is not an idempotent payment executor. The caller must not run
    these arguments until CLI integrity/version, ZIP/header handling, redirects,
    original-order authorization and one-time execution have been verified.
    """
    body=_validate_snapshot(snapshot)
    if (not isinstance(user_goal,str) or not user_goal.strip() or len(user_goal)>4000
            or any((ord(c)<32 and c not in '\r\n\t') or ord(c)==127 for c in user_goal)
            or user_goal.strip() in ('未明确','未知','unknown')):
        raise MarketError('original_goal_required','需要触发本次采购的原始用户目标。')
    identifier(session_id,'session_id')
    if (not isinstance(bill_basename,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',bill_basename)
            or '..' in bill_basename or bill_basename.endswith('.')
            or bill_basename.lower()=='manifest.json'
            or bill_basename.split('.')[0].upper() in {'CON','PRN','AUX','NUL',
                                                     *('COM'+str(i) for i in range(1,10)),
                                                     *('LPT'+str(i) for i in range(1,10))}):
        raise MarketError('invalid_bill_filename','账单文件需要安全且不冲突的本地文件名。')
    folder=_safe_path(directory)
    if not folder.parent.is_dir():
        raise MarketError('private_parent_required','需要宿主预先准备的私有父目录。')
    argv=['402-buyer-pay','--session-id',session_id,'-f',bill_basename,'-r',snapshot.url,
          '--intent-summary','原始请求：'+user_goal,'-m',snapshot.method,'-d',body]
    for name,value in snapshot.headers:argv.extend(('-H',name+':'+value))
    binding={key:getattr(snapshot,key) for key in ('task_id','purpose','order_id','request_id',
                                                  'release_digest','resource_id','version','key_id')}
    manifest={'format':'alipay-agent-preparation-v1','status':'prepared_not_executed',
              'binding':binding,'session_id_sha256':sha(session_id.encode()),
              'url_sha256':sha(snapshot.url.encode()),'method':snapshot.method,
              'body_sha256':sha(snapshot.body),'headers_sha256':sha(compact(snapshot.headers).encode()),
              'payment_needed_sha256':sha(snapshot.payment_needed),'bill_basename':bill_basename,
              'user_goal_sha256':sha(user_goal.encode()),'execution_authorized':False,
              'payment_executed':False,'cli_version_verified':False,'cli_integrity_verified':False,
              'redirect_policy_verified':False,'zip_delivery_verified':False,
              'next_action':'trusted_host_verify_cli_contract_and_original_order_authorization'}
    try:
        folder.mkdir(mode=0o700,exist_ok=False)
    except FileExistsError:
        raise MarketError('output_conflict','准备目录已存在；保留原会话，不覆盖或另起付款。') from None
    except OSError:
        raise MarketError('preparation_not_saved','无法建立准备目录；未执行付款。') from None
    try:
        with _safe_path(folder/bill_basename).open('xb') as handle:handle.write(snapshot.payment_needed)
        with _safe_path(folder/'manifest.json').open('xb') as handle:handle.write(compact(manifest).encode())
    except OSError:
        raise MarketError('preparation_not_saved','保存中断；保留本目录供核查，未执行付款。') from None
    return AlipayAgentPreparation('prepared_not_executed',str(folder/'manifest.json'),tuple(argv),str(folder))


@dataclass(frozen=True)
class AlipayAgentExecution:
    status: str
    record_path: str
    returncode: int | None
    timed_out: bool
    output_limited: bool
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    payment_status: str = 'unknown'
    resource_received: bool = False


def _evidence_ref(value):
    text(value,'evidence_ref',512)
    if value.strip().lower() in ('unknown','pending','false','true','not_verified','未验证','待验证'):
        raise MarketError('authorization_required','需要可信宿主的具体证据引用，不接受布尔开关或待验证标记。')


def _execution_authorization(snapshot,user_goal,executable,authorization):
    """Validate an explicit host assertion, not a decoded/inferred bill price.

    The host must verify its quote, recipient and actual permission against this
    exact bill hash. Evidence references are assertions by that trusted host;
    this function does not read them or certify CLI compatibility itself.
    """
    fields(authorization,('url','task_id','purpose','order_id','request_id','key_id','resource_id','version',
        'release_digest','bill_sha256','user_goal_sha256','amount_cents','currency','payee','expires_at',
        'authorization_ref','quote_ref','executable_path','compatibility'))
    for key in ('url','task_id','purpose','order_id','request_id','key_id','resource_id','version','release_digest'):
        if authorization[key]!=getattr(snapshot,key):
            raise MarketError('authorization_scope_mismatch','付款授权与原订单、资源或任务绑定不一致。')
    if (authorization['bill_sha256']!=sha(snapshot.payment_needed)
            or not isinstance(user_goal,str) or authorization['user_goal_sha256']!=sha(user_goal.encode('utf-8'))):
        raise MarketError('authorization_scope_mismatch','付款授权与原账单或用户目标摘要不一致。')
    integer(authorization['amount_cents'],1,1_000_000_000,'amount_cents')
    if authorization['currency']!='CNY':raise MarketError('authorization_required','金额必须为明确人民币整数分。')
    text(authorization['payee'],'payee',200)
    integer(authorization['expires_at'],1,9_999_999_999,'expires_at')
    if authorization['expires_at']<=time.time():raise MarketError('authorization_expired','付款授权已到期。')
    for key in ('authorization_ref','quote_ref'):_evidence_ref(authorization[key])
    expected_path=_safe_path(executable)
    if (not isinstance(authorization['executable_path'],str)
            or authorization['executable_path']!=str(expected_path)):
        raise MarketError('executable_mismatch','执行文件路径未按当前绝对路径获得授权。')
    compatibility=authorization['compatibility']
    fields(compatibility,('cli_version','cli_sha256','redirect_policy_evidence_ref',
                         'authorization_forwarding_evidence_ref','zip_delivery_evidence_ref'))
    if compatibility['cli_version']!=CLI_VERSION or compatibility['cli_sha256']!=CLI_SHA256:
        raise MarketError('cli_compatibility_required','兼容证据未绑定固定官方版本及摘要。')
    for key in ('redirect_policy_evidence_ref','authorization_forwarding_evidence_ref','zip_delivery_evidence_ref'):
        _evidence_ref(compatibility[key])
    return expected_path


def _verify_executable(path):
    """Fixed official Windows executable only; there is no caller hash override."""
    try:
        path=_safe_path(path)
        if path.suffix.lower()!='.exe' or not path.is_file():raise ValueError
        before=path.stat()
        if not 2<=before.st_size<=268_435_456:raise ValueError
        with path.open('rb') as handle:
            if handle.read(2)!=b'MZ':raise ValueError
            handle.seek(0);hasher=hashlib.sha256()
            while chunk:=handle.read(1_048_576):hasher.update(chunk)
        after=path.stat()
        identity=lambda stat:(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)
        if identity(before)!=identity(after) or hasher.hexdigest()!=CLI_SHA256:raise ValueError
        return identity(after)
    except (OSError,ValueError):
        raise MarketError('unverified_executable','官方固定版本执行文件未通过路径、格式与摘要核验。') from None


def _exclusive_json(path,value):
    with _safe_path(path).open('xb') as handle:
        handle.write(compact(value).encode('utf-8'));handle.flush();os.fsync(handle.fileno())


def _capture_process(process,timeout,limit):
    """Bound both pipes while the process runs; never use unbounded communicate."""
    events=queue.Queue(maxsize=8);stop=threading.Event();streams=(process.stdout,process.stderr)
    def read(index,stream):
        try:
            while not stop.is_set():
                chunk=stream.read(4096)
                while not stop.is_set():
                    try:events.put((index,chunk),timeout=.05);break
                    except queue.Full:pass
                if not chunk:return
        except Exception:
            while not stop.is_set():
                try:events.put((index,None),timeout=.05);return
                except queue.Full:pass
    readers=[threading.Thread(target=read,args=(i,stream),daemon=True) for i,stream in enumerate(streams)]
    for reader in readers:reader.start()
    buffers=[bytearray(),bytearray()];finished=set();total=0;timed_out=False;limited=False;read_error=False
    deadline=time.monotonic()+timeout
    try:
        while len(finished)<2 or process.poll() is None:
            remaining=deadline-time.monotonic()
            if remaining<=0:timed_out=True;break
            try:index,chunk=events.get(timeout=min(.05,remaining))
            except queue.Empty:continue
            if chunk is None:read_error=True;finished.add(index);break
            if not chunk:finished.add(index);continue
            available=limit-total
            buffers[index].extend(chunk[:available]);total+=min(len(chunk),available)
            if len(chunk)>available:limited=True;break
        if timed_out or limited or read_error:
            process.kill()
        try:returncode=process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill();returncode=None;read_error=True
        return returncode,timed_out,limited,read_error,bytes(buffers[0]),bytes(buffers[1])
    finally:
        stop.set()
        for reader in readers:reader.join(timeout=.5)
        for index,stream in enumerate(streams):
            if not readers[index].is_alive():stream.close()


def execute_alipay_agent(snapshot, *, user_goal, session_id, directory, execution_root,
                         executable, authorization, timeout=60, allowed_env=None,
                         max_output_bytes=262144):
    """Explicit trusted-host payment invocation; never call from a model tool.

    No project default authorization/compatibility is configured. The caller
    supplies reviewed evidence, exact price/payee and the bill digest. An exit
    code or CLI text never proves payment or resource delivery. Every claimed
    endpoint/order stays spent for execution purposes, including unknown/crash.
    """
    _validate_snapshot(snapshot)
    if type(timeout) not in (int,float) or not 0<timeout<=60:
        raise MarketError('invalid_params','执行超时必须大于0且不超过60秒。')
    integer(max_output_bytes,1,1_048_576,'max_output_bytes')
    if allowed_env is None:allowed_env={}
    if type(allowed_env) is not dict or set(allowed_env)-{'AIPAY_MODEL','AIPAY_OUTPUT_CHANNEL'}:
        raise MarketError('invalid_environment','只允许显式的官方模型与来源渠道环境变量。')
    allowed_env=dict(allowed_env)
    for value in allowed_env.values():text(value,'environment_value',128)
    executable_path=_execution_authorization(snapshot,user_goal,executable,authorization)
    executable_identity=_verify_executable(executable_path)
    root=_safe_path(execution_root)
    if not root.is_dir():raise MarketError('execution_root_required','需要预先准备的统一私有执行记录目录。')
    # Normalize the origin so session, output directory, host case or :443 cannot
    # provide another payment slot for the same original market order.
    parsed=urlsplit(snapshot.url)
    origin=[parsed.scheme.lower(),parsed.hostname.lower(),parsed.port or 443]
    execution_id=sha(compact([origin,snapshot.order_id]).encode())
    claim_path=root/(execution_id+'.json');record_path=root/(execution_id+'.result.json')
    claim={'format':'alipay-agent-execution-v1','status':'claimed_before_start','payment_status':'unknown',
           'order_id':snapshot.order_id,'task_id':snapshot.task_id,'request_id':snapshot.request_id,
           'key_id':snapshot.key_id,'url_sha256':sha(snapshot.url.encode()),
           'bill_sha256':sha(snapshot.payment_needed),'user_goal_sha256':sha(user_goal.encode()),
           'authorization_sha256':sha(compact(authorization).encode()),
           'cli_version':CLI_VERSION,'cli_sha256':CLI_SHA256,'created_at':int(time.time())}
    try:_exclusive_json(claim_path,claim)
    except FileExistsError:
        raise MarketError('payment_execution_already_claimed','原订单已有执行意图；不重复付款，先核查原记录。') from None
    except OSError:
        raise MarketError('execution_not_recorded','执行意图未完整保存；未启动付款，保留原记录核查。') from None
    process=None;output=b'';errors=b'';returncode=None;timed_out=False;limited=False;uncertain=True
    status='not_started'
    try:
        prepared=prepare_alipay_agent(snapshot,user_goal=user_goal,session_id=session_id,directory=directory)
        # Revalidate immediately before process creation; never accept a stored argv.
        _execution_authorization(snapshot,user_goal,executable_path,authorization)
        if _verify_executable(executable_path)!=executable_identity:
            raise MarketError('executable_changed','执行文件在准备期间发生变化，已停止。')
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0
        process=subprocess.Popen([str(executable_path),*prepared.argv],cwd=prepared.cwd,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            shell=False,env=dict(allowed_env),creationflags=flags,bufsize=0)
        returncode,timed_out,limited,uncertain,output,errors=_capture_process(process,timeout,max_output_bytes)
        status='execution_unknown' if timed_out or limited or uncertain else 'execution_finished_payment_unverified'
    except Exception:
        # Do not propagate exceptions that can include the secret argv, headers or CLI output.
        status='execution_unknown' if process is not None else 'not_started'
        if process is not None:
            try:process.kill();process.wait(timeout=1)
            except (OSError,subprocess.SubprocessError):pass
    result={'format':'alipay-agent-execution-result-v1','status':status,'returncode':returncode,
            'timed_out':timed_out,'output_limited':limited,'output_read_unknown':uncertain,
            'stdout_sha256':sha(output),'stderr_sha256':sha(errors),
            'stdout_bytes':len(output),'stderr_bytes':len(errors),'payment_status':'unknown',
            'resource_received':False,'finished_at':int(time.time()),'retry_allowed':False}
    try:_exclusive_json(record_path,result)
    except OSError:
        raise MarketError('execution_result_not_saved','执行结果记录中断；原订单意图仍占用，禁止再次付款。') from None
    return AlipayAgentExecution(status,str(record_path),returncode,timed_out,limited,output,errors)
