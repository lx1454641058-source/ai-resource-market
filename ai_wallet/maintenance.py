"""来源：本项目原创。本地账本备份/恢复；输出必须是新文件，绝不覆盖已有账本。"""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3


def copy_ledger(source, output):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if not source.is_file():
        raise ValueError('源账本不存在')
    if output.exists() or source == output:
        raise ValueError('输出文件已存在；请选择新的文件名，不会覆盖任何账本')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        if original.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('源账本完整性检查失败')
        tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'settings', 'orders', 'audit'} <= tables:
            raise ValueError('文件不是本产品的账本')
        # 查询固定字段检查版本形状，不执行导入文件中的脚本。
        original.execute('SELECT id, value FROM settings LIMIT 1')
        original.execute('SELECT id, request_key, request_hash, data FROM orders LIMIT 1')
        original.execute('SELECT id, at, actor, action, order_id, detail FROM audit LIMIT 1')
        output.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL 防止检查之后有人创建同名文件，避免覆盖。
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(output)) as target:
                original.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('副本完整性检查失败')
        except Exception:
            # 只清理由本次独占创建的未完成副本，不操作原账本。
            output.unlink(missing_ok=True)
            raise
    return output


def main():
    parser = argparse.ArgumentParser(description='将本地账本备份或恢复为新的文件，禁止覆盖')
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = copy_ledger(args.source, args.output)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'未完成：{exc}\n')
    print(f'Completed {args.action}: {result}')
    print('Original ledger unchanged. Use --database with the new path to inspect the copy.')


if __name__ == '__main__':
    main()
