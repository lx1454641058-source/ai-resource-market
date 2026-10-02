"""本项目原创。接入者CLI成果与收货验收；原目录、真实本机服务，无模型或钱包。"""
from contextlib import closing,redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from ai_supermarket.common import MarketError
from ai_supermarket.http_client import RemoteMarket
from ai_supermarket.tests import test_api_try as fixture
from tools import market_api_try as trial


class ApiAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApiTrialTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.directory=self.f.directory

    def services(self):
        self.f.start('serve-provider',self.f.ports[1]);self.f.start('serve-market',self.f.ports[0])

    def cli(self,command,*args,exit_code=0):
        reply=self.f.run_cli(command,*args)
        self.assertEqual(exit_code,reply.returncode,reply.stdout+reply.stderr)
        return json.loads(reply.stdout)

    def records(self):
        with closing(sqlite3.connect(self.directory/'market.sqlite')) as db:
            return db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'api_client_receipt:%'").fetchone()[0]

    def test_complete_cli_report_and_original_receipt_repeated(self):
        self.services()
        result=self.cli('exercise','--acknowledge')
        self.assertEqual('demonstrated',result['status'])
        receipt=result['receipt'];self.assertEqual('api_receipt_recorded',receipt['status'])
        self.assertEqual(receipt,self.cli('receipt-status'))
        self.assertEqual(receipt,self.cli('receipt'))
        self.assertEqual(1,self.records())
        self.assertIn('补货3件',Path(result['artifact']).read_text(encoding='utf-8'))
        with closing(sqlite3.connect(self.directory/'operations.sqlite')) as db:
            self.assertEqual([('completed',1)],db.execute('SELECT state,COUNT(*) FROM operations GROUP BY state').fetchall())
        self.assertFalse(receipt['package_fulfillment_confirmed'])
        self.assertFalse(receipt['channel_fulfillment_confirmed'])

    def test_plain_exercise_does_not_implicitly_ack(self):
        self.services();reply=self.cli('exercise')
        self.assertNotIn('receipt',reply);self.assertEqual(0,self.records())
        status=self.cli('receipt-status')
        self.assertEqual('awaiting_api_receipt',status['status']);self.assertFalse(status['server_acknowledged'])
        self.assertEqual('api_receipt_recorded',self.cli('receipt')['status'])

    def test_no_original_result_no_discovery_or_new_task(self):
        with patch.object(RemoteMarket,'request',side_effect=AssertionError('不得发起发现请求')):
            with self.assertRaises(MarketError) as error:trial.receipt(self.directory)
        self.assertEqual('receipt_not_ready',error.exception.code)
        self.assertEqual([],list((self.directory/'downloads').iterdir()))
        failed=self.cli('receipt',exit_code=1)
        self.assertEqual('receipt_not_ready',failed['error_code'])
        self.assertIn('原目录',failed['next_action'])

    def test_ack_response_loss_preserves_artifact_and_queries_original(self):
        self.services();original=RemoteMarket.request
        def lost(remote,path,value=None):
            result=original(remote,path,value)
            if path=='/v1/api/receipt':raise MarketError('transport_unknown','secret-never-output')
            return result
        with patch.object(RemoteMarket,'request',new=lost):result=trial.exercise(self.directory,acknowledge=True)
        self.assertEqual('artifact_ready_receipt_pending',result['status'])
        self.assertTrue(Path(result['artifact']).is_file())
        self.assertNotIn('secret-never-output',json.dumps(result))
        self.assertEqual('api_receipt_recorded',self.cli('receipt-status')['status'])
        self.assertEqual(1,self.records())

    def test_conflicting_user_artifact_does_not_ack(self):
        self.services();report=self.directory/'采购建议.md';report.write_text('用户已修改',encoding='utf-8')
        failed=self.cli('exercise','--acknowledge',exit_code=1)
        self.assertEqual('output_conflict',failed['error_code'])
        self.assertEqual('用户已修改',report.read_text(encoding='utf-8'))
        self.assertEqual(0,self.records())

    def test_tampered_result_denied_before_receipt_network(self):
        self.services();self.cli('exercise')
        result=next((self.directory/'downloads').glob('api-result-*/result.json'))
        result.write_text('{"count":999}',encoding='utf-8')
        with patch.object(RemoteMarket,'request',side_effect=AssertionError('不得确认被修改的结果')):
            with self.assertRaises(MarketError):trial.receipt(self.directory)
        self.assertEqual(0,self.records())

    def test_cli_pending_exit_code_and_opaque_error(self):
        stdout=io.StringIO()
        with redirect_stdout(stdout),patch.object(trial,'exercise',return_value={'status':'artifact_ready_receipt_pending'}):
            code=trial.main(['exercise','--directory',str(self.directory),'--acknowledge'])
        self.assertEqual(2,code)
        stdout=io.StringIO()
        with redirect_stdout(stdout),patch.object(trial,'receipt',side_effect=MarketError('private-secret-code','private-secret-text')):
            code=trial.main(['receipt','--directory',str(self.directory)])
        self.assertEqual(1,code);self.assertNotIn('private-secret',stdout.getvalue())
        self.assertEqual('trial_failed',json.loads(stdout.getvalue())['error_code'])

    def test_generated_mcp_config_reuses_script_task_goal(self):
        self.services();self.cli('exercise','--acknowledge')
        config=json.loads((self.directory/'buyer.mcp.json').read_text(encoding='utf-8'))['mcpServers']['ai-supermarket-api-trial']
        calls=list((self.directory/'downloads').glob('task-*/calls/*.json'))
        lines=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'acceptance','version':'1'}}},
               {'jsonrpc':'2.0','method':'notifications/initialized'},
               {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}]
        proc=subprocess.run([config['command'],'-X','utf8','-B',*config['args']],
            input='\n'.join(json.dumps(x) for x in lines)+'\n',capture_output=True,text=True,encoding='utf-8',
            timeout=20,cwd=self.f.root)
        self.assertEqual(0,proc.returncode,proc.stdout+proc.stderr)
        replies=[json.loads(line) for line in proc.stdout.splitlines()]
        tools=next(x['result']['tools'] for x in replies if x.get('id')==2)
        self.assertEqual(8,len(tools))
        self.assertEqual(calls,list((self.directory/'downloads').glob('task-*/calls/*.json')))

    def test_legacy_short_goal_binding_is_preserved(self):
        from ai_supermarket.remote_task import RemoteTaskResources
        self.services()
        remote=RemoteMarket(f'http://127.0.0.1:{self.f.ports[0]}',self.directory/'buyer-credential.json')
        old=RemoteTaskResources(remote,trial.TASK,'仓库查询','personal_use',self.directory/'downloads',mode='api')
        before=(old.directory/'binding.json').read_bytes()
        self.assertEqual('demonstrated',self.cli('exercise','--acknowledge')['status'])
        self.assertEqual(before,(old.directory/'binding.json').read_bytes())

    def test_changed_goal_preserves_original_task(self):
        self.services();self.cli('exercise')
        binding=next((self.directory/'downloads').glob('task-*/binding.json'));original=binding.read_bytes()
        (self.directory/'goal.md').write_text('用户改成另一个任务',encoding='utf-8')
        with patch.object(RemoteMarket,'request',side_effect=AssertionError('不启动不同任务')):
            with self.assertRaises(MarketError):trial.receipt(self.directory)
        self.assertEqual(original,binding.read_bytes())


if __name__=='__main__':unittest.main()
