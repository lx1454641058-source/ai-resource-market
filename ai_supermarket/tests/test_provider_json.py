"""本项目原创。固定JSON后端的真实本机HTTP及原供应商授权/交付链路。"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest
from urllib.parse import parse_qs, quote, urlsplit

from ai_supermarket.common import MarketError
from ai_supermarket.provider_json import ProviderJsonQuery
from ai_supermarket.provider_operations import ProviderOperations
from ai_supermarket.tests import test_api_product as fixture
from ai_supermarket.tests import test_provider_authorization as auth_fixture


class ProviderJsonTests(unittest.TestCase):
    def setUp(self):
        self.api = fixture.metadata()['api']
        self.mode = 'valid'; self.calls = []; self.received_headers = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                owner.calls.append(self.path)
                owner.received_headers.append(dict(self.headers))
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                raw = json.dumps({'count': 7, 'warehouse': query.get('location', [''])[0]}).encode()
                if owner.mode.startswith('echo-'):
                    secret = query.get('key', [''])[0]
                    value = ({'nested': [secret]} if owner.mode == 'echo-value' else
                             {secret: 'value'} if owner.mode == 'echo-key' else
                             {'url': '/?key=' + quote(secret, safe='')} if owner.mode == 'echo-url' else
                             {'number': int(secret)} if owner.mode == 'echo-number' else {'key': secret})
                    raw = json.dumps(value).encode()
                    if owner.mode == 'echo-escaped':
                        raw = ('{"key":"' + ''.join('\\u%04x' % ord(c) for c in secret) + '"}').encode()
                if owner.mode == 'json-error': raw = b'{"error":"private-upstream-message"}'
                if owner.mode == 'duplicate-json': raw = b'{"count":7,"count":8}'
                if owner.mode == 'invalid-json': raw = b'not json'
                if owner.mode == 'oversize': raw = b' ' * 1025
                if owner.mode == 'drop': self.close_connection = True; return
                self.send_response(302 if owner.mode == 'redirect' else 500 if owner.mode == 'error' else 200)
                if owner.mode == 'redirect': self.send_header('Location', '/should-never-follow')
                self.send_header('Content-Type', 'text/html' if owner.mode == 'html' else 'application/json')
                if owner.mode == 'duplicate-header': self.send_header('Content-Type', 'application/json')
                if owner.mode == 'compressed': self.send_header('Content-Encoding', 'gzip')
                if owner.mode != 'oversize':
                    self.send_header('Content-Length', str(len(raw) + (1 if owner.mode == 'truncated' else 0)))
                self.end_headers()
                if owner.mode == 'slow': time.sleep(.15)
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.thread.join, 2); self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/stock'
        self.backend = self.make()

    def make(self, **changes):
        args = {'api': self.api, 'endpoint': self.endpoint, 'query_map': {'warehouse': 'location'},
                'fixed_query': {'format': 'json'}, 'result_check': self.check_result, 'enabled': True}
        args.update(changes)
        return ProviderJsonQuery(**args)

    @staticmethod
    def check_result(result, inputs):
        return (isinstance(result, dict) and set(result) == {'count', 'warehouse'}
                and type(result['count']) is int and result['warehouse'] == inputs['warehouse'])

    def execute(self, **changes):
        return self.backend.execute('original-op', 'stock', changes or {'warehouse': 'fictional'})

    def error(self, code, call, *args, **kwargs):
        with self.assertRaises(MarketError) as caught: call(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn('private-upstream-message', str(caught.exception))

    def provider(self):
        f = auth_fixture.ProviderAuthorizationTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.operations.execute = self.backend.execute
        return f

    def test_actual_query_mapping_and_encoding_cannot_override_fixed_fields(self):
        value = 'a&format=xml/汉字'
        self.assertEqual({'count': 7, 'warehouse': value}, self.execute(warehouse=value))
        parts = urlsplit(self.calls[0])
        self.assertEqual('/stock', parts.path)
        self.assertEqual({'format': ['json'], 'location': [value]}, parse_qs(parts.query))

    def test_disabled_unknown_operation_and_bad_input_never_dispatch(self):
        self.error('provider_backend_disabled', self.make(enabled=False).execute, 'op', 'stock', {'warehouse': 'x'})
        self.error('operation_denied', self.backend.execute, 'op', 'other', {'warehouse': 'x'})
        for inputs in ({}, {'url': self.endpoint}, {'warehouse': 1}, {'warehouse': 'x' * 33}):
            self.error('invalid_params', self.backend.execute, 'op', 'stock', inputs)
        self.assertEqual([], self.calls)

    def test_operator_endpoint_and_headers_are_strictly_fixed(self):
        for endpoint in ('http://example.com/api', 'https://user:pass@example.com/',
                         'https://example.com/api?token=x', 'https://example.com/#x',
                         'https://example.com/\r\n', 'https://example.com:0/'):
            self.error('invalid_endpoint', self.make, endpoint=endpoint)
        for headers in ({'Host': 'evil.invalid'}, {'Authorization': 'test\nsecret'}, {'X-API-Key': ''}):
            self.error('invalid_configuration', self.make, headers=headers)
        self.assertEqual([], self.calls)

    def test_mapping_mismatch_collision_and_invalid_configuration_rejected(self):
        for changes in ({'query_map': {}}, {'query_map': {'warehouse': 'format'}},
                        {'query_map': {'warehouse': 'a&b'}}, {'fixed_query': {'bad': []}},
                        {'timeout': float('nan')}, {'timeout': True}, {'enabled': 'yes'}, {'result_check': None}):
            self.error('invalid_configuration', self.make, **changes)
        self.assertEqual([], self.calls)

    def test_configuration_is_copied_and_synthetic_credentials_only_reach_fixed_origin(self):
        mapping = {'warehouse': 'location'}; headers = {'X-API-Key': 'synthetic-not-a-real-key'}
        self.backend = self.make(query_map=mapping, headers=headers)
        mapping['warehouse'] = 'changed'; headers['X-API-Key'] = 'changed'
        self.api['operation'] = 'changed'
        self.assertEqual(7, self.execute()['count'])
        self.assertEqual('synthetic-not-a-real-key', self.received_headers[0]['X-API-Key'])
        self.assertEqual(1, len(self.calls))

    def test_invalid_http_and_json_results_never_follow_redirect_or_retry(self):
        for mode in ('redirect', 'error', 'html', 'duplicate-header', 'compressed', 'oversize',
                     'truncated', 'duplicate-json', 'invalid-json', 'json-error', 'drop'):
            with self.subTest(mode=mode):
                self.mode = mode; before = len(self.calls)
                self.error('provider_result_unknown', self.execute)
                self.assertEqual(before + 1, len(self.calls))

    def test_timeout_is_bounded_and_does_not_retry(self):
        self.mode = 'slow'; self.backend = self.make(timeout=.05)
        start = time.monotonic()
        self.error('provider_result_unknown', self.execute)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(1, len(self.calls))

    def test_validator_cannot_mutate_original_result_or_input_and_must_return_true(self):
        inputs = {'warehouse': 'fictional'}
        def check(result, provided):
            result['count'] = 99; provided['warehouse'] = 'changed'; return True
        self.backend = self.make(result_check=check)
        self.assertEqual(7, self.backend.execute('op', 'stock', inputs)['count'])
        self.assertEqual('fictional', inputs['warehouse'])
        self.backend = self.make(result_check=lambda *args: 1)
        self.error('provider_result_unknown', self.execute)

    def test_encoded_target_has_limit_before_network(self):
        api = deepcopy(self.api); api['input_fields'][0]['limit'] = 8192
        self.backend = self.make(api=api)
        self.error('invalid_params', self.execute, warehouse='&' * 8192)
        self.assertEqual([], self.calls)

    def test_remote_authorization_to_original_json_api_result_license_and_receipt(self):
        f = self.provider()
        result = f.invoke()
        self.assertEqual('api_result_received', result['status'], result)
        self.assertEqual({'count': 7, 'warehouse': 'fictional'}, json.loads(Path(result['path']).read_text()))
        self.assertEqual(f.f.f.meta['license_text'], Path(result['license_path']).read_text(encoding='utf-8'))
        self.assertEqual('api_receipt_recorded', f.client.acknowledge('one')['status'])
        self.assertEqual(1, len(self.calls)); self.assertGreaterEqual(f.auth_calls, 4)
        f.operations = ProviderOperations(f.root / 'supplier-operations.sqlite', 'provider-test', 'fixed-provider',
            f.key, f.authorizer, self.backend.execute, clock=lambda: 1000)
        again = f.invoke()
        self.assertEqual(Path(result['path']).read_bytes(), Path(again['path']).read_bytes())
        self.assertEqual(result['sha256'], again['sha256'])
        operation_id = json.loads(Path(result['manifest_path']).read_text())['operation_id']
        self.assertEqual({'count': 7, 'warehouse': 'fictional'},
                         f.provider.execute(operation_id, 'stock', {'warehouse': 'fictional'}))
        self.assertEqual(1, len(self.calls))

    def test_supplier_denied_release_stops_before_original_api(self):
        f = self.provider(); f.authorizer.releases = {('vendor:stock', '1.0.0'): 'b' * 64}
        self.assertEqual('reserved', f.invoke()['status'])
        self.assertEqual([], self.calls)

    def test_unknown_api_cannot_be_reexecuted_or_certified_missing_after_restart(self):
        f = self.provider(); self.mode = 'drop'
        self.assertEqual('reserved', f.invoke()['status'])
        self.mode = 'valid'
        f.operations = ProviderOperations(f.root / 'supplier-operations.sqlite', 'provider-test', 'fixed-provider',
            f.key, f.authorizer, self.backend.execute, clock=lambda: 1000)
        self.assertEqual('reserved', f.invoke()['status'])
        self.assertEqual('reserved', f.client.recover('one')['status'])
        self.assertEqual(1, len(self.calls))

    def test_private_query_is_copied_encoded_and_not_overridden_by_input(self):
        secret = 'synthetic&+/=credential'; private = {'key': secret}
        self.backend = self.make(private_query=private)
        private['key'] = 'changed-value'
        self.assertEqual(7, self.execute(warehouse='x&key=attacker')['count'])
        self.assertEqual({'key': [secret], 'format': ['json'], 'location': ['x&key=attacker']},
                         parse_qs(urlsplit(self.calls[0]).query))
        self.error('invalid_params', self.execute, warehouse='x', key='attacker')
        self.assertEqual(1, len(self.calls))

    def test_invalid_private_parameters_and_collisions_never_dispatch(self):
        for private in ([], {'key': ''}, {'key': 'short'}, {'key': 'x' * 2049},
                        {'key': 'linebreak\nvalue'}, {'key': 'contains space'}, {'key': 12345678},
                        {'key': '非ASCII凭据'}, {'bad&name': 'synthetic-key'},
                        {'format': 'synthetic-key'}, {'location': 'synthetic-key'},
                        {f'key{i}': 'synthetic-key' for i in range(5)}):
            with self.subTest(private=private):
                self.error('invalid_configuration', self.make, private_query=private)
        self.assertEqual([], self.calls)

    def test_private_credential_echo_never_reaches_checker(self):
        checks = []
        for mode in ('echo-value', 'echo-key', 'echo-url', 'echo-escaped', 'echo-number'):
            secret = '12345678901' if mode == 'echo-number' else 'synthetic&+/=credential'
            self.backend = self.make(private_query={'key': secret}, result_check=lambda *args: checks.append(args) or True)
            self.mode = mode
            with self.assertRaises(MarketError) as caught: self.execute()
            self.assertEqual('provider_result_unknown', caught.exception.code)
            self.assertNotIn(secret, str(caught.exception))
        self.assertEqual([], checks); self.assertEqual(5, len(self.calls))

    def test_redirect_with_private_query_never_follows_or_exposes_target(self):
        secret = 'synthetic-query-credential'
        self.backend = self.make(private_query={'key': secret}); self.mode = 'redirect'
        with self.assertRaises(MarketError) as caught: self.execute()
        self.assertEqual('provider_result_unknown', caught.exception.code)
        self.assertNotIn(secret, str(caught.exception)); self.assertEqual(1, len(self.calls))

    def test_private_query_is_included_in_pre_network_target_limit(self):
        # Percent encoding expands each punctuation byte by three.
        self.error('invalid_params', self.make, private_query={f'key{i}': '&' * 2048 for i in range(4)})
        self.assertEqual([], self.calls)
