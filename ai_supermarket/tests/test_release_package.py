"""来源：本项目原创。源码候选包、拒绝篡改与独立解压运行；不执行真实模型或支付。"""

from contextlib import closing
import http.client
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import warnings
import zipfile

from tools import market_release as release


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.report = release.build(self.root / 'build', 'internal-test')
        self.archive = Path(self.report['archive'])
        self.sha = self.report['sha256']

    def error(self, code, function, *args):
        with self.assertRaises(release.ReleaseError) as caught:
            function(*args)
        self.assertEqual(code, str(caught.exception))

    def changed_archive(self, name, mutate):
        with zipfile.ZipFile(self.archive) as original:
            entries = [(info, original.read(info)) for info in original.infolist()]
        changed = self.root / (name + '.zip')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(changed, 'x') as target:
                for info, body in mutate(entries):
                    target.writestr(info, body)
        return changed, release.digest(changed.read_bytes())

    def test_exact_allowlist_reproducible_archive_and_no_runtime_data(self):
        second = release.build(self.root / 'second', 'internal-test')
        self.assertEqual(self.sha, second['sha256'])
        result = release.verify(self.archive, self.sha)
        self.assertEqual('verified_internal_candidate', result['status'])
        self.assertFalse(result['distribution_approved'])
        self.assertFalse(result['code_executed'])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(release.PAYLOAD) | {release.MANIFEST}, set(archive.namelist()))
            for name in archive.namelist():
                self.assertFalse(any(part in ('work', 'tests', '__pycache__', '.git', '.env') for part in Path(name).parts))
                self.assertNotIn('.sqlite', name)
        self.assertEqual(77, len(release.MODULES))
        self.assertIn('ai_supermarket/asset_upload.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/alipay_agent.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/alipay-agent.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/restored_purchase.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/purchase_routing.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/purchase-connections.multi.example.json', release.PAYLOAD)
        self.assertIn('ai_supermarket/purchase_connections.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/delivery_recovery.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/purchase_recovery.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/payment_recovery.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/refund_recovery.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/commerce_recovery.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_connections.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/api-connections.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/provider_service.py', release.PAYLOAD)
        self.assertIn('tools/market_provider_service.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/provider-service.example.json', release.PAYLOAD)
        self.assertIn('ai_supermarket/provider_json.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/provider-json.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/payment_refunds.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/refund-reconciliation.md', release.PAYLOAD)
        self.assertIn('tools/market_operations.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/operations.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/alipay_refund_query.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/refund-query.md', release.PAYLOAD)
        self.assertIn('tools/market_api_try.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/api-trial.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/provider_operations.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/provider-operations.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_provider.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/api-provider.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_gateway.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_client.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/api-remote.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_product.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_delivery.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/api-product.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/api_quota.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/api-quota.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/remote_task.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/remote-task.md', release.PAYLOAD)
        self.assertIn('tools/market_remote_try.py', release.PAYLOAD)
        self.assertIn('tools/market_supplier_batch.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/supplier-batch.md', release.PAYLOAD)
        self.assertIn('tools/market_supplier_sync.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/integration/supplier-sync.md', release.PAYLOAD)
        self.assertIn('ai_supermarket/payment_bills.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/purchase_host.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/alipay_fulfillment.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/machine_pay.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/payment_fulfillment.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/alipay_transport.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/alipay_proof.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/requirements-payment.txt', release.PAYLOAD)
        self.assertIn('ai_supermarket/payment_orders.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/commission_agreements.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/_commission.py', release.PAYLOAD)
        self.assertIn('ai_supermarket/origin.py', release.PAYLOAD)
        self.assertEqual(32, len(release.RESOURCES))

    def test_extract_into_new_directory_preserves_all_payload_and_refuses_existing_target(self):
        destination = self.root / 'extracted'
        result = release.extract(self.archive, self.sha, destination)
        self.assertEqual('extracted_internal_candidate', result['status'])
        payload, manifest = release.load_verified(self.archive, self.sha)
        for name, raw in payload.items():
            self.assertEqual(raw, (destination / name).read_bytes())
        self.assertEqual(manifest, json.loads((destination / release.MANIFEST).read_bytes()))
        with self.assertRaises(FileExistsError):
            release.extract(self.archive, self.sha, destination)
        with self.assertRaises(FileExistsError):
            release.build(self.root / 'build', 'internal-test')
        self.assertEqual(self.sha, release.digest(self.archive.read_bytes()))

    def test_expected_outer_digest_is_required_before_any_extraction(self):
        self.error('expected_digest_required', release.verify, self.archive, 'not-a-digest')
        destination = self.root / 'never-created'
        self.error('archive_digest_mismatch', release.extract, self.archive, '0' * 64, destination)
        self.assertFalse(destination.exists())

    def test_changed_payload_is_rejected_even_if_outer_digest_is_supplied(self):
        def alter(entries):
            return [(info, body + b'\n# changed\n' if info.filename == 'ai_supermarket/common.py' else body)
                    for info, body in entries]
        changed, checksum = self.changed_archive('changed', alter)
        self.error('file_digest_mismatch', release.verify, changed, checksum)

    def test_missing_extra_traversal_duplicate_and_symlink_members_are_refused(self):
        def linked(entries):
            info, body = entries[0]
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            return [(info, body), *entries[1:]]
        for name, transform, code in [
            ('missing', lambda rows: rows[1:], 'unexpected_archive_members'),
            ('extra', lambda rows: rows + [(zipfile.ZipInfo('work/credentials.json'), b'fixture')], 'unexpected_archive_members'),
            ('traversal', lambda rows: rows + [(zipfile.ZipInfo('../escaped.txt'), b'fixture')], 'unexpected_archive_members'),
            ('duplicate', lambda rows: rows + [rows[0]], 'unexpected_archive_members'),
            ('linked', linked, 'unsafe_archive_member')]:
            with self.subTest(name=name):
                changed, checksum = self.changed_archive(name, transform)
                destination = self.root / ('extract-' + name)
                self.error(code, release.extract, changed, checksum, destination)
                self.assertFalse(destination.exists())
        self.assertFalse((self.root / 'escaped.txt').exists())

    def test_distribution_flags_and_manifest_types_cannot_claim_approval(self):
        for field, value in [('distribution_approved', True), ('payment_enabled', True),
                             ('format', True), ('label', '../other')]:
            with self.subTest(field=field):
                def alter(entries):
                    result = []
                    for info, body in entries:
                        if info.filename == release.MANIFEST:
                            manifest = json.loads(body)
                            manifest[field] = value
                            body = release.encode(manifest)
                        result.append((info, body))
                    return result
                changed, checksum = self.changed_archive(field, alter)
                self.error('invalid_manifest', release.verify, changed, checksum)

    def test_missing_source_or_wrong_catalog_stops_before_output(self):
        source = self.root / 'source'
        source.mkdir()
        for name in release.PAYLOAD:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(release.ROOT / name, target)
        # Deliberately unrelated local data must not enter the fixed allowlist.
        (source / 'private.fixture').write_text('not-for-release', encoding='utf-8')
        result = release.build(self.root / 'clean-copy', 'internal-test', source)
        self.assertEqual(self.sha, result['sha256'])
        body = source / 'ai_supermarket/catalog/content/product-brief.md'
        body.write_text('altered local test content', encoding='utf-8')
        self.error('catalog_mismatch', release.build, self.root / 'bad-catalog', 'internal-test', source)
        self.assertFalse((self.root / 'bad-catalog').exists())
        self.error('missing_or_oversized_source', release.build, self.root / 'missing', 'internal-test', self.root)
        self.assertFalse((self.root / 'missing').exists())

    def test_duplicate_manifest_json_field_is_refused(self):
        def alter(entries):
            return [(info, body.replace(b'"format": 1', b'"format": 0, "format": 1')
                     if info.filename == release.MANIFEST else body) for info, body in entries]
        changed, checksum = self.changed_archive('duplicate-json', alter)
        self.error('duplicate_json_field', release.verify, changed, checksum)

    def test_archive_path_replacement_cannot_substitute_already_verified_bytes(self):
        original = zipfile.ZipFile
        replaced = []
        def replace_after_read(file, *args, **kwargs):
            self.archive.write_bytes(b'changed-after-verified-read')
            replaced.append(True)
            return original(file, *args, **kwargs)
        with patch.object(release.zipfile, 'ZipFile', side_effect=replace_after_read):
            result = release.verify(self.archive, self.sha)
        self.assertEqual([True], replaced)
        self.assertEqual('verified_internal_candidate', result['status'])
        self.error('archive_digest_mismatch', release.verify, self.archive, self.sha)

    def test_oversized_archive_and_source_are_rejected(self):
        large = self.root / 'oversized.zip'
        large.write_bytes(b'x' * (release.MAX_TOTAL + 1))
        self.error('oversized_archive', release.verify, large, '0' * 64)
        source = self.root / 'oversized-source'
        source.mkdir()
        (source / 'data.py').write_bytes(b'x' * (release.MAX_FILE + 1))
        self.error('missing_or_oversized_source', release.source_bytes, source, 'data.py')


@unittest.skipUnless(importlib.util.find_spec('uvicorn'), '解压后独立网络验证需要既有HTTP依赖')
class ExtractedPackageTests(unittest.TestCase):
    def test_isolated_imports_cli_pilot_prepare_and_real_http_from_extracted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = release.build(root / 'build', 'internal-smoke')
            package = root / 'package'
            release.extract(result['archive'], result['sha256'], package)
            foreign = root / 'unrelated-cwd'
            foreign.mkdir()
            launcher = ('import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); '
                        'module=sys.argv.pop(1); sys.argv[0]=module; runpy.run_module(module,run_name="__main__")')

            def command(module, *arguments):
                return [sys.executable, '-I', '-B', '-c', launcher, str(package), module, *map(str, arguments)]

            def run(module, *arguments):
                execution = subprocess.run(command(module, *arguments), cwd=foreign, capture_output=True, timeout=15)
                self.assertEqual(0, execution.returncode, '解压包中的独立命令失败')
                self.assertFalse(execution.stderr, '解压包命令出现意外错误输出')
                return json.loads(execution.stdout)

            source_probe = '''import importlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
names=json.loads(sys.argv[2])
for name in names:
    module=importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(root), 'Workspace import leaked into package test'
from steward.codex_model import schema_for
assert schema_for('chat') == schema_for('project_manager')
print(json.dumps({'modules':len(names),'outside_workspace':True,'model_called':False}))
'''
            names = ['ai_supermarket.' + name for name in release.MODULES if name != '__init__'] + ['steward.codex_model', 'tools.market_seller', 'tools.market_supplier_batch', 'tools.market_supplier_sync', 'tools.market_supplier_receipts', 'tools.market_api_try','tools.market_provider_pair', 'tools.market_operations']
            probe = subprocess.run([sys.executable, '-I', '-B', '-c', source_probe, str(package), json.dumps(names)],
                                   cwd=foreign, capture_output=True, timeout=15)
            self.assertEqual(0, probe.returncode, '包内模块无法独立导入')
            self.assertEqual(len(names), json.loads(probe.stdout)['modules'])
            api_trial=root/'api-trial'
            self.assertEqual('prepared',run('tools.market_api_try','prepare','--directory',api_trial)['status'])
            self.assertEqual('checked',run('tools.market_api_try','check','--directory',api_trial)['status'])
            provider_pair=root/'provider-pair'
            self.assertEqual('prepared',run('tools.market_provider_pair','prepare','--directory',provider_pair)['status'])
            self.assertEqual('checked',run('tools.market_provider_pair','check-market','--directory',provider_pair/'market')['status'])
            self.assertEqual('checked',run('tools.market_provider_pair','check-provider','--directory',provider_pair/'provider')['status'])
            self.assertEqual(32, run('ai_supermarket', 'check-catalog')['resources'])
            sample_path = root / 'seller-request.json'
            self.assertEqual('sample_written', run('tools.market_seller', 'sample', '--output', sample_path)['status'])
            checked = run('tools.market_seller', 'check', '--input', sample_path, '--seller', 'vendor')
            self.assertEqual('checked', checked['status'])
            self.assertFalse(checked['publication_approved'])
            self.assertFalse(checked['server_state_checked'])
            prepared = run('ai_supermarket.pilot', 'prepare', '--directory', root / 'trial')
            self.assertEqual(0, prepared['real_model_calls'])
            self.assertFalse((root / 'trial/execution').exists())
            identities, market, key = root / 'identities.sqlite', root / 'market.sqlite', root / 'buyer.json'
            self.assertEqual('initialized', run('ai_supermarket.identity_admin', '--identities', identities, 'init')['status'])
            run('ai_supermarket.identity_admin', '--identities', identities, 'create-account', '--principal', 'buyer', '--role', 'buyer')
            run('ai_supermarket.identity_admin', '--identities', identities, 'issue-key', '--principal', 'buyer', '--output', key)
            diagnosis = run('ai_supermarket.exchange_cli', '--database', market, '--principal', 'operator', '--role', 'reviewer', 'diagnose')
            self.assertEqual(0, diagnosis['active_offers'])
            with closing(socket.socket()) as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen(command('ai_supermarket.http_server', '--database', market,
                            '--identities', identities, '--port', port), cwd=foreign,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready = False
                deadline = time.monotonic() + 8
                while process.poll() is None and time.monotonic() < deadline:
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                    try:
                        conn.request('GET', '/health/ready')
                        response = conn.getresponse()
                        ready = response.status == 200 and json.loads(response.read()) == {'status': 'ready'}
                        if ready:
                            break
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.02)
                    finally:
                        conn.close()
                self.assertTrue(ready, '包内HTTP服务未就绪')
                tools = run('ai_supermarket.remote_cli', '--url', f'http://127.0.0.1:{port}',
                            '--credential-file', key, 'describe')
                self.assertEqual('buyer', tools['role'])
                self.assertEqual(7, len(tools['tools']))
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=8)
                self.assertFalse(stdout or stderr, '独立服务器出现意外输出')
            actual = {path.relative_to(package).as_posix() for path in package.rglob('*') if path.is_file()}
            self.assertEqual(set(release.PAYLOAD) | {release.MANIFEST}, actual)
            for name, expected in json.loads((package / release.MANIFEST).read_bytes())['files'].items():
                self.assertEqual(expected['sha256'], release.digest((package / name).read_bytes()))


if __name__ == '__main__':
    unittest.main()
