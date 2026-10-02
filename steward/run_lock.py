"""来源：本项目原创。操作系统文件锁；进程退出释放，不以时间推断长期任务死亡。"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


class RunBusy(ValueError):
    pass


@contextmanager
def project_lock(db_path, pid):
    directory = Path(db_path).parent / 'run-locks'
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256((str(Path(db_path).resolve())+'|'+pid).encode()).hexdigest()
    path = directory / (name+'.lock')
    if directory.is_symlink() or directory.is_junction() or path.is_symlink():
        raise ValueError('运行锁目录不能是链接')
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RunBusy('该任务仍有执行器持有，请在原窗口查看或停止') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def recover(app, pid):
    """调用方必须持有锁；恢复只暂停，不重新调用模型或退还额度。"""
    with app.transaction():
        p = app.get(pid)
        if p.get('run_lock_version') == 1 and p['status'] in ('planning', 'executing'):
            p.update(status='paused', resume_state='ready' if p.get('workflow') or p['tasks'] else 'new',
                     error=None, last_result={'reason': '上次执行器已退出，已保存进度；点击继续由主管接续。'})
            p.pop('error_message', None)
            p.pop('operation', None)
            app.save(p)
            app.event(pid, 'interrupted_run_recovered', reason='已取得原执行器释放的系统锁')
        return p
