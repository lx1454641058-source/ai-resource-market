"""来源：本项目原创。python -m ai_wallet 启动本机模拟环境。"""

import argparse
from contextlib import closing
import getpass
from pathlib import Path
import sqlite3
import webbrowser

from .engine import Wallet
from .allocation import AllocationWallet
from .server import LocalServer


def main():
    parser = argparse.ArgumentParser(description="AI 工作代付 · 本地模拟环境，不会真实扣款")
    parser.add_argument("--port", type=int, help="本机端口，0 表示自动选择；任务预算版默认8769，旧版8767")
    parser.add_argument("--database", type=Path, help="独立账本文件；不同预算模式不能混用")
    parser.add_argument('--task-budgets', action='store_true', help='任务预算版：先设置AI总额度，再确认AI提出的任务预算')
    parser.add_argument("--open", action="store_true", help="启动后用浏览器打开")
    parser.add_argument('--protected', action='store_true', help='本机受保护演示：启动时设置不回显的管理口令；仍无真实付款')
    args = parser.parse_args()
    if args.port is None:
        args.port = 8769 if args.task_budgets else 8767
    if args.database is None:
        args.database = Path(__file__).resolve().parent.parent / 'work' / 'ai-wallet' / ('task-budget-demo.sqlite' if args.task_budgets else 'demo.sqlite')
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0～65535 之间")
    try:
        # Read-only format check: never let the legacy engine reinterpret a new ledger.
        if not args.task_budgets and args.database.is_file():
            with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'settings' in tables and db.execute('SELECT 1 FROM settings WHERE id=11').fetchone():
                    parser.exit(1, 'This is a task-budget ledger. Restart with --task-budgets. Nothing changed.\n')
        password = None
        if args.protected:
            password = getpass.getpass('Local owner password (12-256 characters): ')
            if password != getpass.getpass('Repeat password: '):
                parser.exit(1, 'Passwords do not match. Nothing started.\n')
        wallet_type = AllocationWallet if args.task_budgets else Wallet
        server = LocalServer(args.port, wallet_type(args.database), owner_password=password)
        password = None
    except ValueError as exc:
        parser.exit(1, f'Unable to start: {exc}\n')
    except sqlite3.Error:
        parser.exit(1, 'Unable to read the local ledger. Original file was not replaced.\n')
    except OSError as exc:
        parser.exit(1, f"无法启动本机服务：{exc}\n可加 --port 0 自动选择空闲端口。\n")
    print(f"AI Wallet DEMO: {server.origin}", flush=True)
    print("SIMULATED payments and agents only. Ctrl+C to stop.", flush=True)
    if args.open:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
