"""来源：本项目原创。受信运营签发邀请到新文件，不发送邀请或输出秘密。"""

import argparse
import os
from pathlib import Path
import sqlite3
import sys

from .common import MarketError, compact, error_result
from .enrollment import Enrollments


def issue_to_file(enrollments, role, destination, ttl_seconds=86_400, key_ttl_seconds=2_592_000):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    issued = None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            issued = enrollments.issue(role, ttl_seconds, key_ttl_seconds)
            handle.write(compact({'invitation': issued['invitation']}) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if issued:
            enrollments.revoke(issued['invitation_id'])
        raise
    return {'status': 'issued', 'invitation_id': issued['invitation_id'], 'role': role,
            'expires_at': issued['expires_at'], 'invitation_written': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='受信本机邀请管理；不向模型提供管理工具')
    parser.add_argument('--enrollments', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    issue = commands.add_parser('issue')
    issue.add_argument('--role', choices=('buyer', 'seller'), required=True)
    issue.add_argument('--output', type=Path, required=True)
    issue.add_argument('--ttl-seconds', type=int, default=86_400)
    issue.add_argument('--key-ttl-seconds', type=int, default=2_592_000)
    revoke = commands.add_parser('revoke')
    revoke.add_argument('--invitation-id', required=True)
    args = parser.parse_args(argv)
    try:
        enrollments = Enrollments(args.enrollments, args.identities, initialize=args.command == 'init')
        if args.command == 'init':
            result = {'status': 'initialized', 'accounts_created': False}
        elif args.command == 'issue':
            result = issue_to_file(enrollments, args.role, args.output, args.ttl_seconds, args.key_ttl_seconds)
        else:
            result = enrollments.revoke(args.invitation_id)
        status = 0
    except MarketError as exc:
        result, status = error_result(exc), 1
    except (OSError, sqlite3.Error):
        result, status = {'status': 'error', 'error_code': 'unavailable',
                          'message': '入驻账本或新文件不可用；核原邀请状态，不会覆盖已有文件。'}, 1
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
