"""本项目原创。分目录真实CLI双端演练，供应商进程禁止读取市场目录。"""
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

from ai_supermarket.common import MarketError
from ai_supermarket.identity import Identities
from tools import market_provider_pair as pair
from tools import market_api_try as trial


class ProviderPairTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.directory=self.root/'pair';self.market=self.directory/'market';self.provider=self.directory/'provider'
        sockets=[socket.socket(),socket.socket()]
        for sock in sockets:sock.bind(('127.0.0.1',0))
        self.ports=[sock.getsockname()[1] for sock in sockets]
        for sock in sockets:sock.close()
        pair.prepare(self.directory,*self.ports)

    def command(self,command):
        directory=self.market if command.endswith('market') else self.provider
        return [sys.executable,'-X','utf8','-B',str(pair.ROOT/'tools/market_provider_pair.py'),command,'--directory',str(directory)]

    def cli(self,command,exit_code=0):
        proc=subprocess.run(self.command(command),cwd=self.root,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(exit_code,proc.returncode,proc.stdout+proc.stderr)
        return json.loads(proc.stdout)

    def start(self,command,port,deny_market=False):
        args=self.command(command)
        if deny_market:
            guard='''import os,sys,runpy
blocked=os.path.normcase(os.path.abspath(sys.argv.pop(1)))
tool=sys.argv.pop(1)
sys.path.insert(0,os.path.dirname(os.path.dirname(tool)))
def deny(event,args):
    if event in ('open','sqlite3.connect') and args and isinstance(args[0],(str,bytes,os.PathLike)):
        value=os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
        if value==blocked or value.startswith(blocked+os.sep):
            raise RuntimeError('provider attempted market filesystem access')
sys.addaudithook(deny)
runpy.run_path(tool,run_name='__main__')
'''
            args=[sys.executable,'-X','utf8','-B','-c',guard,str(self.market),args[4],*args[5:]]
        proc=subprocess.Popen(args,cwd=self.root,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def stop():
            if proc.poll() is None:proc.terminate()
            proc.communicate(timeout=8)
        self.addCleanup(stop);deadline=time.monotonic()+7
        while time.monotonic()<deadline:
            if proc.poll() is not None:self.fail('trial service exited before ready')
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):return proc
            except OSError:time.sleep(.03)
        self.fail('trial service did not open configured port')

    def exercise(self,*args,exit_code=0):
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(pair.ROOT/'tools/market_api_try.py'),
            'exercise','--directory',str(self.market),*args],cwd=self.root,capture_output=True,text=True,encoding='utf-8',timeout=25)
        self.assertEqual(exit_code,proc.returncode,proc.stdout+proc.stderr)
        return json.loads(proc.stdout)

    def count(self,directory):
        with closing(sqlite3.connect(directory/'operations.sqlite')) as db:
            return db.execute('SELECT state,COUNT(*) FROM operations GROUP BY state').fetchall()

    def test_provider_check_has_only_own_files_and_no_market_objects(self):
        expected={'provider.json','binding.json','provider-key.json','seller-credential.json','operations.sqlite'}
        self.assertEqual(expected,{p.name for p in self.provider.iterdir()})
        with (patch.object(trial,'application',side_effect=AssertionError('不得构建市场宿主')),
              patch.object(trial,'Exchange',side_effect=AssertionError('不得访问市场库'))):
            app,_,_=pair.provider_application(self.provider)
        self.assertFalse(hasattr(app,'gateway'));self.assertFalse(hasattr(app,'readiness_paths'))
        self.assertEqual('local_configuration_only',self.cli('check-provider')['scope'])
        self.assertEqual(expected,{p.name for p in self.provider.iterdir()})

    def test_two_processes_deliver_with_provider_market_reads_blocked(self):
        provider=self.start('serve-provider',self.ports[1],deny_market=True)
        self.start('serve-market',self.ports[0]);probe=self.cli('probe-provider')
        self.assertEqual('connected',probe['status']);self.assertFalse(probe['operation_authorized'])
        first=self.exercise('--acknowledge');self.assertEqual('api_receipt_recorded',first['receipt']['status'])
        self.assertIn('补货3件',Path(first['artifact']).read_text(encoding='utf-8'))
        provider.terminate();provider.communicate(timeout=8)
        self.start('serve-provider',self.ports[1],deny_market=True)
        recovered=self.exercise('--recover','--acknowledge')
        self.assertEqual(first['receipt'],recovered['receipt'])
        self.assertEqual([('completed',1)],self.count(self.provider));self.assertEqual([],self.count(self.market))

    def test_supplier_starts_without_market_but_probe_reports_failure(self):
        self.start('serve-provider',self.ports[1],deny_market=True)
        self.assertEqual('checked',self.cli('check-provider')['status'])
        self.assertEqual('transport_unknown',self.cli('probe-provider',exit_code=1)['error_code'])
        self.assertEqual([],self.count(self.provider))

    def test_missing_or_empty_provider_database_not_recreated(self):
        path=self.provider/'operations.sqlite';original=path.with_suffix('.saved');path.rename(original)
        with self.assertRaises(MarketError):pair.provider_application(self.provider)
        self.assertFalse(path.exists());path.touch()
        with self.assertRaises(MarketError):pair.provider_application(self.provider)
        self.assertEqual(0,path.stat().st_size)

    def test_market_checks_do_not_need_local_supplier_database(self):
        path=self.market/'operations.sqlite';path.rename(path.with_suffix('.saved'))
        self.assertEqual('checked',self.cli('check-market')['status']);self.assertFalse(path.exists())
        self.start('serve-market',self.ports[0]);self.start('serve-provider',self.ports[1],deny_market=True)
        self.assertEqual('demonstrated',self.exercise('--acknowledge')['status'])
        self.assertFalse(path.exists());self.assertEqual([('completed',1)],self.count(self.provider))

    def test_modified_provider_scope_and_secret_are_refused(self):
        path=self.provider/'provider.json';raw=path.read_bytes();config=json.loads(raw)
        config['market_url']='http://127.0.0.1:8888';path.write_text(json.dumps(config),encoding='utf-8')
        with self.assertRaises(MarketError):pair.provider_application(self.provider)
        path.write_bytes(raw)
        key=self.provider/'provider-key.json';key.write_text('{"key_hex":"'+'b'*64+'"}',encoding='utf-8')
        with self.assertRaises(MarketError):pair.provider_application(self.provider)

    def test_revoked_supplier_cannot_execute_or_probe(self):
        self.start('serve-market',self.ports[0]);self.start('serve-provider',self.ports[1],deny_market=True)
        config=json.loads((self.provider/'provider.json').read_text(encoding='utf-8'))
        Identities(self.market/'identities.sqlite').revoke_key(config['key_id'])
        self.assertEqual('error',self.cli('probe-provider',exit_code=1)['status'])
        result=self.exercise(exit_code=2);self.assertEqual('reserved',result['status'])
        self.assertEqual([],self.count(self.provider))

    def test_prepare_refuses_existing_directory_and_preserves_binding(self):
        before=(self.provider/'binding.json').read_bytes()
        with self.assertRaises(FileExistsError):pair.prepare(self.directory,*self.ports)
        self.assertEqual(before,(self.provider/'binding.json').read_bytes())


if __name__=='__main__':unittest.main()
