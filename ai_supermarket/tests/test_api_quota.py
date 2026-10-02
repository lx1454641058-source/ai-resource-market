"""本项目原创。隔离供应商权益、次数计量与真实本机 API 调用，不含真实付款。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from ai_supermarket.api_quota import ProviderQuota, MAX_RESULT
from ai_supermarket.common import MarketError


class QuotaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'provider.sqlite'
        self.request = {'acquisition_id': 'acquisition-1', 'resource_id': 'vendor:stock',
                        'version': '1.0.0', 'purpose': 'personal_use', 'release_digest': 'a'*64,
                        'task_id': 'task-1', 'request_id': 'request-1', 'operation': 'stock',
                        'input': {'warehouse': 'fictional'}}
        self.grant = {key: value for key, value in self.request.items()
                      if key not in ('task_id', 'request_id', 'input')}
        self.grant.update(provider='vendor', principal='buyer', total_units=2, expires_at=200)
        self.allowed = True
        self.now = 100
        self.executions = []
        self.quota = self.open()

    def authorize(self, principal, request):
        if not self.allowed or principal != 'buyer' or request['task_id'] not in ('task-1', 'task-2'):
            raise MarketError('forbidden', 'fixture authorization denied')
        if request['input'] != {'warehouse': 'fictional'}:
            raise MarketError('invalid_params', 'fixture input schema rejected')
        return deepcopy(self.grant)

    def open(self):
        return ProviderQuota(self.path, 'vendor', self.authorize, clock=lambda: self.now)

    def execute(self, operation_id, operation, value):
        self.executions.append(operation_id)
        return {'count': 7, 'source': 'original fictional fixture'}

    def invoke(self, request=None, execute=None):
        return self.quota.invoke('buyer', request or self.request, execute or self.execute)

    def error(self, code, function, *args):
        with self.assertRaises(MarketError) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def test_success_replay_restart_and_canonical_input(self):
        result = self.invoke()
        self.assertEqual(('completed', 1, 0, 1),
                         tuple(result[k] for k in ('status','used_units','reserved_units','remaining_units')))
        self.quota = self.open()
        self.assertEqual(result, self.invoke(dict(reversed(list(self.request.items())))))
        self.assertEqual(1, len(self.executions))
        self.assertFalse(result['payment_executed']); self.assertFalse(result['task_completed'])

    def test_runtime_missing_quota_never_creates_an_empty_replacement(self):
        self.invoke(); retained = self.path.with_name('retained.sqlite'); self.path.rename(retained)
        self.error('ledger_unavailable', self.invoke)
        self.assertFalse(self.path.exists()); self.assertEqual(1, len(self.executions))
        retained.rename(self.path)
        self.assertEqual(1, self.invoke()['used_units']); self.assertEqual(1, len(self.executions))

    def test_explicit_reopen_rejects_missing_empty_and_tableless_ledgers(self):
        path = self.path.with_name('missing.sqlite')
        def reopen(): return ProviderQuota(path, 'vendor', self.authorize, clock=lambda: self.now, require_existing=True)
        self.error('ledger_unavailable', reopen); self.assertFalse(path.exists())
        path.write_bytes(b''); self.error('ledger_unavailable', reopen); self.assertEqual(b'', path.read_bytes())
        with closing(sqlite3.connect(path)) as db: db.execute('VACUUM')
        raw = path.read_bytes(); self.error('ledger_mismatch', reopen); self.assertEqual(raw, path.read_bytes())

    def test_runtime_loss_between_file_check_and_sqlite_open_cannot_recreate(self):
        from unittest.mock import patch
        connect = sqlite3.connect
        def remove_before_open(path, *args, **kwargs):
            self.assertTrue(str(path).endswith('?mode=rw')); self.assertTrue(kwargs['uri'])
            self.path.rename(self.path.with_name('retained-race.sqlite'))
            return connect(path, *args, **kwargs)
        with patch('ai_supermarket.api_quota.sqlite3.connect', side_effect=remove_before_open):
            self.error('ledger_unavailable', self.invoke)
        self.assertFalse(self.path.exists()); self.assertEqual([], self.executions)

    def test_loss_after_dispatch_keeps_original_reservation_for_restoration(self):
        retained = self.path.with_name('retained-after-dispatch.sqlite')
        def execute(*args):
            result = self.execute(*args); self.path.rename(retained); return result
        self.error('ledger_unavailable', self.invoke, None, execute)
        self.assertFalse(self.path.exists()); self.assertEqual(1, len(self.executions))
        retained.rename(self.path)
        self.assertEqual('reserved', self.invoke()['status'])
        self.assertEqual(1, len(self.executions))

    def test_quota_shared_across_tasks_and_not_reset_by_reopening(self):
        self.invoke()
        self.invoke({**self.request, 'task_id':'task-2'})
        self.quota = self.open()
        self.error('quota_exhausted', self.invoke, {**self.request,'request_id':'third'})
        self.assertEqual(2, len(self.executions))

    def test_unknown_holds_quota_and_does_not_auto_execute_again(self):
        def timeout(*args):
            self.executions.append('unknown')
            raise TimeoutError('SECRET must not be returned')
        result = self.invoke(execute=timeout)
        self.assertEqual(('reserved',1,1), tuple(result[k] for k in ('status','reserved_units','remaining_units')))
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(result, self.invoke())
        self.assertEqual(['unknown'], self.executions)
        self.assertEqual(result, self.quota.recover('buyer', self.request, lambda _: {'state':'unknown'}))
        self.error('invalid_evidence', self.quota.recover, 'buyer', self.request, lambda _: {'state':'not_found'})

    def test_recover_completed_original_result_without_redispatch(self):
        self.invoke(execute=lambda *a: (_ for _ in ()).throw(TimeoutError()))
        recovered = self.quota.recover('buyer', self.request, lambda _: {
            'state':'completed','result':{'count':7},'evidence_ref':'provider-original-1'})
        self.assertEqual('completed', recovered['status'])
        self.assertEqual(1, recovered['used_units'])
        self.assertEqual(recovered, self.invoke())
        self.assertEqual([], self.executions)

    def test_definitive_not_executed_releases_but_original_request_is_terminal(self):
        self.grant['total_units'] = 1
        self.invoke(execute=lambda *a: (_ for _ in ()).throw(TimeoutError()))
        self.error('quota_exhausted', self.invoke, {**self.request,'request_id':'second'})
        result = self.quota.recover('buyer', self.request,
            lambda _: {'state':'not_executed','evidence_ref':'supplier-fenced-operation'})
        self.assertEqual(('not_executed',1), (result['status'],result['remaining_units']))
        self.assertEqual('not_executed', self.invoke()['status'])
        self.invoke({**self.request,'request_id':'second'})
        self.assertEqual(1,len(self.executions))

    def test_changed_request_and_acquisition_cannot_reuse_id(self):
        self.invoke()
        changed = {**self.request,'operation':'different'}
        self.grant['operation'] = 'different'
        self.error('grant_changed', self.invoke, changed)
        self.grant['operation'] = 'stock'
        self.grant['acquisition_id'] = 'acquisition-2'
        self.error('idempotency_conflict', self.invoke, {**self.request,'acquisition_id':'acquisition-2'})
        self.assertEqual(1,len(self.executions))

    def test_authorization_revoked_or_expired_blocks_cached_results(self):
        self.invoke()
        self.allowed = False
        self.error('forbidden', self.invoke)
        self.error('forbidden', self.quota.status, 'buyer', self.request)
        self.allowed = True; self.now = 200
        self.error('entitlement_expired', self.invoke)
        self.error('forbidden', self.quota.invoke, 'other-buyer', self.request, self.execute)

    def test_post_execution_revocation_preserves_hold_and_hides_result(self):
        def revoke(*args):
            self.allowed = False
            return {'private': 'result'}
        self.error('forbidden', self.invoke, None, revoke)
        self.allowed = True
        self.assertEqual('reserved', self.quota.status('buyer',self.request)['status'])
        self.assertEqual('reserved',self.invoke()['status'])

    def test_limits_cannot_be_increased_on_original_grant(self):
        self.invoke()
        self.grant['total_units'] = 100
        self.error('grant_changed', self.invoke, {**self.request,'request_id':'more'})

    def test_invalid_input_and_mismatched_grant_never_execute(self):
        for changed in ({'input':{'warehouse':'private'}}, {'total_units':999},
                        {'release_digest':'b'*64}, {'purpose':'commercial_use'}, {'task_id':'other-task'}):
            with self.subTest(changed=changed), self.assertRaises(MarketError):
                self.invoke({**self.request, **changed})
        self.assertEqual([],self.executions)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM calls').fetchone()[0])

    def test_status_never_reserves_and_wrong_provider_database_refused(self):
        self.error('not_found', self.quota.status, 'buyer', self.request)
        self.error('ledger_mismatch', ProviderQuota, self.path, 'other-provider', self.authorize)
        self.assertEqual(1,self.invoke()['remaining_units'])

    def test_wrong_database_is_not_migrated(self):
        path = self.path.with_name('existing.sqlite')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE valuable (value TEXT)')
            db.execute("INSERT INTO valuable VALUES ('keep')")
        self.error('ledger_mismatch', ProviderQuota, path, 'vendor', self.authorize)
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual([('valuable',)],db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            self.assertEqual('keep',db.execute('SELECT value FROM valuable').fetchone()[0])

    def test_invalid_or_oversized_result_keeps_original_reservation(self):
        self.error('payload_too_large', self.invoke, None, lambda *a: {'large':'x'*MAX_RESULT})
        self.assertEqual('reserved',self.invoke()['status'])
        self.assertEqual([],self.executions)

    def test_result_corruption_does_not_trigger_execution(self):
        self.invoke()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE calls SET result='{}'")
        self.error('integrity_failed', self.invoke)
        self.assertEqual(1,len(self.executions))

    def test_concurrent_same_request_dispatches_once(self):
        started = threading.Event(); release = threading.Event()
        def slow(*args):
            self.executions.append('one'); started.set()
            if not release.wait(5): raise TimeoutError()
            return {'count':7}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.invoke,None,slow)
            self.assertTrue(started.wait(2))
            try:
                self.assertEqual('reserved', self.open().invoke('buyer',self.request,self.execute)['status'])
            finally:
                release.set()
            self.assertEqual('completed',first.result(5)['status'])
        self.assertEqual(['one'],self.executions)

    def test_concurrent_distinct_requests_cannot_overspend_last_unit(self):
        self.grant['total_units'] = 1
        barrier = threading.Barrier(2)
        def attempt(index):
            client=self.open(); barrier.wait(3)
            try:
                return client.invoke('buyer',{**self.request,'request_id':f'call-{index}'},self.execute)['status']
            except MarketError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(attempt, range(2)))
        self.assertEqual(['completed','quota_exhausted'],sorted(result))
        self.assertEqual(1,len(self.executions))

    def test_executor_cannot_mutate_bound_input(self):
        def mutate(_id, _operation, value):
            value['warehouse'] = 'changed by executor'
            return {'count':7}
        self.assertEqual('completed',self.invoke(execute=mutate)['status'])
        self.assertEqual({'warehouse':'fictional'},self.request['input'])
        self.assertEqual('completed',self.invoke()['status'])

    def test_real_loopback_api_lost_response_recovers_original_output(self):
        outputs = {}; calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(data['operation_id'])
                outputs[data['operation_id']] = {'count':7,'warehouse':data['input']['warehouse']}
                # Work completed; deliberately close without a response.
                self.close_connection = True
            def do_GET(self):
                result = {'state':'completed','result':outputs[self.path[1:]],'evidence_ref':'original-api-record'}
                raw = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers()
                self.wfile.write(raw)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(thread.join,2); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        def execute(operation_id,operation,value):
            data=json.dumps({'operation_id':operation_id,'operation':operation,'input':value}).encode()
            with urlopen(Request(base,data=data,method='POST'),timeout=2) as response:
                return json.load(response)
        def lookup(operation_id):
            with urlopen(base+'/'+operation_id,timeout=2) as response:
                return json.load(response)
        self.assertEqual('reserved',self.invoke(execute=execute)['status'])
        self.quota=self.open()
        result=self.quota.recover('buyer',self.request,lookup)
        self.assertEqual({'count':7,'warehouse':'fictional'},result['result'])
        self.assertEqual(1,result['used_units'])
        self.assertEqual(result,self.invoke(execute=execute))
        self.assertEqual(1,len(calls))


if __name__ == '__main__': unittest.main()
