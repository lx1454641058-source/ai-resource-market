"""来源：本项目原创。受信供应商宿主的本地批次留存示例，复用既有市场接口。

目录包含获准导出的正文与许可，应放在宿主私有工作区。connection_id由宿主
绑定实际市场与卖方凭据；它不是身份认证，也不能由模型或供应商目录指定。
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from ai_supermarket.common import MarketError, compact, fields, identifier, parse_json
from ai_supermarket.examples.supplier_bridge import MAX_FEED_BYTES, prepare_batch, submit_batch


def _path(value):
    path = Path(value).absolute()
    if str(path).startswith(('\\\\', '//')):
        raise MarketError('invalid_path', '批次需要本机普通目录。')
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise MarketError('invalid_path', '批次不使用链接路径。')
    return path


def _write(path, value):
    # Never replace an existing batch or receipt, including a partial failed write.
    with _path(path).open('xb') as output:
        output.write((compact(value) + '\n').encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())


def _read(path, limit=8192):
    with _path(path).open('rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise MarketError('invalid_path', '批次需要普通文件。')
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise MarketError('invalid_size', '批次或回执超过上限。')
    return parse_json(raw)


@contextmanager
def _locked(directory):
    # Keep the lock file; the OS releases the lock on close/process exit. A file's
    # existence alone is NOT evidence that another process is still running.
    with _path(directory / 'run.lock').open('a+b') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MarketError('invalid_path', '锁需要普通文件。')
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MarketError('job_busy', '本批次仍在执行，未启动新请求。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def create_job(requests, seller, connection_id, directory):
    """Freeze a validated batch before any client call. Retain returned digest separately."""
    identifier(connection_id, 'connection_id')
    rows = prepare_batch(requests, seller)
    envelope = {'format': 1, 'seller': seller, 'connection_id': connection_id, 'requests': rows}
    digest = hashlib.sha256(compact(envelope).encode('utf-8')).hexdigest()
    directory = _path(directory)
    directory.mkdir()  # Exclusive: existing work and incomplete jobs are never overwritten.
    _write(directory / 'batch.json', envelope)
    return {'status': 'prepared', 'batch_sha256': digest, 'items': len(rows),
            'market_calls': 0, 'publication_approved': False, 'payment_available': False}


def run_job(directory, expected_sha256, seller, connection_id, seller_client, *, resume_unknown=False):
    """Run one explicit attempt; no fetch, retries, approval or credential persistence.

    After an unknown outcome the trusted caller must inspect original requests and
    server state, then explicitly set resume_unknown. That flag is not proof that
    the inspection took place. The connection binding must remain stable across runs.
    """
    identifier(seller, 'seller')
    identifier(connection_id, 'connection_id')
    if (not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256)
            or type(resume_unknown) is not bool):
        raise MarketError('invalid_params', '需要原批次摘要及布尔恢复选项。')
    directory = _path(directory)
    with _locked(directory):
        batch = _read(directory / 'batch.json', MAX_FEED_BYTES + 1024)
        fields(batch, ('format', 'seller', 'connection_id', 'requests'))
        if (type(batch['format']) is not int or batch['format'] != 1
                or batch['seller'] != seller or batch['connection_id'] != connection_id
                or hashlib.sha256(compact(batch).encode('utf-8')).hexdigest() != expected_sha256):
            raise MarketError('batch_mismatch', '批次摘要或宿主连接绑定变化，未发送。')
        rows = prepare_batch(batch['requests'], seller)
        starts = sorted(directory.glob('attempt-*.started.json'))
        if len(starts) >= 1000:
            raise MarketError('attempt_limit', '本批次执行次数达到上限，需人工核查。')
        previous_unknown = False
        for index, start in enumerate(starts, 1):
            if start.name != f'attempt-{index:04d}.started.json':
                raise MarketError('invalid_journal', '批次执行记录不连续。')
            record = _read(start)
            if record != {'attempt': index, 'batch_sha256': expected_sha256}:
                raise MarketError('invalid_journal', '批次执行记录不匹配。')
            result_path = directory / f'attempt-{index:04d}.result.json'
            if not result_path.exists():
                previous_unknown = True
            else:
                result = _read(result_path)
                if (not isinstance(result, dict) or result.get('attempt') != index or result.get('batch_sha256') != expected_sha256
                        or result.get('status') not in ('submitted', 'stopped')):
                    raise MarketError('invalid_journal', '批次回执不匹配。')
                previous_unknown = result['status'] != 'submitted'
        if previous_unknown and not resume_unknown:
            raise MarketError('recovery_required', '上次结果未确认；先核原请求和服务器状态，再显式恢复。')
        attempt = len(starts) + 1
        binding = {'attempt': attempt, 'batch_sha256': expected_sha256}
        # Persist intent before the first network call. If result storage fails,
        # the next process sees an unknown attempt and must stop for inspection.
        _write(directory / f'attempt-{attempt:04d}.started.json', binding)
        result = {**submit_batch(rows, seller, seller_client), **binding}
        _write(directory / f'attempt-{attempt:04d}.result.json', result)
        return result
