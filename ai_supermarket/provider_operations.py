"""本项目原创。供应商原操作的持久防重与查询，独立于市场付款和次数账本。"""
from contextlib import contextmanager
import sqlite3
import time

from .api_provider import provider_response, verify_provider_request
from .api_quota import MAX_INPUT, MAX_RESULT, canonical, checksum
from .common import MarketError, identifier, parse_json
from .origin import _safe_path

FORMAT='market-provider-operations-v1'


class ProviderOperations:
    """One immutable provider/connection per local ledger, shared by its workers.

    authorize(request) is provider policy, not a payment assertion. execute and
    lookup are fixed backend functions. Backend IDs must be scoped to this same
    connection; no callback, input, URL or credential comes from a model tool.
    An interrupted reservation never automatically dispatches again.
    """
    def __init__(self,path,provider,binding_id,key,authorize,execute,lookup=None,*,clock=None,require_existing=False):
        self.path=_safe_path(path)
        self.provider=identifier(provider,'provider');self.binding_id=identifier(binding_id,'binding_id')
        if (not isinstance(key,bytes) or not 32<=len(key)<=4096 or not callable(authorize)
                or not callable(execute) or (lookup is not None and not callable(lookup)) or type(require_existing) is not bool):
            raise MarketError('invalid_configuration','供应商需要独立凭据、授权器及固定执行器。')
        self._key=key;self.authorize=authorize;self.execute=execute;self.lookup=lookup
        self.clock=clock or time.time
        with self._db(create=not require_existing) as db:
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables!={'configuration','operations'}:
                raise MarketError('ledger_mismatch','需要独立操作账本，不迁移其他数据库。')
            if not tables:
                if require_existing:
                    raise MarketError('ledger_mismatch','原操作账本缺少表，不能重新初始化。')
                db.execute('CREATE TABLE configuration (format TEXT NOT NULL,provider TEXT NOT NULL,binding_id TEXT NOT NULL)')
                db.execute('INSERT INTO configuration VALUES (?,?,?)',(FORMAT,self.provider,self.binding_id))
                db.execute('''CREATE TABLE operations (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','completed','not_executed')),
                    result TEXT, result_sha256 TEXT, evidence_ref TEXT)''')
            self._binding(db)

    @contextmanager
    def _db(self,*,create=False):
        _safe_path(self.path)
        try:
            if not create:
                with self.path.open('rb') as original:
                    if original.read(16)!=b'SQLite format 3\x00':
                        raise MarketError('ledger_unavailable','原操作账本为空或不可识别，停止派发。')
            db=sqlite3.connect(self.path.as_uri()+('?mode=rwc' if create else '?mode=rw'),
                               uri=True,timeout=5,isolation_level=None)
        except (OSError,sqlite3.Error):
            raise MarketError('ledger_unavailable','原操作账本不可用，保留记录核查，不重建。') from None
        db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def _binding(self,db):
        rows=[tuple(row) for row in db.execute('SELECT format,provider,binding_id FROM configuration')]
        if rows!=[(FORMAT,self.provider,self.binding_id)]:
            raise MarketError('ledger_mismatch','供应商操作账本属于其他连接。')

    def _authorized(self,raw,signature):
        return verify_provider_request(raw,signature,self._key,self.provider,self.binding_id,
                                       self.authorize,now=self.clock())

    @staticmethod
    def _stable(request):
        return canonical({k:v for k,v in request.items() if k not in ('issued_at','expires_at')},MAX_INPUT+4096)

    def _row(self,db,operation_id):
        self._binding(db)
        row=db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
        if row is not None and checksum(row['request'])!=row['fingerprint']:
            raise MarketError('integrity_failed','供应商原请求记录损坏，不能重新执行。')
        return row

    def _reserve(self,request,fingerprint):
        raw=self._stable(request)
        with self._db() as db:
            row=self._row(db,request['operation_id'])
            if row is not None:
                if row['fingerprint']!=fingerprint or row['request']!=raw:
                    raise MarketError('idempotency_conflict','原操作编号已绑定其他输入。')
                return self._evidence(row,False),False
            db.execute('INSERT INTO operations VALUES (?,?,?,?,NULL,NULL,NULL)',
                       (request['operation_id'],raw,fingerprint,'reserved'))
            return {'state':'unknown'},True

    @staticmethod
    def _evidence(row,lookup):
        if row is None or row['state']=='reserved':return {'state':'unknown'}
        if row['state']=='completed':
            if not isinstance(row['result'],str) or checksum(row['result'])!=row['result_sha256']:
                raise MarketError('integrity_failed','供应商结果记录损坏，不能重新执行。')
            canonical(parse_json(row['result']),MAX_RESULT);identifier(row['evidence_ref'],'evidence_ref')
            return {'state':'completed','result':parse_json(row['result']),'evidence_ref':row['evidence_ref']}
        if row['state']=='not_executed':
            identifier(row['evidence_ref'],'evidence_ref')
            # Only a lookup response can certify fenced nonexecution on the wire.
            return ({'state':'not_executed','evidence_ref':row['evidence_ref'],'fenced':True}
                    if lookup else {'state':'unknown'})
        raise MarketError('integrity_failed','供应商原操作状态无效。')

    def _settle(self,operation_id,evidence):
        state=evidence['state'];result=canonical(evidence['result'],MAX_RESULT) if state=='completed' else None
        with self._db() as db:
            row=self._row(db,operation_id)
            if row is None:raise MarketError('not_found','没有原预留操作，不接受凭空完成。')
            if row['state']!='reserved':
                if row['state']!=state or row['result']!=result:
                    raise MarketError('settlement_conflict','原操作已有不一致的终态，保留原证据。')
                return self._evidence(row,True)
            db.execute('UPDATE operations SET state=?,result=?,result_sha256=?,evidence_ref=? WHERE id=?',
                (state,result,checksum(result) if result is not None else None,evidence['evidence_ref'],operation_id))
            return self._evidence(self._row(db,operation_id),True)

    def handle(self,raw,signature):
        """Return response bytes/signature for an authenticated bounded HTTP ingress.

        Caller sets HTTP 200/JSON headers. Errors are handled by its existing HTTP
        framework without logging raw requests. No background runner or retry.
        """
        request,fingerprint=self._authorized(raw,signature)
        if request['action']=='execute':
            evidence,fresh=self._reserve(request,fingerprint)
            if fresh:
                self._authorized(raw,signature)
                try:
                    result=self.execute(request['operation_id'],request['operation'],
                                        parse_json(canonical(request['input'],MAX_INPUT)))
                    evidence={'state':'completed','result':result,'evidence_ref':'executor-returned'}
                    # Invalid/oversized results leave the durable reservation unknown.
                    provider_response(raw,self._key,evidence)
                except Exception:
                    evidence={'state':'unknown'}
                self._authorized(raw,signature)
                if evidence['state']=='completed':evidence=self._settle(request['operation_id'],evidence)
        else:
            with self._db() as db:
                row=self._row(db,request['operation_id']);evidence=self._evidence(row,True)
                exists=row is not None
            if exists and evidence['state']=='unknown' and self.lookup is not None:
                self._authorized(raw,signature)
                try:
                    evidence=self.lookup(request['operation_id'])
                    provider_response(raw,self._key,evidence)
                except Exception:
                    evidence={'state':'unknown'}
                self._authorized(raw,signature)
                if evidence['state']!='unknown':evidence=self._settle(request['operation_id'],evidence)
        self._authorized(raw,signature)
        return provider_response(raw,self._key,evidence)
