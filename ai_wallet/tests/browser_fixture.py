"""来源：本项目原创。独立浏览器验收的临时商店宿主；仅DEMO数据，无真实资金。"""
import json
from pathlib import Path
import sys
import tempfile
import threading

from ai_wallet.checkout_lab import CheckoutLab, LabServer


def main():
    with tempfile.TemporaryDirectory(prefix='wallet-browser-fixture-') as folder:
        lab = CheckoutLab(Path(folder) / 'demo.sqlite', require_host_claim=True)
        servers = {shop: LabServer(lab, shop) for shop in lab.shops}
        for server in servers.values():
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for line in sys.stdin:
                command = json.loads(line)
                if command == {'action': 'close'}:
                    break
                if set(command) == {'action', 'shop', 'quantity'} and command['action'] == 'prepare':
                    shop = command['shop']
                    checkout = lab.prepare(shop, command['quantity'])
                    grant = lab.authorize_checkout(shop, checkout)
                    result = {'url': servers[shop].origin + '/checkout/' + checkout,
                              'amount_cents': grant['checkout']['amount_cents'], 'payment_ref': grant['payment_ref']}
                elif command == {'action': 'state'}:
                    state = lab.wallet.state()
                    result = {'spent_cents': state['allocation']['spent_cents'], 'charges': lab.charge_count,
                              'statuses': [o['status'] for o in state['orders']]}
                elif set(command) == {'action', 'shop', 'checkout_id', 'origin', 'amount_cents'} and command['action'] == 'claim':
                    result = {'allowed': lab.claim_for_browser(command['shop'], command['checkout_id'],
                              origin=command['origin'], amount_cents=command['amount_cents'])}
                else:
                    raise ValueError('Unsupported fixture command')
                print(json.dumps(result), flush=True)
        finally:
            for server in servers.values():
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    main()
