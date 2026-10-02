"""来源：本项目原创。宿主演示的真实交付与工程检查范围验证。"""

import json
from pathlib import Path
import tempfile
import unittest

from ai_supermarket.evaluation import demo


class EvaluationTests(unittest.TestCase):
    def test_demo_creates_unique_outputs_and_reports_measurement_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            first = demo(folder)
            second = demo(folder)
            self.assertEqual(first['status'], 'complete')
            self.assertNotEqual(first['document'], second['document'])
            content = Path(first['document']).read_text(encoding='utf-8')
            self.assertNotIn('{{', content)
            self.assertIn('## 验证', content)
            report = json.loads(Path(first['report']).read_text(encoding='utf-8'))
            self.assertEqual(report['feedback']['status'], 'accepted')
            self.assertEqual(report['duplicate_feedback']['status'], 'duplicate')
            self.assertEqual(report['retrieval']['passed'], 24)
            self.assertIsNone(report['retrieval']['token_savings'])
            self.assertFalse(report['task_quality_verified'])
            self.assertEqual(report['model_calls'], 0)


if __name__ == '__main__':
    unittest.main()
