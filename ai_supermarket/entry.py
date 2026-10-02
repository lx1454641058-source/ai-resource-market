"""来源：本项目原创。允许AI客户端从任意工作目录启动本地MCP。"""

from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_supermarket.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main())

