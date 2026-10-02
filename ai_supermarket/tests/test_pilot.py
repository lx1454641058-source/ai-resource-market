"""来源：本项目原创。有限试点的冻结、次数、故障和证据标签；不调用真实模型。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError, compact
from ai_supermarket import pilot
from steward.codex_model import CodexModel, ModelUnavailable


class PilotModel:
    name, effort = CodexModel.name, 'medium'

    def __init__(self, contexts, selection='first'):
        self.contexts, self.selection = contexts, selection
        self.last_receipt = None

    def respond(self, role, context):
        self.contexts.append(context)
        self.last_receipt = {'provider': 'codex', 'model': self.name, 'completed': True,
            'completed_turns': 1, 'usage': {'input_tokens': 100, 'cached_input_tokens': 0, 'output_tokens': 20}}
        if context['stage'] == 'select':
            item = context['candidates'][0]
            selected = {'id': item['id'], 'version': item['version']}
            if self.selection == 'none':
                selected = {'id': None, 'version': None}
            elif self.selection == 'invalid':
                selected = {'id': 'not-in-candidates', 'version': '1.0.0'}
            return {'content': compact(selected)}
        return {'content': '## 目标\n## 范围\n## 验收\n' + '\n'.join(context['facts'])}


class PilotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pack = self.root / 'pack'
        self.prepared = pilot.prepare(self.pack)
        self.hash = self.prepared['proposal_hash']
        self.contexts = []

    def execute(self, selection='first'):
        return pilot.execute(self.pack, self.hash, 'synthetic-test-approval', evidence_mode='synthetic',
                             model_factory=lambda: PilotModel(self.contexts, selection))

    def error(self, code, method, *args, **kwargs):
        with self.assertRaises(MarketError) as caught:
            method(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_prepare_has_no_execution_and_does_not_overwrite(self):
        self.assertFalse((self.pack / 'execution').exists())
        self.assertFalse(self.prepared['approved'])
        self.assertEqual(0, self.prepared['real_model_calls'])
        with self.assertRaises(FileExistsError):
            pilot.prepare(self.pack)
        review = json.loads((self.pack / 'review.json').read_text(encoding='utf-8'))
        self.assertEqual(12, review['max_model_calls'])
        self.assertIsNone(review['known_account_charge'])

    def test_tampering_or_source_change_rejects_before_execution(self):
        self.error('pilot_changed', pilot.execute, self.pack, '0'*64, 'test')
        self.assertFalse((self.pack / 'execution').exists())
        with patch('ai_supermarket.pilot.source_hashes', return_value={}):
            self.error('pilot_changed', self.execute)
        spec = json.loads((self.pack / 'proposal.json').read_text(encoding='utf-8'))
        spec['approved'] = True
        (self.pack / 'proposal.json').write_text(compact(spec), encoding='utf-8')
        changed_hash = pilot.digest(compact(spec).encode('utf-8'))
        self.error('pilot_changed', pilot.execute, self.pack, changed_hash, 'test')
        self.assertFalse((self.pack / 'execution').exists())

    def test_synthetic_without_double_and_real_with_double_are_refused(self):
        self.error('invalid_params', pilot.execute, self.pack, self.hash, 'test', evidence_mode='synthetic')
        self.error('invalid_params', pilot.execute, self.pack, self.hash, 'test', model_factory=lambda: None)
        self.assertFalse((self.pack / 'execution').exists())

    def test_complete_three_arm_flow_keeps_quality_and_cost_unknown(self):
        result = self.execute()
        self.assertEqual('awaiting_independent_review', result['status'])
        self.assertEqual(9, len(result['records']))
        self.assertEqual(11, result['model_calls_claimed'])
        self.assertEqual(11, len(self.contexts))
        self.assertIsNone(result['quality_gain'])
        self.assertIsNone(result['cost_savings'])
        self.assertEqual(11_000_000, result['budget']['held_micro_cny'])
        self.assertTrue(all(r['measurement']['outcome'] == 'unknown' for r in result['records']))
        self.assertTrue(all(r['measurement']['total_cost_micro_cny'] is None for r in result['records']))
        self.assertTrue(all(r['measurement']['evidence_mode'] == 'synthetic' for r in result['records']))
        self.assertEqual(9, len(list((self.pack / 'execution').glob('*.md'))))
        self.assertTrue((self.pack / 'execution/report.json').exists())

    def test_existing_rights_and_no_match_are_observed_not_assumed(self):
        result = self.execute()
        selection_contexts = [c for c in self.contexts if c['stage'] == 'select']
        self.assertEqual(2, len(selection_contexts))
        self.assertTrue(any(c['owned'] for c in selection_contexts[1]['candidates']))
        no_match = next(r for r in result['records'] if r['run_id'] == 'no-match-market')
        self.assertIsNone(no_match['selected'])
        self.assertEqual(1, no_match['measurement']['model_calls'])
        self.assertEqual(2, no_match['measurement']['calls'])  # search + deliver

    def test_model_may_decline_all_candidates(self):
        result = self.execute('none')
        self.assertTrue(all(r['selected'] is None for r in result['records'] if r['measurement']['arm'] == 'market'))
        self.assertEqual(11, result['model_calls_claimed'])

    def test_invalid_choice_stops_without_silent_first_item_fallback(self):
        self.error('pilot_invalid_selection', self.execute, 'invalid')
        failure = json.loads((self.pack / 'execution/failure.json').read_text(encoding='utf-8'))
        self.assertEqual(3, failure['model_calls_claimed'])
        self.assertEqual(2, failure['completed_runs'])
        self.assertFalse((self.pack / 'execution/report.json').exists())
        self.error('pilot_already_started', self.execute)
        self.assertEqual(3, len(self.contexts))

    def test_timeout_preserves_claim_and_does_not_leak_provider_error(self):
        with patch.object(PilotModel, 'respond', side_effect=TimeoutError('private provider detail')) as response:
            with self.assertRaises(TimeoutError):
                self.execute()
            self.assertEqual(1, response.call_count)
        failure = (self.pack / 'execution/failure.json').read_text(encoding='utf-8')
        self.assertNotIn('private provider detail', failure)
        self.assertTrue((self.pack / 'execution/call-01.json').exists())
        self.error('pilot_already_started', self.execute)

    def test_concurrent_execution_claims_pack_once(self):
        def invoke(_):
            try:
                return self.execute()['status']
            except MarketError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(invoke, range(2)))
        self.assertCountEqual(['awaiting_independent_review', 'pilot_already_started'], result)
        self.assertEqual(11, len(self.contexts))

    def test_expected_selection_and_validator_not_leaked_to_model(self):
        self.execute()
        for context in self.contexts:
            self.assertNotIn('checks', context)
            self.assertNotIn('owned', context)
            if context['stage'] == 'select':
                self.assertNotIn('resource', context)
            if context['stage'] == 'deliver' and 'resource' in context:
                self.assertIsInstance(context['resource'], str)

    def test_shape_checks_do_not_grant_independent_pass(self):
        case = pilot.CASES[0]
        check = pilot.structural_check(case, '## 1\n## 2\n## 3\n纸舟 12 7 Markdown 已成功节省100万元')
        self.assertTrue(check['required_literals_present'])
        self.assertEqual('unknown', check['task_outcome'])
        self.assertFalse(check['independent_quality_verified'])

    def test_chinese_section_titles_satisfy_brief_structure_without_hash_prefix(self):
        case = pilot.CASES[0]
        body = '纸舟 12 7 Markdown\n\n一、目标\n内容\n\n二、首版范围\n内容\n\n三、验收\n内容\n\n四、风险\n内容'
        check = pilot.structural_check(case, body)
        self.assertTrue(check['minimum_headings_present'])
        self.assertEqual('unknown', check['task_outcome'])
        self.assertFalse(check['independent_quality_verified'])

    def test_section_words_in_prose_or_numbered_facts_are_not_titles(self):
        body = '纸舟 12 7 Markdown\n目标包括以下范围和风险。\n1. 验收应检查事实\n2. 范围待确认\n3. 风险尚未分析'
        self.assertFalse(pilot.structural_check(pilot.CASES[0],body)['minimum_headings_present'])

    def test_plain_named_and_arabic_numbered_section_titles_are_supported(self):
        for body in ('目标\n范围\n验收标准\n风险', '1. 目标\n2. 首版范围\n3. 验收：\n4. 风险'):
            with self.subTest(body=body):
                check = pilot.structural_check(pilot.CASES[0],body + '\n纸舟 12 7 Markdown')
                self.assertTrue(check['minimum_headings_present'])
                self.assertFalse(check['independent_quality_verified'])

    def test_call_ceiling_counts_claims_before_process_start(self):
        # Keep the already frozen proposal valid while testing the execution guard.
        with patch('ai_supermarket.pilot.verify', return_value=pilot.definition()):
            with patch('ai_supermarket.pilot.MAX_CALLS', 2):
                self.error('pilot_limit', self.execute)
        self.assertEqual(2, len(self.contexts))
        self.assertFalse((self.pack / 'execution/call-03.json').exists())

    def test_remaining_wait_is_bounded_by_overall_deadline(self):
        model = PilotModel(self.contexts)
        original = model.respond
        def respond(role, context):
            self.assertGreater(model.timeout, 0)
            self.assertLessEqual(model.timeout, 2)
            return original(role, context)
        with patch.object(model, 'respond', side_effect=respond):
            with patch('ai_supermarket.pilot.verify', return_value=pilot.definition()):
                with patch('ai_supermarket.pilot.MAX_SECONDS', 2):
                    # Stop after the first response so wall-clock test scheduling cannot
                    # accidentally turn this contract check into a timing benchmark.
                    with patch('ai_supermarket.pilot.structural_check', side_effect=RuntimeError('test stop')):
                        with self.assertRaisesRegex(RuntimeError, 'test stop'):
                            pilot.execute(self.pack, self.hash, 'test', evidence_mode='synthetic', model_factory=lambda: model)
        self.assertEqual(1, len(self.contexts))

    def test_failure_record_disk_error_does_not_hide_provider_exception(self):
        write = pilot.write_new
        def fail_only_failure(path, data):
            if Path(path).name == 'failure.json':
                raise OSError('disk full')
            return write(path, data)
        with patch('ai_supermarket.pilot.write_new', side_effect=fail_only_failure):
            with patch.object(PilotModel, 'respond', side_effect=TimeoutError('original provider timeout')):
                with self.assertRaisesRegex(TimeoutError, 'original provider timeout'):
                    self.execute()
        self.error('pilot_already_started', self.execute)

    def test_process_timeout_diagnostic_is_saved_without_releasing_unknown(self):
        raw = b'{"type":"turn.started","private":"account-secret"}\n'
        with patch('steward.codex_model.codex_path', return_value='codex.exe'):
            with patch('steward.codex_model.subprocess.run', side_effect=subprocess.TimeoutExpired(
                    'private-command', 60, output=raw, stderr=b'private-stderr')) as run:
                with self.assertRaises(ModelUnavailable):
                    pilot.execute(self.pack, self.hash, 'synthetic-test-approval', evidence_mode='synthetic',
                                  model_factory=lambda: CodexModel(runtime=self.root / 'runtime'))
        self.assertEqual(1, run.call_count)
        failure = json.loads((self.pack / 'execution/failure.json').read_text(encoding='utf-8'))
        self.assertEqual('model_timeout', failure['error_code'])
        self.assertEqual('turn.started', failure['model_diagnostic']['last_control_event'])
        self.assertNotIn('private', json.dumps(failure))
        self.assertNotIn('account-secret', json.dumps(failure))
        meter = pilot.TaskMeter.existing(self.pack / 'execution/measurement.sqlite', 'brief-direct', 'pilot-host')
        self.assertEqual(1, meter.report()['unknown_calls'])
        self.assertIsNone(meter.report()['total_cost_micro_cny'])
        budget = pilot.BudgetLedger(self.pack / 'execution/budget.sqlite').scope('pilot', 'brief-direct', 'pilot-host')
        self.assertEqual(1_000_000, budget.summary()['held_micro_cny'])
        self.error('pilot_already_started', self.execute)

    def test_unstructured_model_error_never_persists_its_message(self):
        with patch.object(PilotModel, 'respond', side_effect=ModelUnavailable('private-provider-message')):
            with self.assertRaises(ModelUnavailable):
                self.execute()
        failure = json.loads((self.pack / 'execution/failure.json').read_text(encoding='utf-8'))
        self.assertEqual('execution_error', failure['error_code'])
        self.assertNotIn('model_diagnostic', failure)
        self.assertNotIn('private', json.dumps(failure))


if __name__ == '__main__':
    unittest.main()
