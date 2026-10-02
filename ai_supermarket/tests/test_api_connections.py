"""本项目原创。通用市场/供应商配置接线，隔离HTTP与成果回执，无真实账户。"""
from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ai_supermarket.api_connections import FORMAT, _app, attach, load, prepare
from ai_supermarket.api_provider import ApiProviderClient
from ai_supermarket.common import MarketError, compact
from ai_supermarket import provider_service
from ai_supermarket.tests import test_provider_pair as pair_fixture
from ai_supermarket.tests import test_provider_json as backend_fixture
from tools import market_api_try as trial

ROOT = Path(__file__).resolve().parents[2]


class ApiConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.f = pair_fixture.ProviderPairTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.root; self.market = self.f.market
        self.directory = self.root / 'connections'
        self.marker = json.loads((self.market / 'trial.json').read_text(encoding='utf-8'))
        self.plan = {'format': FORMAT, 'database': str(self.market / 'market.sqlite'),
            'identities': str(self.market / 'identities.sqlite'), 'budget': str(self.market / 'budget.sqlite'),
            'providers': [{'provider': trial.PROVIDER, 'binding_id': trial.BINDING,
                'endpoint': f'http://127.0.0.1:{self.f.ports[1]}/api',
                'connection_key_file': str(self.market / 'provider-key.json'), 'operations': ['stock'],
                'tasks': [{'key_id': self.marker['key_id'], 'principal': trial.BUYER, 'root_id': 'api-trial-root',
                           'task_id': trial.TASK, 'purpose': 'personal_use'}]}]}
        self.config = self.root / 'plan.json'
        self.write_plan()

    def write_plan(self):
        self.config.write_text(compact(self.plan), encoding='utf-8')

    def error(self, code, function, *args):
        with self.assertRaises(MarketError) as caught: function(*args)
        self.assertEqual(code, caught.exception.code)

    def configured(self):
        config, _ = load(self.directory)
        app = _app(config); attach(app, self.directory)
        return app

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'ai_supermarket.api_connections', *args],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
        private = json.loads((self.market / 'provider-key.json').read_text())['key_hex']
        self.assertNotIn(private, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_prepare_check_cli_no_network_or_original_database_changes(self):
        paths = [Path(self.plan[name]) for name in ('database', 'identities', 'budget')]
        original = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.assertEqual('prepared', self.cli('prepare', '--config', str(self.config), '--directory', str(self.directory))['status'])
        self.assertEqual('local_configuration_only', self.cli('check', '--directory', str(self.directory))['scope'])
        self.assertEqual(original, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
        self.assertEqual({'connections.json', 'binding.json', 'provider-0-key.json', 'provider-0-quota.sqlite'},
                         {p.name for p in self.directory.iterdir()})

    def test_mismatched_task_is_not_rebound_or_prepared(self):
        self.plan['providers'][0]['tasks'][0]['task_id'] = 'different-task'; self.write_plan()
        self.error('api_host_mismatch', prepare, self.directory, self.config)
        self.assertFalse(self.directory.exists())

    def test_duplicate_provider_or_task_is_refused_before_output(self):
        original = deepcopy(self.plan)
        for providers in ([original['providers'][0]] * 2, [{**original['providers'][0],
                            'tasks': original['providers'][0]['tasks'] * 2}]):
            self.plan['providers'] = providers; self.write_plan()
            self.error('invalid_configuration', prepare, self.directory, self.config)
            self.assertFalse(self.directory.exists())

    def test_endpoint_and_private_file_validation_precedes_output(self):
        self.plan['providers'][0]['endpoint'] = 'http://external.example.invalid/api'; self.write_plan()
        self.error('invalid_endpoint', prepare, self.directory, self.config)
        self.assertFalse(self.directory.exists())
        self.plan['providers'][0]['endpoint'] = 'https://example.invalid/api'
        bad = self.root / 'bad-key.json'; bad.write_text(compact({'key_hex': 'a' * 65}))
        self.plan['providers'][0]['connection_key_file'] = str(bad); self.write_plan()
        self.error('invalid_configuration', prepare, self.directory, self.config)
        self.assertFalse(self.directory.exists())

    def test_missing_original_market_is_not_initialized(self):
        self.plan['database'] = str(self.root / 'absent.sqlite'); self.write_plan()
        self.error('not_found', prepare, self.directory, self.config)
        self.assertFalse(Path(self.plan['database']).exists()); self.assertFalse(self.directory.exists())

    def test_reprepare_never_recreates_missing_quota(self):
        prepare(self.directory, self.config)
        quota = self.directory / 'provider-0-quota.sqlite'; quota.rename(self.directory / 'original.sqlite')
        self.error('output_exists', prepare, self.directory, self.config)
        self.error('ledger_unavailable', self.configured)
        self.assertFalse(quota.exists())
        quota.write_bytes(b'')
        self.error('ledger_unavailable', self.configured); self.assertEqual(b'', quota.read_bytes())

    def test_startup_and_runtime_changed_private_file_fail_closed(self):
        prepare(self.directory, self.config); app = self.configured()
        host = app.apis.hosts[self.marker['key_id']][trial.PROVIDER]
        path = self.directory / 'provider-0-key.json'; path.write_bytes(b'{}')
        self.error('connection_changed', self.configured)
        self.error('connection_changed', host.authority.task_check, trial.BUYER, trial.TASK, trial.PROVIDER)

    def test_revoked_task_binding_blocks_existing_host(self):
        prepare(self.directory, self.config); app = self.configured()
        host = app.apis.hosts[self.marker['key_id']][trial.PROVIDER]
        app.gateway.revoke_binding(self.marker['key_id'])
        self.error('task_binding_denied', host.authority.task_check, trial.BUYER, trial.TASK, trial.PROVIDER)
        self.error('task_binding_denied', self.configured)

    def test_other_database_and_already_attached_app_are_refused(self):
        prepare(self.directory, self.config); app = self.configured()
        self.error('api_host_mismatch', attach, app, self.directory)
        config, _ = load(self.directory); other = _app(config)
        other.gateway.database = str(self.root / 'other.sqlite')
        self.error('api_host_mismatch', attach, other, self.directory)
        self.assertIsNone(other.apis)

    def test_missing_market_during_attach_does_not_create_replacement(self):
        prepare(self.directory, self.config)
        config, _ = load(self.directory); app = _app(config)
        path = Path(self.plan['database']); path.rename(path.with_suffix('.retained'))
        self.error('not_found', attach, app, self.directory)
        self.assertFalse(path.exists()); self.assertIsNone(app.apis)

    def test_changed_policy_does_not_expand_existing_host(self):
        prepare(self.directory, self.config); app = self.configured()
        host = app.apis.hosts[self.marker['key_id']][trial.PROVIDER]
        with app.gateway.identities.store.connect(write=True) as db:
            row = db.execute('SELECT policy FROM accounts WHERE principal=?', (trial.BUYER,)).fetchone()
            policy = json.loads(row[0]); policy['permissions'] = []
            db.execute('UPDATE accounts SET policy=? WHERE principal=?', (compact(policy), trial.BUYER))
        self.assertFalse(host.authority.task_check(trial.BUYER, trial.TASK, trial.PROVIDER))

    def test_http_cli_requires_budget_enforcement_before_starting(self):
        prepare(self.directory, self.config)
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'ai_supermarket.http_server',
            '--database', str(self.root / 'unused.sqlite'), '--identities', self.plan['identities'],
            '--api-connections', str(self.directory)], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(1, result.returncode); self.assertFalse((self.root / 'unused.sqlite').exists())

    def test_multiple_tasks_share_original_provider_quota(self):
        # Two existing authorized keys/tasks cannot double one acquired package.
        app, _, _ = trial.application(self.market, local_provider=False)
        issued = app.gateway.identities.issue_key(trial.BUYER)
        app.gateway.budgets.grant_task('api-trial-root', 'second-task', trial.BUYER)
        app.gateway.bind_key(issued['key_id'], 'api-trial-root', 'second-task', trial.BUYER, 'second-test-binding')
        self.plan['providers'][0]['tasks'].append({**self.plan['providers'][0]['tasks'][0],
                                                  'key_id': issued['key_id'], 'task_id': 'second-task'})
        self.write_plan(); prepare(self.directory, self.config); app = self.configured()
        first = app.apis.hosts[self.marker['key_id']][trial.PROVIDER]
        second = app.apis.hosts[issued['key_id']][trial.PROVIDER]
        self.assertEqual(first.quota.path, second.quota.path)
        acquired = first.market.acquire('api-trial-seller:stock', '1.0.0', 'shared-acquire',
            purpose='personal_use', expected_release_digest=self.marker['release_digest'])['acquisition_id']
        with patch.object(ApiProviderClient, 'execute', return_value={'count': 7, 'warehouse': 'fictional'}) as execute:
            # Hosts bind the client's bound methods at creation, so reattach under the mock.
            app = self.configured()
            first = app.apis.hosts[self.marker['key_id']][trial.PROVIDER]
            second = app.apis.hosts[issued['key_id']][trial.PROVIDER]
            for i in range(10):
                host = first if i < 5 else second
                self.assertEqual('api_result_delivered', host.use(acquired, {'warehouse': 'fictional'}, f'call-{i}')['status'])
            with self.assertRaises(MarketError): second.use(acquired, {'warehouse': 'fictional'}, 'over-limit')
            self.assertEqual(10, execute.call_count)

    def start(self, args, port):
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', *args], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def stop():
            if process.poll() is None: process.terminate()
            process.communicate(timeout=8)
        self.addCleanup(stop)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode('utf-8'))
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1): return process
            except OSError: time.sleep(.03)
        self.fail('generic configured process did not open port')

    def test_generic_market_and_supplier_cli_deliver_artifact_receipt_and_restart(self):
        backend = backend_fixture.ProviderJsonTests(); backend.setUp(); self.addCleanup(backend.doCleanups)
        prepare(self.directory, self.config)
        old = json.loads((self.f.provider / 'provider.json').read_text(encoding='utf-8'))
        config = {'format': provider_service.FORMAT, 'market_url': old['market_url'],
            'provider': trial.PROVIDER, 'binding_id': trial.BINDING, 'seller': old['seller'], 'key_id': old['key_id'],
            'listen_port': self.f.ports[1], 'release': {k: old[k] for k in ('id', 'version', 'release_digest')},
            'api': trial.sample()['metadata']['api'],
            'backend': {'endpoint': backend.endpoint, 'query_map': {'warehouse': 'location'}, 'fixed_query': {}, 'timeout': 2},
            'result_checks': [{'path': ['count'], 'type': 'integer', 'minimum': 0},
                              {'path': ['warehouse'], 'type': 'string', 'input': 'warehouse'}]}
        source = self.root / 'provider-config.json'; source.write_text(compact(config), encoding='utf-8')
        supplier = self.root / 'generic-supplier'
        provider_service.prepare(supplier, source, self.f.provider / 'seller-credential.json', self.f.provider / 'provider-key.json')
        market_args = ['-m', 'ai_supermarket.http_server', '--database', self.plan['database'],
            '--identities', self.plan['identities'], '--budget', self.plan['budget'], '--enforce-task-budget',
            '--api-connections', str(self.directory), '--port', str(self.f.ports[0])]
        supplier_args = [str(ROOT / 'tools/market_provider_service.py'), 'serve', '--directory', str(supplier)]
        market = self.start(market_args, self.f.ports[0]); provider = self.start(supplier_args, self.f.ports[1])
        first = self.f.exercise('--acknowledge')
        def check_ready():
            connection = http.client.HTTPConnection('127.0.0.1', self.f.ports[0], timeout=2)
            try:
                connection.request('GET', '/health/ready')
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual('ready', json.loads(response.read())['status'])
            finally: connection.close()
        check_ready()
        self.assertEqual('api_receipt_recorded', first['receipt']['status'])
        self.assertIn('补货3件', Path(first['artifact']).read_text(encoding='utf-8'))
        self.assertEqual(1, len(backend.calls))
        for process in (market, provider): process.terminate(); process.communicate(timeout=8)
        self.start(market_args, self.f.ports[0]); self.start(supplier_args, self.f.ports[1])
        again = self.f.exercise('--recover', '--acknowledge')
        self.assertEqual(first['receipt'], again['receipt']); self.assertEqual(1, len(backend.calls))
        check_ready()


if __name__ == '__main__':
    unittest.main()
