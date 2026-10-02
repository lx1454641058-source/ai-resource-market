"""来源：本项目原创。卖方离线准备与真实服务端契约交叉验收；只使用临时数据。"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket.exchange import Exchange
from ai_supermarket.exchange_tools import ExchangeInterface
from tools.market_seller import MAX_ERRORS, MAX_INPUT_BYTES, check_request, sample_request


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools/market_seller.py'


class SellerPreparationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.seller = Exchange(self.root / 'market.sqlite', 'vendor', 'seller')
        self.interface = ExchangeInterface(self.seller)

    def test_sample_is_accepted_as_draft_and_submission_but_not_published(self):
        request = sample_request()
        result = check_request(request, 'vendor')
        self.assertEqual('checked', result['status'])
        self.assertTrue(result['request_valid'])
        self.assertTrue(result['submission_contract_valid'])
        for flag in ('server_state_checked', 'rights_verified', 'sample_executed',
                     'publication_approved', 'payment_available'):
            self.assertFalse(result[flag])
        self.assertNotIn(request['content'], compact(result))
        draft = self.interface.call('seller_draft', request)
        self.assertEqual('draft', draft['status'])
        pending = self.seller.submit(draft['id'], draft['version'], draft['revision'], 'submit')
        self.assertEqual('pending_review', pending['status'])
        buyer = Exchange(self.root / 'market.sqlite', 'buyer', 'buyer')
        self.assertEqual('no_match', buyer.search('事实清单', purpose='personal_use')['status'])

    def test_missing_contract_distinguishes_saveable_draft_from_submission(self):
        request = sample_request()
        del request['metadata']['usage_contract']
        result = check_request(request, 'vendor')
        self.assertTrue(result['request_valid'])
        self.assertFalse(result['submission_contract_valid'])
        self.assertEqual('contract_required', result['errors'][0]['code'])
        draft = self.interface.call('seller_draft', request)
        result = self.interface.call('seller_submit', {'id': draft['id'], 'version': draft['version'],
                                    'revision': 1, 'request_id': 'submit'})
        self.assertEqual('contract_required', result['error_code'])

    def test_many_errors_are_bounded_known_paths_and_do_not_echo_unknown_data(self):
        request = sample_request()
        request['metadata'] = {'secret-unknown-field': 'private-sentinel'}
        request['content'] = False
        result = check_request(request, 'vendor')
        self.assertEqual(MAX_ERRORS, len(result['errors']))
        self.assertTrue(result['more_errors'])
        self.assertEqual('$.metadata.title', result['errors'][0]['path'])
        self.assertNotIn('private-sentinel', compact(result))
        self.assertNotIn('secret-unknown-field', compact(result))
        self.assertFalse(result['request_valid'])

    def test_schema_type_enum_duplicate_and_nested_errors_match_server_rejection(self):
        cases = []
        for field, value in [('price_cents', True), ('allowed_uses', ['personal_use'] * 2),
                             ('kind', 'executable'), ('keywords', [{}]), ('usage_contract', None)]:
            request = sample_request()
            request['metadata'][field] = value
            cases.append(request)
        nested = sample_request()
        nested['metadata']['usage_contract']['sample']['check'] = ''
        cases.append(nested)
        for request in cases:
            with self.subTest(request=request['metadata'].get('kind')):
                result = check_request(request, 'vendor')
                self.assertFalse(result['request_valid'])
                self.assertEqual('error', self.interface.call('seller_draft', request)['status'])
        self.assertEqual('$.metadata.usage_contract.sample.check', check_request(nested, 'vendor')['errors'][0]['path'])

    def test_utf8_byte_limits_match_domain_limits_not_character_count(self):
        for field, amount in [('content', 8001), ('license_text', 2667)]:
            request = sample_request()
            destination = request if field == 'content' else request['metadata']
            destination[field] = '中' * amount
            result = check_request(request, 'vendor')
            self.assertFalse(result['request_valid'])
            self.assertEqual('$.content' if field == 'content' else '$.metadata.license_text', result['errors'][0]['path'])
            self.assertEqual('error', self.interface.call('seller_draft', request)['status'])

    def test_whitespace_control_text_and_unsupported_license_match_domain(self):
        for field, value in [('content', ' \n\t'), ('content', 'text\x00'), ('license_name', 'AGPL-3.0')]:
            request = sample_request()
            (request if field == 'content' else request['metadata'])[field] = value
            result = check_request(request, 'vendor')
            self.assertFalse(result['request_valid'])
            self.assertEqual(result['errors'][0]['code'], self.interface.call('seller_draft', request)['error_code'])

    def test_contract_domain_checks_missing_capability_and_bad_dependencies(self):
        for change in ('missing_capability', 'license', 'duplicate'):
            request = sample_request()
            contract = request['metadata']['usage_contract']
            dependency = {'id': 'dep:1', 'license_name': 'LicenseRef-Test', 'max_cost_cents': 0}
            if change == 'missing_capability':
                contract['capabilities'] = []
            elif change == 'license':
                contract['dependencies'] = [{**dependency, 'license_name': 'GPL-3.0'}]
            else:
                contract['dependencies'] = [dependency, copy.deepcopy(dependency)]
            result = check_request(request, 'vendor')
            self.assertFalse(result['request_valid'])
            self.assertEqual(result['errors'][0]['code'], self.interface.call('seller_draft', request)['error_code'])

    def test_unknown_dependency_cost_is_valid_declaration_not_readiness(self):
        request = sample_request()
        request['metadata']['usage_contract']['dependencies'] = [
            {'id': 'dep:1', 'license_name': 'LicenseRef-Test', 'max_cost_cents': None}]
        result = check_request(request, 'vendor')
        self.assertEqual('checked', result['status'])
        self.assertFalse(result['server_state_checked'])
        self.assertNotIn('execution_authorized', result)
        self.assertEqual('draft', self.interface.call('seller_draft', request)['status'])

    def test_reference_identifiers_and_composed_id_size_match_server(self):
        for field, value in [('slug', 'Capital'), ('version', 'one'), ('request_id', '../bad')]:
            request = sample_request()
            request[field] = value
            self.assertFalse(check_request(request, 'vendor')['request_valid'])
            self.assertEqual('error', self.interface.call('seller_draft', request)['status'])
        long_seller = 'a' * 128
        self.assertFalse(check_request(sample_request(), long_seller)['request_valid'])

    def test_free_and_high_self_prices_are_valid_without_reference_price(self):
        for price in (0, 100_000_000):
            request = sample_request()
            request['metadata']['price_cents'] = price
            request['slug'] += '-' + str(price)
            request['request_id'] += '-' + str(price)
            self.assertEqual('checked', check_request(request, 'vendor')['status'])
            self.assertEqual('draft', self.interface.call('seller_draft', request)['status'])

    def test_revision_check_cannot_claim_current_server_state_or_ownership(self):
        request = sample_request()
        draft = self.interface.call('seller_draft', request)
        revision = {key: value for key, value in request.items() if key != 'slug'}
        revision.update(id=draft['id'], revision=1, request_id='revise')
        self.assertEqual('checked', check_request(revision, 'vendor', 'seller_revise')['status'])
        self.assertEqual(2, self.interface.call('seller_revise', revision)['revision'])
        revision['request_id'] = 'stale'
        self.assertEqual('checked', check_request(revision, 'vendor', 'seller_revise')['status'])
        self.assertEqual('revision_conflict', self.interface.call('seller_revise', revision)['error_code'])
        mismatch = check_request(revision, 'other', 'seller_revise')
        self.assertEqual('seller_mismatch', mismatch['errors'][0]['code'])

    def test_checker_has_no_database_or_network_side_effects_and_does_not_mutate_input(self):
        request = sample_request()
        original = copy.deepcopy(request)
        with patch('sqlite3.connect', side_effect=AssertionError('unexpected database')), \
             patch('socket.socket', side_effect=AssertionError('unexpected network')):
            self.assertEqual('checked', check_request(request, 'vendor')['status'])
        self.assertEqual(original, request)
        self.assertEqual([], self.seller.list_submissions()['items'])

    def test_direct_python_invalid_json_values_raise_market_error_not_traceback(self):
        for bad in ('\ud800', float('nan'), {'not-json'}):
            request = sample_request()
            request['content'] = bad
            with self.subTest(kind=type(bad).__name__), self.assertRaises(MarketError):
                check_request(request, 'vendor')

    def test_direct_python_oversized_and_cyclic_values_stop_before_validation(self):
        request = sample_request()
        request['content'] = 'x' * MAX_INPUT_BYTES
        with self.assertRaises(MarketError) as caught:
            check_request(request, 'vendor')
        self.assertEqual('invalid_size', caught.exception.code)
        request['content'] = request
        with self.assertRaises(MarketError) as caught:
            check_request(request, 'vendor')
        self.assertEqual('invalid_json', caught.exception.code)


class SellerPreparationCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def run_cli(self, *arguments):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, arguments)],
                                cwd=self.root, capture_output=True, timeout=10)
        self.assertFalse(result.stderr, '助手不应返回堆栈或原始输入')
        return result.returncode, json.loads(result.stdout)

    def test_sample_and_check_from_unrelated_directory_no_overwrite_or_database(self):
        path = self.root / 'request.json'
        self.assertEqual(0, self.run_cli('sample', '--output', path)[0])
        original = path.read_bytes()
        code, reply = self.run_cli('sample', '--output', path)
        self.assertEqual((1, 'destination_exists'), (code, reply['error_code']))
        self.assertEqual(original, path.read_bytes())
        code, reply = self.run_cli('check', '--input', path, '--seller', 'vendor')
        self.assertEqual((0, 'checked'), (code, reply['status']))
        self.assertEqual(['request.json'], [p.name for p in self.root.iterdir()])

    def test_cli_invalid_json_oversize_missing_file_and_field_errors(self):
        path = self.root / 'request.json'
        for raw, expected in [(b'{"a":1,"a":2}', 'invalid_json'), (b'"\\ud800"', 'invalid_json'),
                              (b'{"a":NaN}', 'invalid_json'), (b'x' * (MAX_INPUT_BYTES + 1), 'invalid_size')]:
            path.write_bytes(raw)
            code, reply = self.run_cli('check', '--input', path, '--seller', 'vendor')
            self.assertEqual((1, expected), (code, reply['error_code']))
        path.write_bytes(b'{}')
        code, reply = self.run_cli('check', '--input', path, '--seller', 'vendor')
        self.assertEqual((1, 'needs_changes'), (code, reply['status']))
        code, reply = self.run_cli('check', '--input', self.root / 'missing.json', '--seller', 'vendor')
        self.assertEqual((1, 'unavailable'), (code, reply['error_code']))


if __name__ == '__main__':
    unittest.main()
