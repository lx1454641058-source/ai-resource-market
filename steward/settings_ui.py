"""来源：本项目原创。插件市场、API接入、权限设置的实际配置窗口。"""
import tkinter as tk
import queue
import threading
from tkinter import ttk, simpledialog, messagebox

from . import extensions as ext


def dialog(window, title, size='700x540'):
    top = tk.Toplevel(window.root)
    top.title(title)
    top.geometry(size)
    top.transient(window.root)
    frame = ttk.Frame(top, padding=18)
    frame.pack(fill='both', expand=True)
    return top, frame


def plugin_market(window):
    top, frame = dialog(window, '插件市场')
    ttk.Label(frame, text='安装插件后，主管可以阅读用法，并在权限允许时调用。\n当前支持声明式检查工具和工具指南，不执行下载的脚本。', wraplength=650).pack(anchor='w')
    rows = tk.Listbox(frame, height=12)
    rows.pack(fill='both', expand=True, pady=12)
    info = ttk.Label(frame, wraplength=650)
    info.pack(fill='x')
    entries = []
    polls = set()
    def destroyed(event):
        if event.widget == top:
            for ident in polls:
                top.after_cancel(ident)
    top.bind('<Destroy>', destroyed)
    def refresh():
        entries[:] = ext.market(window.app)
        rows.delete(0, 'end')
        for entry in entries:
            status = '已安装' if entry.get('id') in ext.installed(window.app) else '可安装'
            rows.insert('end', f"{entry['name']} · {status} · {entry.get('license', '下载后校验许可证')}")
    def selected(_event=None):
        if rows.curselection():
            entry = entries[rows.curselection()[0]]
            info.configure(text=entry.get('manual', '来自你添加的市场源；安装时校验清单与哈希。'))
    rows.bind('<<ListboxSelect>>', selected)
    def background(operation, value):
        # 下载在独立线程及其自己的数据库连接中进行，界面仍可响应停止与切换。
        from .core import Steward
        results = queue.Queue()
        path = window.app.db_path
        info.configure(text='正在下载并校验，请稍候……')
        def work():
            app = Steward(path)
            try:
                operation(app, value)
                result = '已完成。已安装插件需在权限设置中允许执行。'
            except Exception as error:
                result = str(error) if isinstance(error, ValueError) else '下载失败，请检查市场源与网络。'
            finally:
                app.close()
            results.put(result)
        def poll():
            try:
                result = results.get_nowait()
            except queue.Empty:
                polls.add(top.after(100, poll))
                return
            refresh()
            info.configure(text=result)
        threading.Thread(target=work, daemon=True).start()
        polls.add(top.after(100, poll))
    def install():
        if not rows.curselection():
            return
        background(ext.install, entries[rows.curselection()[0]])
    def source():
        url = simpledialog.askstring('添加市场源', '输入你信任的HTTPS插件索引网址：', parent=top)
        if url:
            background(ext.add_market, url)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=10)
    ttk.Button(buttons, text='下载 / 安装选中插件', command=install).pack(side='left')
    ttk.Button(buttons, text='添加市场源', command=source).pack(side='left', padx=10)
    refresh()
    return top


def api_connections(window):
    top, frame = dialog(window, 'API 接入', '780x720')
    ttk.Label(frame, text='主管：GPT-6 Astra，使用你自己的 Codex 登录。\n负责人：可接入兼容 Chat Completions 的 API。密钥可加密保存在本机。', wraplength=730).pack(anchor='w')
    configs = ext.setting(window.app, 'api_connections', {})
    picker = ttk.Combobox(frame, values=list(configs), state='readonly')
    picker.pack(fill='x', pady=8)
    fields = {}
    for key, label in [('id', '接入名称（英文）'), ('base_url', '服务基础地址（HTTPS，通常以 /v1 结尾）'),
                       ('model', '供应商模型名称'), ('key_env', '保存密钥的环境变量名称'),
                       ('input_price', '输入价格（美元/百万Token，可留空）'),
                       ('output_price', '输出价格（美元/百万Token，可留空）'), ('quality_note', '能力与质量依据（可写未知）')]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=label, width=43).pack(side='left')
        fields[key] = ttk.Entry(row)
        fields[key].pack(fill='x', expand=True)
    fields['quality_note'].insert(0, '尚未实测')
    fields['key_env'].insert(0, 'STEWARD_MODEL_KEY')
    key_row = ttk.Frame(frame)
    key_row.pack(fill='x', pady=4)
    ttk.Label(key_row, text='API 密钥（留空保留已保存密钥）', width=43).pack(side='left')
    secret = ttk.Entry(key_row, show='●')
    secret.pack(fill='x', expand=True)
    enabled = tk.BooleanVar(top, value=True)
    ttk.Checkbutton(frame, text='加入候选模型（实际项目调用还需要批准资源申请）', variable=enabled).pack(anchor='w', pady=8)
    info = ttk.Label(frame, text='密钥绑定当前Windows账户及服务地址，不进入对话。测试会发送固定测试句并消耗1次调用额度；不会发送个人项目。', wraplength=730)
    info.pack(fill='x', pady=6)
    def load(_event=None):
        config = configs[picker.get()]
        for key, entry in fields.items():
            entry.delete(0, 'end')
            entry.insert(0, '' if config[key] is None else str(config[key]))
        enabled.set(config['enabled'])
        secret.delete(0, 'end')
        state = '已保存本机加密密钥' if window.app.credential_vault().has(config) else '尚无本机密钥，调用时尝试环境变量'
        receipt = ext.setting(window.app, 'connection_test_api:'+config['id'], {})
        from .credentials import fingerprint
        tested = receipt.get('fingerprint') == fingerprint(config)
        info.configure(text=state+('。协议测试通过于 '+receipt['checked_at'] if tested else '。当前配置尚未通过连接测试。'))
    picker.bind('<<ComboboxSelected>>', load)
    def save():
        if active['control']:
            info.configure(text='连接测试尚未结束，请先停止或等待完成再修改配置。')
            return None
        try:
            config = {key: entry.get().strip() for key, entry in fields.items()}
            for key in ('input_price', 'output_price'):
                config[key] = None if not config[key] else float(config[key])
            config['enabled'] = enabled.get()
            from .api_model import validate_connection
            validate_connection(config)
            if secret.get().strip():
                window.app.credential_vault().save(config, secret.get())
                ext.save_setting(window.app, 'connection_test_api:'+config['id'], {})
                secret.delete(0, 'end')
            window.app.save_api_connection(config)
            configs[config['id']] = config
            picker.configure(values=list(configs))
            info.configure(text='配置已保存，未发起付费调用。主管下一轮可评估该模型；请回到任务的“资源申请”批准具体范围后再使用。')
            return config
        except ValueError as error:
            info.configure(text=str(error))
            return None
    controls = ttk.Frame(frame)
    controls.pack(fill='x', pady=8)
    ttk.Button(controls, text='保存接入配置', command=save).pack(side='left')
    active = {'control': None, 'poll_id': None}
    def test(config=None):
        if active['control']:
            return
        from .runtime import RunControl
        from .personal import open_personal
        control = active['control'] = RunControl()
        path = window.app.db_path
        results = queue.Queue()
        info.configure(text='正在真实连接测试；等待响应，可以停止。')
        def work():
            app = None
            try:
                app = open_personal(path, control=control)
                receipt = app.test_connection(config, control)
                result = f"真实调用与动作协议测试通过，用时 {receipt.get('elapsed_seconds', '?')} 秒。"
            except Exception as error:
                result = str(error) if isinstance(error, ValueError) else '测试未完成，请检查配置或网络。'
            finally:
                if app:
                    app.close()
            results.put(result)
        def poll():
            active['poll_id'] = None
            if not top.winfo_exists():
                return
            try:
                result = results.get_nowait()
            except queue.Empty:
                active['poll_id'] = top.after(150, poll)
                return
            active['control'] = None
            info.configure(text=result)
        thread = threading.Thread(target=work, daemon=True)
        window.connection_jobs.append((control, thread))
        thread.start()
        active['poll_id'] = top.after(150, poll)
    def test_api():
        config = save()
        if config:
            test(config)
    def stop():
        if active['control']:
            active['control'].cancel()
    def destroyed(event):
        if event.widget == top:
            if active['poll_id']:
                top.after_cancel(active['poll_id'])
                active['poll_id'] = None
            stop()
    top.bind('<Destroy>', destroyed)
    ttk.Button(controls, text='测试负责人 API', command=test_api).pack(side='left', padx=6)
    ttk.Button(controls, text='测试主管登录', command=test).pack(side='left')
    ttk.Button(controls, text='停止测试', command=stop).pack(side='left', padx=6)
    def forget():
        if active['control']:
            info.configure(text='请先停止连接测试再移除密钥。')
            return
        selected_id = picker.get()
        if selected_id and selected_id in configs:
            window.app.credential_vault().forget(configs[selected_id])
            ext.save_setting(window.app, 'connection_test_api:'+selected_id, {})
            secret.delete(0, 'end')
            info.configure(text='已移除选中配置的本机密钥；环境变量由你在系统设置中管理。')
    ttk.Button(frame, text='移除选中配置的本机密钥', command=forget).pack(anchor='w', pady=4)
    ttk.Button(frame, text='配置已有 Codex 登录中的负责人模型', command=window.configure_models).pack(anchor='w')
    return top


def permissions(window):
    top, frame = dialog(window, '权限设置', '680x530')
    current = ext.permissions(window.app)
    variables = {key: tk.BooleanVar(top, value=value) for key, value in current.items()}
    sharing = tk.BooleanVar(top, value=window.app.context_sharing_allowed())
    ttk.Label(frame, text='这些权限由你决定，主管和插件不能自行扩大。\n调整后对下一次派发或工具调用生效，已发送的请求不能撤回。', wraplength=600).pack(anchor='w', pady=12)
    ttk.Checkbutton(frame, text='允许将个人上下文发送给主管及你启用的负责人模型', variable=sharing).pack(anchor='w', pady=8)
    for key, title in [('api', '允许向已配置的API服务发送负责人任务上下文'),
                       ('plugins', '允许使用已安装插件'), ('export', '允许AI将成果导出为本机文件')]:
        ttk.Checkbutton(frame, text=title, variable=variables[key]).pack(anchor='w', pady=8)
    def save():
        with window.app.transaction():
            ext.save_setting(window.app, 'permissions', {key: value.get() for key, value in variables.items()})
            window.app.set_context_sharing(sharing.get())
        window.notice.configure(text='权限设置已保存。')
        top.destroy()
    ttk.Button(frame, text='设置当前任务与全局调用额度', command=window.change_budget).pack(anchor='w', pady=12)
    def backup_data():
        from .backup import backup
        try:
            path = backup(window.app, window.app.db_path.parent / 'backups')
            messagebox.showinfo('本地备份已保存', str(path)+'\n包含私人对话，不包含API密钥。请妥善保管。', parent=top)
        except (ValueError, OSError):
            messagebox.showerror('备份未完成', '请检查磁盘空间与数据目录权限。', parent=top)
    ttk.Button(frame, text='备份本机数据', command=backup_data).pack(anchor='w', pady=5)
    def restore_data():
        from tkinter import filedialog
        from .backup import restore
        import subprocess
        import sys
        from pathlib import Path
        selected = filedialog.askopenfilename(parent=top, title='恢复到新的独立目录，原数据保留', filetypes=[('总管备份', '*.zip')])
        if not selected:
            return
        try:
            folder = restore(selected, window.app.db_path.parent / 'restores')
            subprocess.Popen([sys.executable, '-E', '-s', '-m', 'steward.desktop', '--data', str(folder)],
                             cwd=Path(__file__).resolve().parent.parent, shell=False,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            messagebox.showinfo('备份已恢复', '已在新窗口打开独立恢复数据。原窗口数据保留；新窗口需要重新授权和保存密钥。\n'+str(folder), parent=top)
        except Exception:
            messagebox.showerror('未能恢复', '请检查备份格式与磁盘空间；当前数据未被覆盖。', parent=top)
    ttk.Button(frame, text='从备份恢复到新窗口', command=restore_data).pack(anchor='w', pady=5)
    ttk.Button(frame, text='保存权限', command=save).pack(anchor='e', pady=12)
    return top
