"""本项目原创。真实文件解析、不修改输入及受限输出验收。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from tools import market_data_inspect as tool


class DataInspectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_csv_real_parser_preserves_ids_multiline_and_reports_shape(self):
        path = self.root / 'input.csv'
        raw = 'id,note,amount\r\n0012,"hello,\nworld",\r\n0013,ok,2\r\n0014,short\r\n0015,=1+1,3\r\n'.encode()
        path.write_bytes(raw)
        report = tool.csv_report(path)
        self.assertEqual(4, report['rows'])
        self.assertEqual(1, report['row_width_mismatches'])
        self.assertEqual([0, 0, 1], report['empty_cells_by_column'])
        self.assertEqual([{'record': 5, 'column': 2}], report['formula_like_locations'])
        self.assertNotIn('0012', json.dumps(report))
        self.assertEqual(raw, path.read_bytes())

    def test_csv_duplicate_header_delimiter_and_bad_quote(self):
        path = self.root / 'input.csv'
        path.write_text('a;a;\n1;2;3\n', encoding='utf-8')
        report = tool.csv_report(path, ';')
        self.assertEqual(1, report['duplicate_header_count'])
        self.assertEqual(1, report['blank_header_count'])
        path.write_text('a,b\n1,"unfinished', encoding='utf-8')
        with self.assertRaises(tool.csv.Error):
            tool.csv_report(path)

    def test_strict_json_duplicates_numbers_and_no_value_echo(self):
        path = self.root / 'input.json'
        for body, reason in [(' {"a":1,"a":2}', 'duplicate_json_key'), ('[NaN]', 'nonstandard_json_number'),
                             ('{"x":}', 'invalid_json_syntax')]:
            path.write_text(body, encoding='utf-8')
            self.assertEqual(reason, tool.json_report(path)['reason'])
        path.write_text('{"s":"private-fixture-value","n":1e400,"a":[true,null]}', encoding='utf-8')
        result = tool.json_report(path)
        self.assertTrue(result['valid'])
        self.assertEqual(1, result['nodes_by_type']['number'])
        self.assertNotIn('private-fixture-value', json.dumps(result))

    def make_database(self):
        path = self.root / 'input.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE "odd""table" (id TEXT, amount REAL, note BLOB, generated TEXT GENERATED ALWAYS AS (upper(id)))')
            db.execute('INSERT INTO "odd""table" (id,amount,note) VALUES (?,?,?)', ('0012', 5, b'abc'))
            db.execute('INSERT INTO "odd""table" (id,amount,note) VALUES (?,?,?)', ('0013', float('inf'), b''))
            db.execute('CREATE VIEW hidden AS SELECT * FROM "odd""table"')
            db.commit()
        return path

    def test_extreme_json_exponent_returns_bounded_failure_without_traceback(self):
        path = self.root / 'exponent.json'
        path.write_text('1e999999999999999999999999999999999999999999', encoding='utf-8')
        result = tool.json_report(path)
        self.assertFalse(result['valid'])
        self.assertEqual('json_limit', result['reason'])
        process = subprocess.run([sys.executable, '-I', '-B', str(Path(tool.__file__).resolve()),
                                  'json', '--input', str(path)], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(1, process.returncode)
        self.assertEqual(b'', process.stderr)
        self.assertEqual('invalid', json.loads(process.stdout)['status'])

    def test_sqlite_schema_samples_are_bounded_and_original_unchanged(self):
        path = self.make_database()
        original = path.read_bytes()
        schema = tool.sqlite_report(path)
        self.assertEqual(1, schema['table_count'])
        self.assertNotIn('sample_rows', schema)
        report = tool.sqlite_report(path, 'odd"table', 1)
        self.assertEqual(['id', 'amount', 'note'], report['sample_columns'])
        self.assertEqual([['0012', 5.0, {'type': 'blob', 'bytes': 3}]], report['sample_rows'])
        self.assertTrue(report['sample_truncated'])
        self.assertEqual(original, path.read_bytes())
        self.assertEqual([path], list(self.root.iterdir()))
        json.dumps(tool.sqlite_report(path, 'odd"table'), allow_nan=False)

    def test_sqlite_views_unknown_and_sql_injection_not_queried(self):
        path = self.make_database()
        for table in ('hidden', 'missing', 'odd"table; DROP TABLE x;--'):
            with self.assertRaises(tool.InspectError):
                tool.sqlite_report(path, table)
        self.assertEqual(1, tool.sqlite_report(path)['table_count'])

    def test_live_sqlite_snapshot_refused_and_missing_file_not_created(self):
        path = self.make_database()
        (self.root / 'input.sqlite-wal').write_bytes(b'ongoing')
        with self.assertRaisesRegex(tool.InspectError, 'snapshot_required'):
            tool.sqlite_report(path)
        missing = self.root / 'absent.sqlite'
        with self.assertRaises(tool.InspectError):
            tool.sqlite_report(missing)
        self.assertFalse(missing.exists())

    def test_limits_oversize_json_depth_and_foreign_cwd_cli(self):
        path = self.root / 'input.json'
        path.write_bytes(b' ' * (tool.MAX_TEXT + 1))
        with self.assertRaisesRegex(tool.InspectError, 'input_too_large'):
            tool.json_report(path)
        path.write_text('[' * 130 + '0' + ']' * 130, encoding='utf-8')
        with self.assertRaisesRegex(tool.InspectError, 'json_structure_limit'):
            tool.json_report(path)
        path.write_text('[1,true,null]', encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        process = subprocess.run([sys.executable, '-I', '-B', str(Path(tool.__file__).resolve()),
                                  'json', '--input', str(path)], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual(digest, json.loads(process.stdout)['source']['sha256'])
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
