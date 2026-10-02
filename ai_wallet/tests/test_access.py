"""来源：本项目原创。只用虚构口令和临时账本验证本机会话。"""
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from ai_wallet.access import OwnerAccess
from ai_wallet.engine import Wallet, WalletError
from ai_wallet.server import LocalServer

PASSWORD = 'fixture-only-not-a-real-password'


class AccessTests(unittest.TestCase):
    def test_expiry_logout_and_forgery(self):
        now = [0]
        access = OwnerAccess(PASSWORD, clock=lambda: now[0])
        token = access.login(PASSWORD)
        cookie = 'wallet_owner=' + token
        access.require(cookie)
        for invalid in ('', 'wallet_owner=forged', 'other=' + token):
            with self.assertRaises(WalletError):
                access.require(invalid)
        now[0] = 1801
        with self.assertRaises(WalletError):
            access.require(cookie)
        cookie = 'wallet_owner=' + access.login(PASSWORD)
        access.logout(cookie)
        with self.assertRaises(WalletError):
            access.require(cookie)

    def test_absolute_expiry_despite_activity(self):
        now = [0]
        access = OwnerAccess(PASSWORD, clock=lambda: now[0])
        cookie = 'wallet_owner=' + access.login(PASSWORD)
        for step in range(1, 29):
            now[0] = step * 1000
            access.require(cookie)
        now[0] = 28800
        with self.assertRaises(WalletError):
            access.require(cookie)

    def test_login_throttle(self):
        now = [0]
        access = OwnerAccess(PASSWORD, clock=lambda: now[0])
        for _ in range(5):
            with self.assertRaises(WalletError) as error:
                access.login('wrong')
            self.assertEqual(error.exception.status, 401)
        with self.assertRaises(WalletError) as error:
            access.login(PASSWORD)
        self.assertEqual(error.exception.status, 429)
        now[0] = 61
        self.assertTrue(access.login(PASSWORD))

    def test_protected_http_requires_owner_before_state_and_mutations(self):
        with TemporaryDirectory() as folder:
            wallet = Wallet(Path(folder) / 'test.sqlite')
            server = LocalServer(0, wallet, owner_password=PASSWORD)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            def call(method, path, body=None, cookie='', csrf='', origin=None):
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                headers = {'Content-Type': 'application/json', 'Origin': origin or server.origin,
                           'Cookie': cookie, 'X-Wallet-Token': csrf}
                try:
                    connection.request(method, path, json.dumps(body or {}) if method == 'POST' else None, headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read()), dict(response.getheaders())
                finally:
                    connection.close()
            try:
                self.assertEqual(call('GET', '/api/state')[0], 401)
                self.assertEqual(call('POST', '/api/wallet/pause', csrf=server.csrf_token)[0], 401)
                self.assertEqual(call('POST', '/api/session/login', {'password': PASSWORD}, origin='https://outside.example')[0], 403)
                status, _, headers = call('POST', '/api/session/login', {'password': PASSWORD})
                self.assertEqual(status, 200)
                self.assertIn('HttpOnly', headers['Set-Cookie'])
                self.assertIn('SameSite=Strict', headers['Set-Cookie'])
                cookie = headers['Set-Cookie'].split(';')[0]
                status, state, _ = call('GET', '/api/state', cookie=cookie)
                self.assertEqual(status, 200)
                self.assertTrue(state['owner_protected'])
                self.assertEqual(call('POST', '/api/wallet/pause', cookie=cookie)[0], 403)
                self.assertEqual(call('POST', '/api/session/logout', cookie=cookie, csrf=state['csrf_token'])[0], 200)
                self.assertEqual(call('GET', '/api/state', cookie=cookie)[0], 401)
                self.assertTrue(wallet.state()['policy']['enabled'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
