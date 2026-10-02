"""来源：本项目原创。客户端从任意工作目录启动本仓库MCP适配器。"""
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ai_wallet.agent_mcp import main
    raise SystemExit(main())
