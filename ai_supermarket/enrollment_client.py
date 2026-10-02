"""来源：本项目原创。买卖方本地准备凭据并领取邀请，无重试或秘密输出。"""

import argparse
import math
import os
from pathlib import Path
import re
import sys

from .common import MarketError, choice, compact, error_result, fields, identifier, parse_json
from .enrollment import INVITE_PATTERN
from .http_client import RemoteMarket
from .identity import new_credential


def prepare_credential(destination):
    # Persist before submitting. Lost HTTP responses can reuse this exact credential.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(compact({'token': new_credential()}) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return {'status': 'prepared', 'credential_written': True, 'account_created': False}


class RemoteEnrollment(RemoteMarket):
    def enroll(self, invitation_file):
        credential = self.credential()
        try:
            with Path(invitation_file).open('rb') as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError('size')
            value = parse_json(raw)
            fields(value, ('invitation',))
            invitation = value['invitation']
            if not isinstance(invitation, str) or not re.fullmatch(INVITE_PATTERN, invitation):
                raise ValueError('format')
        except (OSError, ValueError, MarketError):
            raise MarketError('invitation_unavailable', '邀请文件不可用；未发送入驻请求。') from None
        reply = self._send('/v1/enroll', {'invitation': invitation, 'credential': credential}, {})
        if reply.get('status') == 'error':
            # Never echo an arbitrary server message/body that might reflect the secrets.
            code = reply.get('error_code')
            if code not in ('enrollment_denied', 'enrollment_conflict', 'enrollment_inactive', 'not_ready',
                            'enrollment_recovery_required', 'unavailable', 'rate_limited', 'not_found'):
                code = 'enrollment_unknown'
            result = {'status': 'error', 'error_code': code,
                      'message': '入驻未确认；保留原邀请与凭据核验，不重新生成凭据。'}
            retry = reply.get('retry_after_seconds')
            if type(retry) is int and 0 <= retry <= 999999:
                result['retry_after_seconds'] = retry
            return result
        try:
            fields(reply, ('status', 'principal', 'role', 'key_id', 'expires_at'))
            choice(reply['status'], ('enrolled',), 'status')
            choice(reply['role'], ('buyer', 'seller'), 'role')
            identifier(reply['principal'], 'principal')
            expected_principal = 'member_' + invitation.split('.', 1)[0][4:]
            if reply['principal'] != expected_principal or reply['key_id'] != credential.split('.', 1)[0]:
                raise ValueError('binding')
            if type(reply['expires_at']) not in (int, float) or not math.isfinite(reply['expires_at']) or reply['expires_at'] <= 0:
                raise ValueError('expiration')
        except (MarketError, ValueError):
            raise MarketError('invalid_response', '入驻响应无法核验；保留原凭据查询，不重复开户。') from None
        return reply


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地凭据准备与受控自助入驻；邀请和凭据只从文件读取')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--credential-file', type=Path, required=True)
    enroll = commands.add_parser('enroll')
    enroll.add_argument('--url', required=True)
    enroll.add_argument('--credential-file', type=Path, required=True)
    enroll.add_argument('--invitation-file', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (prepare_credential(args.credential_file) if args.command == 'prepare'
                  else RemoteEnrollment(args.url, args.credential_file).enroll(args.invitation_file))
        status = 1 if result.get('status') == 'error' else 0
    except MarketError as exc:
        result, status = error_result(exc), 1
    except OSError:
        result, status = {'status': 'error', 'error_code': 'file_unavailable',
                          'message': '文件不可用；不会覆盖已有凭据或输出秘密。'}, 1
    sys.stdout.buffer.write(compact(result).encode('utf-8') + b'\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
