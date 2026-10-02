"""本项目原创。配置式供应商真实CLI启动及市场到原接口交付，无真实账户或资金。"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ai_supermarket.api_provider import ApiProviderClient
from ai_supermarket.assets import version_hash
from ai_supermarket.common import MarketError, compact
from ai_supermarket.provider_service import FORMAT, ResultChecks, application, configure, load, prepare, probe
from ai_supermarket.tests import test_provider_json as backend_fixture
from ai_supermarket.tests import test_provider_authorization as auth_fixture

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'tools/market_provider_service.py'


class ProviderServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = backend_fixture.ProviderJsonTests(); self.backend.setUp(); self.addCleanup(self.backend.doCleanups)
        self.f = auth_fixture.ProviderAuthorizationTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.root; self.directory = self.root / 'configured-provider'; self.query_file = None
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.config = {'format': FORMAT, 'market_url': self.f.remote.base_url,
            'provider': 'provider-test', 'binding_id': 'fixed-provider', 'seller': 'vendor',
            'key_id': self.f.seller['key_id'], 'listen_port': self.port,
            'release': {'id': 'vendor:stock', 'version': '1.0.0', 'release_digest': self.f.f.f.digest},
            'api': deepcopy(self.f.f.f.meta['api']),
            'backend': {'endpoint': self.backend.endpoint, 'query_map': {'warehouse': 'location'},
                        'fixed_query': {'format': 'json'}, 'timeout': 2},
            'result_checks': [{'path': ['count'], 'type': 'integer', 'minimum': 0},
                              {'path': ['warehouse'], 'type': 'string', 'input': 'warehouse'}]}
        self.config_file = self.root / 'service.json'; self.key_file = self.root / 'connection.json'
        self.config_file.write_text(compact(self.config), encoding='utf-8')
        self.key_file.write_text(compact({'key_hex': self.f.key.hex()}), encoding='utf-8')

    def prepare(self):
        return prepare(self.directory, self.config_file, self.root / 'seller.json', self.key_file, query_file=self.query_file)

    def command(self, name):
        return [sys.executable, '-X', 'utf8', '-B', str(TOOL), name, '--directory', str(self.directory)]

    def cli(self, name, *, code=0):
        args = self.command(name)
        if name == 'prepare': args += ['--config', str(self.config_file), '--seller-credential', str(self.root / 'seller.json'),
                                       '--connection-key', str(self.key_file)]
        if name == 'prepare' and self.query_file is not None:
            args += ['--backend-query-credentials', str(self.query_file)]
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(code, result.returncode, result.stderr)
        self.assertNotIn(self.f.seller['token'], result.stdout + result.stderr)
        self.assertNotIn(self.f.key.hex(), result.stdout + result.stderr)
        self.assertNotIn('synthetic&+/=credential', result.stdout + result.stderr)
        return json.loads(result.stdout)

    def error(self, code, function, *args):
        with self.assertRaises(MarketError) as caught: function(*args)
        self.assertEqual(code, caught.exception.code)

    def start(self):
        proc = subprocess.Popen(self.command('serve'), cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def stop():
            if proc.poll() is None: proc.terminate()
            proc.communicate(timeout=8)
        self.addCleanup(stop)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _, error = proc.communicate(); self.fail(error.decode('utf-8'))
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1): break
            except OSError: time.sleep(.03)
        else: self.fail('configured service did not start')
        client = ApiProviderClient('provider-test', f'http://127.0.0.1:{self.port}/api', self.f.key,
                                   'fixed-provider', ['stock'])
        self.f.f.host.execute = client.execute; self.f.f.host.lookup = client.lookup
        return proc, client

    def test_prepare_check_and_probe_cli_are_separate_and_do_not_call_backend(self):
        self.assertEqual('prepared', self.cli('prepare')['status'])
        before = hashlib.sha256((self.directory / 'operations.sqlite').read_bytes()).hexdigest()
        self.assertEqual('local_configuration_only', self.cli('check')['scope'])
        self.assertEqual(before, hashlib.sha256((self.directory / 'operations.sqlite').read_bytes()).hexdigest())
        result = self.cli('probe')
        self.assertEqual('connected', result['status']); self.assertFalse(result['operation_authorized'])
        self.assertEqual([], self.backend.calls)
        self.assertEqual({'provider.json', 'seller-credential.json', 'provider-key.json', 'backend-headers.json',
                          'binding.json', 'operations.sqlite'}, {p.name for p in self.directory.iterdir()})

    def test_existing_directory_is_never_reinitialized(self):
        self.prepare(); before = (self.directory / 'operations.sqlite').read_bytes()
        self.assertEqual('error', self.cli('prepare', code=1)['status'])
        self.assertEqual(before, (self.directory / 'operations.sqlite').read_bytes())

    def test_invalid_configuration_fails_before_creating_output(self):
        for change in ({'seller': 'other'}, {'key_id': 'ak_' + '0' * 32},
                       {'result_checks': []}, {'result_checks': [{'path': [], 'type': [], 'expression': 'unsafe'}]}):
            self.config_file.write_text(compact({**self.config, **change}), encoding='utf-8')
            with self.assertRaises(MarketError): self.prepare()
            self.assertFalse(self.directory.exists())
        self.assertEqual([], self.backend.calls)

    def test_missing_or_empty_original_ledger_is_not_recreated(self):
        self.prepare(); path = self.directory / 'operations.sqlite'
        path.rename(self.directory / 'retained.sqlite')
        self.error('provider_incomplete', load, self.directory); self.assertFalse(path.exists())
        path.write_bytes(b'')
        self.error('provider_incomplete', load, self.directory); self.assertEqual(b'', path.read_bytes())

    def test_changed_backend_config_or_credential_refuses_start(self):
        self.prepare()
        for name in ('provider.json', 'provider-key.json', 'backend-headers.json', 'seller-credential.json'):
            path = self.directory / name; raw = path.read_bytes(); path.write_bytes(raw + b' ')
            try: self.error('connection_changed', application, self.directory)
            finally: path.write_bytes(raw)
        self.assertEqual([], self.backend.calls)

    def test_rule_paths_types_ranges_and_input_matching(self):
        rules = [{'path': [1, 0, 'value'], 'type': 'integer', 'minimum': 1, 'maximum': 10},
                 {'path': [1, 0, 'warehouse'], 'type': 'string', 'input': 'warehouse'},
                 {'path': [0, 'status'], 'type': 'string', 'equals': 'ok'}]
        check = ResultChecks(rules, self.config['api'])
        value = [{'status': 'ok'}, [{'value': 7, 'warehouse': 'x'}]]
        self.assertTrue(check(value, {'warehouse': 'x'}))
        for change in (True, 0, 11, '7'):
            altered = deepcopy(value); altered[1][0]['value'] = change
            self.assertFalse(check(altered, {'warehouse': 'x'}))
        self.assertFalse(check(value, {'warehouse': 'wrong'})); self.assertFalse(check([], {}))
        rules[0]['minimum'] = 9
        self.assertTrue(check(value, {'warehouse': 'x'}))

    def test_invalid_rules_are_rejected_without_executing_code(self):
        invalid = [[], [{'path': ['x'], 'type': 'integer', 'minimum': True}],
                   [{'path': ['x'], 'type': 'integer', 'minimum': 9, 'maximum': 2}],
                   [{'path': ['x'], 'type': 'integer', 'equals': True}],
                   [{'path': ['x'], 'type': 'string', 'input': []}],
                   [{'path': [True], 'type': 'string'}], [{'path': [], 'type': []}],
                   [{'path': [], 'type': 'object', 'expression': 'arbitrary Python'}]]
        for rules in invalid:
            with self.subTest(rules=rules): self.error('invalid_configuration' if not any('expression' in r for r in rules)
                else 'invalid_params', ResultChecks, rules, self.config['api'])

    def test_real_cli_process_delivers_files_receipt_and_reopens_original_operations(self):
        self.prepare(); process, client = self.start()
        result = self.f.invoke()
        self.assertEqual('api_result_received', result['status'], result)
        self.assertEqual({'count': 7, 'warehouse': 'fictional'}, json.loads(Path(result['path']).read_text()))
        self.assertEqual(self.f.f.f.meta['license_text'], Path(result['license_path']).read_text(encoding='utf-8'))
        self.assertEqual('api_receipt_recorded', self.f.client.acknowledge('one')['status'])
        operation_id = json.loads(Path(result['manifest_path']).read_text())['operation_id']
        process.terminate(); process.communicate(timeout=8)
        _, client = self.start()
        self.assertEqual(7, client.execute(operation_id, 'stock', {'warehouse': 'fictional'})['count'])
        self.assertEqual(1, len(self.backend.calls))

    def test_runtime_change_stops_request_before_backend(self):
        self.prepare(); self.start()
        path = self.directory / 'provider.json'; path.write_bytes(path.read_bytes() + b' ')
        self.assertEqual('reserved', self.f.invoke()['status'])
        self.assertEqual([], self.backend.calls)

    def test_business_result_failure_remains_unknown_across_restart(self):
        self.prepare(); process, _ = self.start(); self.backend.mode = 'json-error'
        self.assertEqual('reserved', self.f.invoke()['status'])
        process.terminate(); process.communicate(timeout=8); self.backend.mode = 'valid'; self.start()
        self.assertEqual('reserved', self.f.client.recover('one')['status'])
        self.assertEqual(1, len(self.backend.calls))

    def test_revoked_seller_blocks_probe_and_dispatch(self):
        self.prepare(); self.start(); self.f.f.identities.revoke_key(self.f.seller['key_id'])
        self.error('provider_connection_unverified', probe, self.directory)
        self.assertEqual('reserved', self.f.invoke()['status'])
        self.assertEqual([], self.backend.calls)

    def test_http_ingress_rejects_browser_remote_peer_and_wrong_host_before_body(self):
        self.prepare(); app, _ = application(self.directory)
        async def run():
            for host, peer, origin, expected in ((b'wrong', '127.0.0.1', False, 400),
                    (f'127.0.0.1:{self.port}'.encode(), '127.0.0.2', False, 403),
                    (f'127.0.0.1:{self.port}'.encode(), '127.0.0.1', True, 403)):
                scope = {'type': 'http', 'method': 'POST', 'path': '/api', 'scheme': 'http',
                         'client': (peer, 1234), 'headers': [(b'host', host)], 'query_string': b''}
                if origin: scope['headers'].append((b'origin', b'https://example.invalid'))
                async def unread(): raise AssertionError('must not read body')
                messages = []
                async def send(message): messages.append(message)
                await app(scope, unread, send)
                self.assertEqual(expected, messages[0]['status'])
        asyncio.run(run())
        self.assertEqual([], self.backend.calls)

    def private_query(self):
        self.query_file = self.root / 'private-query.json'
        self.query_file.write_text(compact({'key': 'synthetic&+/=credential'}), encoding='utf-8')

    def test_private_cli_delivery_and_restart_keep_secret_out_of_records(self):
        self.private_query(); self.assertEqual('prepared', self.cli('prepare')['status'])
        self.assertEqual('checked', self.cli('check')['status'])
        self.assertEqual('connected', self.cli('probe')['status'])
        self.assertEqual([], self.backend.calls)
        process, _ = self.start(); result = self.f.invoke()
        self.assertEqual('api_result_received', result['status'], result)
        self.assertEqual(['synthetic&+/=credential'], parse_qs(urlsplit(self.backend.calls[0]).query)['key'])
        self.assertEqual('api_receipt_recorded', self.f.client.acknowledge('one')['status'])
        operation_id = json.loads(Path(result['manifest_path']).read_text())['operation_id']
        for path in (Path(result['path']), Path(result['manifest_path']), self.directory / 'operations.sqlite',
                     self.directory / 'provider.json', self.directory / 'binding.json'):
            self.assertNotIn(b'synthetic&+/=credential', path.read_bytes())
        process.terminate(); stdout, stderr = process.communicate(timeout=8)
        self.assertNotIn(b'synthetic', stdout + stderr)
        _, client = self.start()
        self.assertEqual(7, client.execute(operation_id, 'stock', {'warehouse': 'fictional'})['count'])
        self.assertEqual(1, len(self.backend.calls))

    def test_private_file_change_or_loss_cannot_fall_back_to_anonymous(self):
        self.private_query(); self.prepare()
        path = self.directory / 'backend-query.json'; raw = path.read_bytes()
        path.write_bytes(raw + b' ')
        self.error('connection_changed', load, self.directory)
        path.write_bytes(raw); path.rename(self.directory / 'retained-query.json')
        with self.assertRaises(OSError): load(self.directory)
        self.assertFalse(path.exists()); self.assertEqual([], self.backend.calls)

    def test_private_runtime_change_blocks_before_backend_and_keeps_original_ledger(self):
        self.private_query(); self.prepare(); self.start()
        path = self.directory / 'backend-query.json'; path.write_bytes(path.read_bytes() + b' ')
        self.assertEqual('reserved', self.f.invoke()['status'])
        self.assertEqual([], self.backend.calls)

    def test_unbound_private_file_cannot_upgrade_old_configuration(self):
        self.prepare(); self.start()
        (self.directory / 'backend-query.json').write_text('{"key":"synthetic-unbound"}', encoding='utf-8')
        self.error('connection_changed', load, self.directory)
        self.assertEqual('reserved', self.f.invoke()['status']); self.assertEqual([], self.backend.calls)

    def test_echoed_private_key_is_not_cached_or_redelivered_after_restart(self):
        self.private_query(); self.prepare(); process, _ = self.start(); self.backend.mode = 'echo-value'
        self.assertEqual('reserved', self.f.invoke()['status'])
        self.assertNotIn(b'synthetic&+/=credential', (self.directory / 'operations.sqlite').read_bytes())
        process.terminate(); process.communicate(timeout=8); self.backend.mode = 'valid'; self.start()
        self.assertEqual('reserved', self.f.client.recover('one')['status'])
        self.assertEqual(1, len(self.backend.calls))

    def test_invalid_private_file_cli_leaves_no_prepared_directory(self):
        self.private_query(); self.query_file.write_text('{"key":"secret\\ninvalid"}', encoding='utf-8')
        result = self.cli('prepare', code=1)
        self.assertEqual('invalid_configuration', result['error_code'])
        self.assertFalse(self.directory.exists()); self.assertEqual([], self.backend.calls)

    def configuration_source(self):
        deployment = {name: self.config[name] for name in ('binding_id', 'listen_port', 'backend', 'result_checks')}
        path = self.root / 'deployment.json'; path.write_text(compact(deployment), encoding='utf-8')
        return path, self.root / 'generated'

    def test_configure_cli_approved_release_to_running_service_without_manual_digest(self):
        deployment, output = self.configuration_source()
        args = [sys.executable, '-X', 'utf8', '-B', str(TOOL), 'configure',
                '--url', self.f.remote.base_url, '--credential-file', str(self.root / 'seller.json'),
                '--seller', 'vendor', '--id', 'vendor:stock', '--version', '1.0.0',
                '--deployment', str(deployment), '--output', str(output)]
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual('configured_not_started', receipt['status'])
        self.assertEqual(2, receipt['network_requests']); self.assertEqual(0, receipt['market_mutations'])
        self.assertNotIn(self.f.seller['token'], result.stdout + result.stderr)
        self.assertNotIn(self.f.f.f.meta['license_text'], result.stdout)
        self.assertEqual(self.f.f.f.digest, receipt['release_digest'])
        observation = json.loads((output / 'observation.json').read_text(encoding='utf-8'))
        self.assertEqual(observation['content_hash'], version_hash(observation['metadata'], observation['content']))
        self.assertEqual(receipt['observation_sha256'], hashlib.sha256((output / 'observation.json').read_bytes()).hexdigest())
        config = json.loads((output / 'provider.json').read_text(encoding='utf-8'))
        self.assertEqual(self.config, config)
        self.assertEqual([], self.backend.calls)
        self.assertEqual({'provider.json', 'observation.json', 'receipt.json'}, {p.name for p in output.iterdir()})
        self.config_file = output / 'provider.json'; self.prepare(); self.start()
        delivered = self.f.invoke()
        self.assertEqual('api_result_received', delivered['status'], delivered)
        self.assertEqual('api_receipt_recorded', self.f.client.acknowledge('one')['status'])
        self.assertEqual(1, len(self.backend.calls))

    def test_configure_refuses_wrong_seller_before_status_query(self):
        deployment, output = self.configuration_source()
        with patch.object(self.f.seller_remote, 'call', wraps=self.f.seller_remote.call) as call:
            self.error('provider_connection_unverified', configure, self.f.seller_remote,
                       'another', 'another:stock', '1.0.0', deployment, output)
            call.assert_not_called()
        self.assertFalse(output.exists())

    def test_configure_refuses_nonapproved_inactive_changed_or_malformed_release(self):
        deployment, output = self.configuration_source(); remote = self.f.seller_remote
        source = remote.call('seller_status', {'id': 'vendor:stock', 'version': '1.0.0'})
        discovery = remote.tool_list()
        altered = []
        for field, value in [('status', 'pending_review'), ('id', 'other:stock'), ('version', '2.0.0'),
                             ('content_hash', 'b' * 64), ('content', 'modified-body'), ('revision', True)]:
            item = deepcopy(source); item[field] = value; altered.append(item)
        for availability in ('withdrawn', 'suspended'):
            item = deepcopy(source); item['offer']['availability'] = availability; altered.append(item)
        item = deepcopy(source); item['metadata']['api']['provider'] = []; altered.append(item)
        item = deepcopy(source); item['offer'] = None; altered.append(item)
        item = deepcopy(source); item['unexpected'] = True; altered.append(item)
        with patch.object(remote, 'tool_list', return_value=discovery):
            for item in altered:
                with self.subTest(keys=list(item)), patch.object(remote, 'call', return_value=item) as call:
                    with self.assertRaises(MarketError):
                        configure(remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
                    self.assertEqual(1, call.call_count); self.assertFalse(output.exists())
        self.assertEqual([], self.backend.calls)

    def test_configure_preserves_existing_candidate_without_network_refresh(self):
        deployment, output = self.configuration_source(); remote = self.f.seller_remote
        configure(remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
        original = {path.name: path.read_bytes() for path in output.iterdir()}
        with patch.object(remote, 'tool_list', side_effect=AssertionError('must not refresh')):
            self.error('state_conflict', configure, remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
        self.assertEqual(original, {path.name: path.read_bytes() for path in output.iterdir()})

    def test_configure_read_error_does_not_retry_or_write_candidate(self):
        deployment, output = self.configuration_source(); remote = self.f.seller_remote
        with patch.object(remote, 'call', side_effect=MarketError('network_error', 'synthetic')) as call:
            self.error('network_error', configure, remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
            self.assertEqual(1, call.call_count)
        self.assertFalse(output.exists())
        with patch.object(remote, 'call', return_value={'status': 'error', 'error_code': 'not_found'}):
            self.error('provider_release_unverified', configure, remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
        self.assertFalse(output.exists())

    def test_configure_invalid_local_scope_stops_before_discovery(self):
        deployment, output = self.configuration_source(); remote = self.f.seller_remote
        with patch.object(remote, 'tool_list', side_effect=AssertionError('must not connect')):
            self.error('seller_mismatch', configure, remote, 'vendor', 'other:stock', '1.0.0', deployment, output)
            deployment.write_text('{"arbitrary":"instructions"}', encoding='utf-8')
            self.error('invalid_params', configure, remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
        self.assertFalse(output.exists())

    def test_configure_detects_credential_change_during_discovery(self):
        deployment, output = self.configuration_source(); remote = self.f.seller_remote
        discovery = remote.tool_list(); credential = self.root / 'seller.json'
        def change():
            credential.write_text(compact({'token': 'ak_' + 'b' * 32 + '.' + 'c' * 43}), encoding='utf-8')
            return discovery
        with patch.object(remote, 'tool_list', side_effect=change), patch.object(remote, 'call') as call:
            self.error('connection_changed', configure, remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
            call.assert_not_called()
        self.assertFalse(output.exists())

    def test_configure_saved_snapshot_does_not_bypass_later_suspension(self):
        deployment, output = self.configuration_source()
        configure(self.f.seller_remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
        self.config_file = output / 'provider.json'; self.prepare(); self.start()
        with self.f.f.f.seller.store.connect(write=True) as db:
            db.execute("UPDATE offers SET availability='suspended' WHERE resource_id='vendor:stock'")
        result = self.f.invoke()
        self.assertEqual('error', result['status'])
        self.assertEqual([], self.backend.calls)

    def test_configure_current_offer_price_does_not_replace_approved_metadata(self):
        deployment, output = self.configuration_source()
        with self.f.f.f.seller.store.connect(write=True) as db:
            db.execute("UPDATE offers SET price_cents=123,revision=revision+1 WHERE resource_id='vendor:stock'")
        result = configure(self.f.seller_remote, 'vendor', 'vendor:stock', '1.0.0', deployment, output)
        self.assertEqual(self.f.f.f.digest, result['release_digest'])
        observed = json.loads((output / 'observation.json').read_text(encoding='utf-8'))
        self.assertEqual(0, observed['metadata']['price_cents'])
        self.assertEqual(123, observed['offer']['price_cents'])
        self.assertNotIn('price_cents', json.loads((output / 'provider.json').read_text(encoding='utf-8')))
