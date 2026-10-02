"""来源：本项目原创。SQLite一致性备份，只新增快照，不打包凭据。"""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import uuid
import zipfile


def backup(app, directory):
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    name = 'steward-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.zip'
    output = destination / name
    with tempfile.TemporaryDirectory(prefix='backup-', dir=app.db_path.parent) as temp:
        snapshot = Path(temp) / 'personal-steward.sqlite'
        database = sqlite3.connect(snapshot)
        try:
            app.db.backup(database)
            if database.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('备份完整性检查未通过')
        finally:
            database.close()
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, 'personal-steward.sqlite')
            archive.writestr('backup-info.json', json.dumps({'version': 1, 'credentials_included': False,
                'note': '包含个人对话与API地址配置；恢复后请检查权限并重新保存密钥。成果正文在数据库内。'}, ensure_ascii=False))
    return output


def restore(archive_path, directory):
    """恢复到新的独立目录；不覆盖当前账户数据，也不恢复发送授权。"""
    with zipfile.ZipFile(archive_path) as archive:
        if sorted(archive.namelist()) != ['backup-info.json', 'personal-steward.sqlite']:
            raise ValueError('不是支持的总管备份文件')
        if archive.getinfo('personal-steward.sqlite').file_size > 100_000_000 or archive.getinfo('backup-info.json').file_size > 10000:
            raise ValueError('备份超过本版本100MB恢复限制')
        info = json.loads(archive.read('backup-info.json'))
        if info.get('version') != 1 or info.get('credentials_included') is not False:
            raise ValueError('备份格式版本不支持')
        data = archive.read('personal-steward.sqlite')
    if not data.startswith(b'SQLite format 3\x00'):
        raise ValueError('备份数据库无效')
    folder = Path(directory)/('restored-'+uuid.uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    path = folder/'personal-steward.sqlite'
    path.write_bytes(data)
    database = sqlite3.connect(path)
    try:
        database.execute('PRAGMA trusted_schema=OFF')
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects', 'memories', 'events', 'limits'} <= tables or database.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('备份数据库结构或完整性校验失败；原数据未变')
    finally:
        database.close()
    from .core import Steward
    from .extensions import save_setting
    app = Steward(path)
    try:
        with app.transaction():
            app.event(None, 'context_sharing_consent', allowed=False, source='备份恢复后重新授权')
            save_setting(app, 'permissions', {'api': False, 'plugins': False, 'export': True})
            for project in app.projects():
                if project['status'] in ('planning', 'executing'):
                    project.update(status='paused', resume_state='ready' if project.get('workflow') or project['tasks'] else 'new')
                # 导出路径仍属于原账户；恢复后按当前目录重新导出。
                project.pop('files', None)
                for request in project.get('resource_requests', []):
                    if request['state'] in ('approved', 'uncertain'):
                        request['state'] = 'revoked'
                    request.pop('inflight', None)
                app.save(project)
    finally:
        app.close()
    return folder
