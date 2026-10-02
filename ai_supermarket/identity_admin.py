"""来源：本项目原创。仅受信本机运营使用；不向HTTP或模型暴露身份管理。"""

import argparse
import os
from pathlib import Path
import sqlite3
import sys

from .common import MarketError, compact, error_result, parse_json
from .identity import Identities


def issue_to_file(identities, principal, destination, ttl_seconds):
    """Reserve a new file before issuance; refuse overwrite and revoke a failed delivery."""
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    issued = None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            issued = identities.issue_key(principal, ttl_seconds)
            handle.write(compact({'token': issued['token']}) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if issued:
            identities.revoke_key(issued['key_id'])
        # Keep the failed output for operator inspection; never overwrite/delete another file.
        raise
    return {'status': 'issued', 'principal': principal, 'key_id': issued['key_id'],
            'expires_at': issued['expires_at'], 'credential_written': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='受信本机身份管理；不得作为公众或AI工具暴露')
    parser.add_argument('--identities', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    create = commands.add_parser('create-account')
    create.add_argument('--principal', required=True)
    create.add_argument('--role', choices=('buyer', 'seller', 'reviewer'), required=True)
    create.add_argument('--policy', type=Path)
    issue = commands.add_parser('issue-key')
    issue.add_argument('--principal', required=True)
    issue.add_argument('--output', type=Path, required=True)
    issue.add_argument('--ttl-seconds', type=int, default=2_592_000)
    revoke = commands.add_parser('revoke-key')
    revoke.add_argument('--key-id', required=True)
    disable = commands.add_parser('disable-account')
    disable.add_argument('--principal', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command != 'init' and not args.identities.is_file():
            raise MarketError('identity_not_initialized', '先由受信运营者初始化身份账本。')
        identities = Identities(args.identities)
        if args.command == 'init':
            result = {'status': 'initialized', 'accounts_created': False}
        elif args.command == 'create-account':
            policy = None
            if args.policy:
                with args.policy.open('rb') as handle:
                    raw = handle.read(16385)
                if len(raw) > 16384:
                    raise MarketError('invalid_size', '策略文件超过16KB。')
                policy = parse_json(raw)
            result = identities.create_account(args.principal, args.role, policy)
        elif args.command == 'issue-key':
            result = issue_to_file(identities, args.principal, args.output, args.ttl_seconds)
        elif args.command == 'revoke-key':
            result = identities.revoke_key(args.key_id)
        else:
            result = identities.disable_account(args.principal)
        status = 0
    except MarketError as exc:
        result, status = error_result(exc), 1
    except (OSError, sqlite3.Error):
        result, status = {'status': 'error', 'error_code': 'unavailable',
                          'message': '身份账本或新输出文件不可用；不会覆盖已有文件，请核对是否存在未完成签发。'}, 1
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
