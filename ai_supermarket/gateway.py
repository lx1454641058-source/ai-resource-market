"""来源：本项目原创。凭据绑定身份的应用网关；供服务传输层使用，不是公网服务器。"""

import sqlite3

from .common import MarketError, error_result
from .exchange import Exchange
from .exchange_tools import ExchangeInterface
from .identity import Identities


class AuthenticatedGateway:
    def __init__(self, market_database, identity_database, clock=None):
        self.database = market_database
        self.identities = Identities(identity_database, clock=clock)
        self.clock = clock

    def _interface(self, token):
        identity = self.identities.authenticate(token)
        market = Exchange(self.database, identity['principal'], identity['role'],
                          clock=self.clock, host_policy=identity['policy'])
        return ExchangeInterface(market)

    def call(self, token, tool, arguments):
        try:
            return self._interface(token).call(tool, arguments)
        except MarketError as exc:
            return error_result(exc)
        except (OSError, sqlite3.Error):
            return {'status': 'error', 'error_code': 'unavailable', 'message': '身份或市场服务暂不可用。'}

    def tool_list(self, token):
        interface = self._interface(token)
        result = {'tools': interface.tool_list(), 'instructions': interface.instructions,
                  'role': interface.market.role, 'version': '0.3.0'}
        if interface.market.role == 'seller':
            result['seller_principal'] = interface.market.principal
        return result
