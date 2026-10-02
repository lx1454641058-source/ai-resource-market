"""本项目原创。可启动API接入包的本机CLI/HTTP验收，无真实模型、钱包或外部供应商。"""
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_supermarket.api_provider import ApiProviderClient
from ai_supermarket.common import MarketError
from tools import market_api_try as trial


class ApiTrialTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.directory=self.root/'trial'
        sockets=[socket.socket(),socket.socket()]
        for sock in sockets:sock.bind(('127.0.0.1',0))
        self.ports=[sock.getsockname()[1] for sock in sockets]
        for sock in sockets:sock.close()
        trial.prepare(self.directory,*self.ports)

    def run_cli(self,command,*args):
        return subprocess.run([sys.executable,'-X','utf8','-B',str(trial.ROOT/'tools/market_api_try.py'),
            command,'--directory',str(self.directory),*args],cwd=self.root,capture_output=True,text=True,encoding='utf-8',timeout=20)

    def start(self,command,port):
        proc=subprocess.Popen([sys.executable,'-X','utf8','-B',str(trial.ROOT/'tools/market_api_try.py'),
            command,'--directory',str(self.directory)],cwd=self.root,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def stop():
            if proc.poll() is None:proc.terminate()
            proc.communicate(timeout=8)
        self.addCleanup(stop)
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            if proc.poll() is not None:self.fail('service exited before ready')
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):return proc
            except OSError:time.sleep(.03)
        self.fail('service did not open its configured port')

    def test_prepare_check_and_no_model_or_payment_configuration(self):
        app,operations,marker=trial.application(self.directory)
        self.assertIsNone(app.purchases);self.assertEqual(trial.PROVIDER,operations.provider)
        self.assertEqual(0,app.gateway.inspect_binding(marker['key_id'])['budget']['limit_cents'])
        self.assertTrue((self.directory/'buyer.mcp.json').exists())
        result=self.run_cli('check');self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('local_configuration_only',json.loads(result.stdout)['scope'])
        self.assertNotIn('token',result.stdout);self.assertNotIn('key_hex',result.stdout)

    def test_reprepare_preserves_original_directory(self):
        original=(self.directory/'trial.json').read_bytes()
        with self.assertRaises(FileExistsError):trial.prepare(self.directory,*self.ports)
        self.assertEqual(original,(self.directory/'trial.json').read_bytes())

    def test_missing_database_is_not_recreated(self):
        path=self.directory/'operations.sqlite';saved=path.with_suffix('.saved');path.rename(saved)
        with self.assertRaises(MarketError):trial.application(self.directory)
        self.assertFalse(path.exists());self.assertTrue(saved.exists())

    def test_empty_database_is_not_initialized(self):
        path=self.directory/'quota.sqlite';path.rename(path.with_suffix('.saved'));path.touch()
        with self.assertRaises(MarketError):trial.application(self.directory)
        self.assertEqual(0,path.stat().st_size)

    def test_nonzero_budget_refused(self):
        with closing(sqlite3.connect(self.directory/'budget.sqlite')) as db:
            db.execute('UPDATE budgets SET limit_cents=100');db.commit()
        with self.assertRaises(MarketError):trial.application(self.directory)

    def test_modified_marker_refused(self):
        path=self.directory/'trial.json';value=json.loads(path.read_text(encoding='utf-8'))
        value['provider_port']=value['market_port'];path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(MarketError):trial.application(self.directory)

    def test_revoked_identity_is_refused_before_serve(self):
        app,_,marker=trial.application(self.directory)
        app.gateway.identities.revoke_key(marker['key_id'])
        with self.assertRaises(MarketError):trial.application(self.directory)

    def test_independent_provider_rejects_unreserved_operation(self):
        self.start('serve-provider',self.ports[1])
        private=json.loads((self.directory/'provider-key.json').read_text())
        client=ApiProviderClient(trial.PROVIDER,f'http://127.0.0.1:{self.ports[1]}/api',
            bytes.fromhex(private['key_hex']),trial.BINDING,['stock'])
        with self.assertRaises(MarketError):client.execute('invented-operation','stock',{'warehouse':'fictional'})
        with closing(sqlite3.connect(self.directory/'operations.sqlite')) as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM operations').fetchone()[0])

    def test_two_cli_services_to_actual_artifact_restart_does_not_reexecute(self):
        provider=self.start('serve-provider',self.ports[1]);self.start('serve-market',self.ports[0])
        first=self.run_cli('exercise');self.assertEqual(0,first.returncode,first.stderr+first.stdout)
        self.assertEqual('demonstrated',json.loads(first.stdout)['status'])
        report=(self.directory/'采购建议.md').read_text(encoding='utf-8')
        self.assertIn('补货3件',report);self.assertIn('未知',report)
        provider.terminate();provider.communicate(timeout=8);self.start('serve-provider',self.ports[1])
        again=self.run_cli('exercise');self.assertEqual(0,again.returncode,again.stderr+again.stdout)
        recovered=self.run_cli('exercise','--recover');self.assertEqual(0,recovered.returncode,recovered.stderr+recovered.stdout)
        self.assertEqual(report,(self.directory/'采购建议.md').read_text(encoding='utf-8'))
        with closing(sqlite3.connect(self.directory/'operations.sqlite')) as db:
            self.assertEqual([('completed',1)],db.execute('SELECT state,COUNT(*) FROM operations GROUP BY state').fetchall())
        with closing(sqlite3.connect(self.directory/'quota.sqlite')) as db:
            self.assertEqual([('completed',1)],db.execute('SELECT state,COUNT(*) FROM calls GROUP BY state').fetchall())

    def test_user_report_preserved_on_conflict(self):
        self.start('serve-provider',self.ports[1]);self.start('serve-market',self.ports[0])
        report=self.directory/'采购建议.md';report.write_text('用户已有成果',encoding='utf-8')
        failed=self.run_cli('exercise');self.assertEqual(1,failed.returncode)
        self.assertEqual('用户已有成果',report.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
