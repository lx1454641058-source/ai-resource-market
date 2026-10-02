"""来源：本项目原创。可选的本机口令会话；不是公网多用户身份系统。

参考 OWASP Session Management Cheat Sheet。只用于受保护本机验收。
会话在服务器重启、退出登录、闲置或绝对期限后失效。
"""
from collections import deque
from http.cookies import SimpleCookie, CookieError
import hashlib
import secrets
import threading
import time

from .engine import WalletError


class OwnerAccess:
    def __init__(self, password, clock=time.monotonic):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('本机管理口令应为 12～256 个字符')
        self._salt = secrets.token_bytes(16)
        self._digest = self._hash(password)
        self._sessions = {}
        self._attempts = deque()
        self._clock = clock
        self._lock = threading.Lock()

    def _hash(self, password):
        return hashlib.scrypt(password.encode('utf-8'), salt=self._salt, n=16384, r=8, p=1)

    @staticmethod
    def cookie_value(header):
        try:
            cookie = SimpleCookie()
            cookie.load(header or '')
            return cookie['wallet_owner'].value if 'wallet_owner' in cookie else ''
        except CookieError:
            return ''

    def login(self, password):
        with self._lock:
            now = self._clock()
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 5:
                raise WalletError('尝试过于频繁，请一分钟后再试', 'rate_limited', 429)
            self._attempts.append(now)
            if not isinstance(password, str) or len(password) > 256 or not secrets.compare_digest(self._hash(password), self._digest):
                raise WalletError('管理口令不正确', 'invalid_credentials', 401)
            self._sessions = {key: value for key, value in self._sessions.items()
                              if now - value[0] < 8 * 3600 and now - value[1] < 1800}
            if len(self._sessions) >= 8:
                raise WalletError('活动会话已达上限，请先退出其他会话或稍后重试', 'session_limit', 429)
            token = secrets.token_urlsafe(32)
            self._sessions[hashlib.sha256(token.encode()).digest()] = (now, now)
            return token

    def require(self, cookie_header, touch=True):
        token = self.cookie_value(cookie_header)
        digest = hashlib.sha256(token.encode()).digest()
        with self._lock:
            now = self._clock()
            item = self._sessions.get(digest)
            if not item or now - item[0] >= 8 * 3600 or now - item[1] >= 1800:
                self._sessions.pop(digest, None)
                raise WalletError('请先登录本机管理界面', 'login_required', 401)
            if touch:
                self._sessions[digest] = (item[0], now)

    def logout(self, cookie_header):
        token = self.cookie_value(cookie_header)
        with self._lock:
            self._sessions.pop(hashlib.sha256(token.encode()).digest(), None)
