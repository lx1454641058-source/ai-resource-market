"""来源：本项目原创。单进程Uvicorn启动器，默认仅本机，不更改部署或凭据配置。"""

import argparse
import ipaddress
from pathlib import Path
import sqlite3
import sys

from .common import MarketError
from .http_app import MarketHTTP


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI超市机器HTTP服务；采购需显式接入已有授权，不执行钱包支付')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--asset-uploads', action='store_true', help='允许已认证卖方提交PNG/WAV草稿；须已有素材库，不自动审核')
    parser.add_argument('--budget', type=Path, help='受信预算账本；默认仅就绪检查，配合--enforce-task-budget约束静态交付，不授权付款')
    parser.add_argument('--enforce-task-budget', action='store_true', help='静态资料read强制凭据任务绑定及共享预算，需--budget；不执行依赖')
    parser.add_argument('--enrollments', type=Path, help='可选已初始化邀请账本，启用受控自助入驻')
    parser.add_argument('--measurement', type=Path, help='可选计量账本，加入就绪及恢复批次核验')
    parser.add_argument('--api-connections', type=Path, help='已准备的API连接目录；需原预算及--enforce-task-budget，不启用付款')
    parser.add_argument('--purchase-connections', type=Path, help='已准备的采购连接目录；需原购买授权与任务预算，可交接账单及核验凭证，不调用钱包')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allowed-host', action='append', default=[])
    parser.add_argument('--tls-cert', type=Path)
    parser.add_argument('--tls-key', type=Path)
    args = parser.parse_args(argv)
    try:
        address = ipaddress.ip_address(args.host)
        if not 1 <= args.port <= 65535:
            raise ValueError('端口需为1～65535。')
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ValueError('证书和私钥路径必须同时指定。')
        if not address.is_loopback and (not args.tls_cert or not args.allowed_host):
            raise ValueError('非本机监听必须明确TLS证书和允许的Host。')
        literal = f'[{address}]' if address.version == 6 else str(address)
        hosts = args.allowed_host or [f'{literal}:{args.port}', f'localhost:{args.port}']
        if args.api_connections and (not args.enforce_task_budget or args.budget is None):
            raise ValueError('API连接必须使用原任务预算。')
        if args.purchase_connections and (not args.enforce_task_budget or args.budget is None):
            raise ValueError('采购连接必须使用原任务预算。')
        app = MarketHTTP(args.database, args.identities, allowed_hosts=hosts, budget_database=args.budget,
                         enrollment_database=args.enrollments, measurement_database=args.measurement,
                         enforce_task_budget=args.enforce_task_budget, asset_uploads=args.asset_uploads)
        if args.api_connections:
            from .api_connections import attach
            attach(app, args.api_connections)
        if args.purchase_connections:
            from .purchase_connections import attach as attach_purchases
            attach_purchases(app, args.purchase_connections)
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, workers=1, http='h11', ws='none',
                    proxy_headers=False, server_header=False, access_log=False, log_level='warning',
                    limit_concurrency=32, backlog=64, timeout_keep_alive=5, timeout_graceful_shutdown=30,
                    h11_max_incomplete_event_size=16384, lifespan='on',
                    ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
                    ssl_keyfile=str(args.tls_key) if args.tls_key else None)
        return 0
    except ImportError:
        print('网络服务依赖未安装；请在独立环境安装requirements-http.txt。', file=sys.stderr)
    except (ValueError, MarketError, OSError, sqlite3.Error):
        print('服务配置、账本或证书不可用；请核对本地配置，原始异常未输出。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
