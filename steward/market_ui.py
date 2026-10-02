"""本项目原创。当前任务的超市连接编辑器；不显示凭据、不联网或运行模型。"""
import tkinter as tk
from tkinter import filedialog, ttk

from .settings_ui import dialog


def market_connection(window):
    if not window.current or window.busy:
        window.notice.configure(text='先选择并停止一个任务，再配置超市连接。')
        return None
    try:
        from . import market_settings
        from .remote_market_gateway import project_scope
    except ModuleNotFoundError as exc:
        if exc.name != 'ai_supermarket' and not exc.name.startswith('ai_supermarket.'):
            raise
        window.notice.configure(text='当前程序未包含超市模块；请使用配套完整源码后配置连接。')
        return None
    pid = window.current
    project = window.app.get(pid)
    scope = project_scope(project)
    top, frame = dialog(window, '当前任务 · AI超市连接', '760x650')
    ttk.Label(frame, text='保存后，当前任务执行前会先检查超市，重启总管仍有效。\n需要已有市场账号和获准任务；这里不创建账号、付款或启动模型。', wraplength=700).pack(anchor='w')
    ttk.Label(frame, text='当前任务：' + project['goal'][:100], wraplength=700).pack(anchor='w', pady=7)
    values = {}
    for key, label in [('url', '市场地址'), ('credential_file', '私有凭据文件路径'), ('task_id', '市场已授权任务编号'), ('output_root', '已有结果目录')]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=label, width=23).pack(side='left')
        values[key] = tk.StringVar(master=top)
        ttk.Entry(row, textvariable=values[key]).pack(side='left', fill='x', expand=True)
        if key in ('credential_file', 'output_root'):
            def choose(field=key):
                path = (filedialog.askopenfilename(parent=top, title='选择私有凭据文件') if field == 'credential_file'
                        else filedialog.askdirectory(parent=top, title='选择已有结果目录'))
                if path:
                    values[field].set(path)
            ttk.Button(row, text='选择', command=choose).pack(side='right')
    for key, label, choices, default in [('mode', '模式', ('free_text', 'api', 'purchase'), 'free_text'),
                                         ('purpose', '实际用途', ('personal_use', 'commercial_use'), 'personal_use')]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=label, width=23).pack(side='left')
        values[key] = tk.StringVar(master=top, value=default)
        ttk.Combobox(row, values=choices, textvariable=values[key], state='readonly').pack(side='left', fill='x', expand=True)
    ttk.Label(frame, text='free_text：免费文本；api：免费/已有API权益；purchase：采购宿主。\n收费模式仍需可信宿主的钱包接续，保存连接不代表钱包已可用。', wraplength=700).pack(anchor='w', pady=7)
    row = ttk.Frame(frame)
    row.pack(fill='x')
    ttk.Label(row, text='任务网络尝试上限（2–100）', width=30).pack(side='left')
    values['max_calls'] = tk.StringVar(master=top, value='24')
    ttk.Entry(row, textvariable=values['max_calls'], width=8).pack(side='left')
    values['allow_api_result_read'] = tk.BooleanVar(master=top, value=False)
    ttk.Checkbutton(frame, text='允许AI按需读取本任务已核验的API结果（每页最多4000字符）',
                    variable=values['allow_api_result_read']).pack(anchor='w', pady=9)
    info = ttk.Label(frame, text='尚未检查；本地检查不证明网络连通或服务端授权有效。', wraplength=700)
    info.pack(fill='x', pady=7)

    def fill(config):
        for key, value in config.items():
            if key in values:
                values[key].set(value)

    def invoke(operation):
        try:
            if operation == 'disable':
                market_settings.disable(window.app, pid)
                info.configure(text='已停用。原任务、权益、请求和结果记录保留。')
                return
            if operation == 'import':
                path = filedialog.askopenfilename(parent=top, title='选择超市连接JSON（只包含路径，不包含Token）')
                if path:
                    fill(market_settings.load_import(path))
                    info.configure(text='字段已导入，尚未保存、联网或运行。请核对当前任务范围。')
                return
            config = {key: value.get() for key, value in values.items()}
            config['max_calls'] = int(config['max_calls'])
            if operation == 'check':
                market_settings.check(window.app, pid, config, scope)
                info.configure(text='本地配置检查通过；未联网，不代表服务端身份、预算或交付可用。')
            else:
                market_settings.save(window.app, pid, config, scope)
                info.configure(text='已为当前任务保存并启用。下次执行时核验服务并初查；本次未调用模型或支付。')
        except ValueError as exc:
            # Project exceptions have fixed messages; parser errors must not echo imported values.
            from .core import ContractError
            from .run_lock import RunBusy
            info.configure(text=str(exc) if isinstance(exc, (ContractError, RunBusy)) else '字段格式有误，请核对上限和配置。')
        except OSError:
            info.configure(text='本地配置不可用，保留原记录。')

    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=12)
    for label, operation in [('导入配置', 'import'), ('检查配置', 'check'), ('保存并启用', 'save'), ('停用连接', 'disable')]:
        ttk.Button(buttons, text=label, command=lambda op=operation: invoke(op)).pack(side='left', padx=4)
    try:
        existing = market_settings.saved(window.app, pid)
        if existing:
            fill(existing['config'])
            state = '已启用' if existing['enabled'] else '已停用'
            note = '；任务范围已改变，旧配置不可复用' if existing['scope'] != scope else ''
            info.configure(text=state + note + '。检查与保存均不联网。')
    except ValueError:
        info.configure(text='原配置记录异常，未填充；请保留记录核查。')
    return top
