"""来源：本项目原创。分发版数据置于当前用户目录，源码开发保留既有路径。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_directory():
    override = os.environ.get('STEWARD_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if (ROOT / '.steward-release').exists():
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'PersonalSteward'
    return ROOT / 'work'
