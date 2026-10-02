"""来源：本项目原创。仅供本机测试：业务返回后、HTTP响应前终止本测试进程。"""

import argparse
import json
import os
from pathlib import Path

from ai_supermarket.http_app import MarketHTTP


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('database', 'identities', 'budget', 'measurement', 'fault-marker'):
        parser.add_argument('--' + field, type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--fault-tool', choices=('market_acquire', 'market_read'), required=True)
    args = parser.parse_args()
    app = MarketHTTP(args.database, args.identities, allowed_hosts=[f'127.0.0.1:{args.port}'],
                     budget_database=args.budget, measurement_database=args.measurement,
                     enforce_task_budget=True)
    original = app.gateway.call

    def crash_after_commit(token, tool, arguments):
        result = original(token, tool, arguments)
        expected = 'acquired' if tool == 'market_acquire' else 'read'
        if tool == args.fault_tool and result.get('status') == expected:
            # This durable marker contains no credentials or content. The actual business
            # method has returned; the ASGI layer has not received a result to send yet.
            with args.fault_marker.open('x', encoding='utf-8') as handle:
                json.dump({'tool': tool, 'committed_status': expected}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os._exit(91)
        return result

    app.gateway.call = crash_after_commit
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, http='h11', ws='none',
                proxy_headers=False, access_log=False, log_level='critical', lifespan='on')


if __name__ == '__main__':
    main()
