"""来源：本项目原创。交付包入口、诊断与界面启动验收。"""
import argparse
import json
import os
import sys

from . import __version__ as VERSION


def main():
    parser = argparse.ArgumentParser(description='个人总管')
    parser.add_argument('--data', help='独立用户数据目录')
    parser.add_argument('--check', action='store_true', help='检查本机依赖，不调用模型')
    parser.add_argument('--smoke-ui', action='store_true', help='启动真实界面并自动关闭，用于打包验证')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args()
    if args.data:
        os.environ['STEWARD_DATA_DIR'] = args.data
    if args.check:
        import sqlite3
        import ssl
        import tkinter
        from .codex_model import codex_path, ModelUnavailable
        try:
            codex_path()
            codex = True
        except ModelUnavailable:
            codex = False
        print(json.dumps({'version': VERSION, 'python': sys.version.split()[0],
                          'sqlite': sqlite3.sqlite_version, 'tk': tkinter.TkVersion,
                          'tls': ssl.OPENSSL_VERSION, 'codex_found': codex,
                          'login_verified': False, 'network_called': False}, ensure_ascii=False))
        return
    import tkinter as tk
    from .gui import DemoWindow
    root = tk.Tk()
    app = DemoWindow(root, personal=True)
    if args.smoke_ui:
        root.after(900, app.close)
    root.mainloop()


if __name__ == '__main__':
    main()
