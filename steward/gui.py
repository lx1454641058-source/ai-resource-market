"""来源：本项目原创。使用 Python 自带 Tkinter 构建本地演示窗口。"""

import tkinter as tk
import queue
import threading
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .core import Steward
from .runtime import RunControl


ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"new": "待推进", "planning": "规划中", "ready": "待继续", "executing": "执行中",
            "awaiting_review": "待你验收", "accepted": "你已验收", "rejected": "需要改进",
            "paused": "已暂停", "failed": "执行失败", "budget_exhausted": "调用额度已用完",
            "waiting_input": "等待你补充", "deleted": "已删除"}

ACTION_LABELS = {'plan': '更新计划', 'save_artifact': '保存成果', 'read_artifact': '读取历史成果',
                 'request_resource': '向你申请API资源',
                 'delegate': '委派项目负责人', 'note': '保存项目笔记', 'ask_user': '请求补充', 'finish': '提交验收'}


class DemoWindow:
    def __init__(self, root, db_path=None, personal=False):
        self.root = root
        self.personal = personal
        if personal:
            from .personal import open_personal
            self.app = open_personal(db_path)
        else:
            self.app = Steward(db_path or ROOT / "work" / "desktop-demo.sqlite")
        self.busy = False
        self.jobs = queue.Queue()
        self.job_id = None
        self.current = None
        self.control = None
        self.active_project = None
        self.active_message = None
        self.closing = False
        self.connection_jobs = []
        self.close_wait_id = None
        root.title("我的个人总管 · GPT-6 Astra · 中等" if personal else "个人总管 · 可交互 Demo")
        width = min(1160, int(root.winfo_screenwidth() * .9))
        height = min(800, int(root.winfo_screenheight() * .85))
        root.geometry(f"{width}x{height}")
        root.minsize(min(920, width), min(650, height))
        self.mode = 'new'
        self.drafts = {}
        self.contents = {}
        root.configure(bg="#ffffff")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#ffffff")
        style.configure("TLabel", background="#ffffff", font=("Microsoft YaHei UI", 10))
        style.configure("Side.TFrame", background="#f4f4f4")
        style.configure("Side.TLabel", background="#f4f4f4", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 7),
                        background="#f4f4f4", borderwidth=0)
        style.map("TButton", background=[('active', '#e7e7e7')])
        style.configure("Send.TButton", background="#202020", foreground="#ffffff")
        style.map("Send.TButton", background=[('disabled', '#999999'), ('active', '#444444')])
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=40,
                        background="#f4f4f4", fieldbackground="#f4f4f4", borderwidth=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        left = ttk.Frame(root, style="Side.TFrame", padding=16, width=270)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(5, weight=1)
        ttk.Label(left, text="个人总管", style="Side.TLabel", font=("Microsoft YaHei UI", 18, "bold")).grid(row=0, column=0, sticky="w", pady=(8, 22))
        ttk.Button(left, text="＋  新需求", command=lambda: self.navigate('new')).grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(left, text="记忆与偏好", command=lambda: self.navigate('memory')).grid(row=3, column=0, sticky="ew", pady=4)
        ttk.Label(left, text="任务", style="Side.TLabel").grid(row=4, column=0, sticky="w", pady=(24, 8))
        task_list = ttk.Frame(left, style="Side.TFrame")
        task_list.grid(row=5, column=0, sticky="nsew")
        self.projects = ttk.Treeview(task_list, columns=("title", "status"), show="tree", selectmode="browse")
        self.projects.column('#0', width=215, stretch=True)
        scroll = ttk.Scrollbar(task_list, command=self.projects.yview)
        self.projects.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.projects.pack(fill="both", expand=True)
        self.projects.bind("<<TreeviewSelect>>", self.select)
        self.projects.bind("<ButtonRelease-1>", self.open_clicked_task)
        self.projects.bind('<Delete>', lambda event: self.delete_task())
        self.task_menu = tk.Menu(root, tearoff=False)
        self.task_menu.add_command(label='删除任务', command=self.delete_task)
        self.task_menu.add_command(label='回收站', command=self.show_trash)
        self.projects.bind('<Button-3>', self.task_context_menu)
        settings = ttk.Frame(left, style="Side.TFrame")
        settings.grid(row=6, column=0, sticky="ew", pady=(16, 0))
        if personal:
            from . import settings_ui
            self.settings_buttons = {}
            for label, command in (("插件市场", settings_ui.plugin_market), ("API 接入", settings_ui.api_connections), ("权限设置", settings_ui.permissions)):
                button = ttk.Button(settings, text=label, command=lambda fn=command: fn(self))
                button.pack(fill='x', pady=3)
                self.settings_buttons[label] = button
        ttk.Label(settings, text="GPT-6 Astra · 中等" if personal else "本地演示 · 不调用模型", style="Side.TLabel").pack(anchor="w", pady=(12, 0))
        right = ttk.Frame(root, padding=(28, 20))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.summary = ttk.Label(right, text="今天想推进什么？", style="Title.TLabel", wraplength=700)
        self.summary.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.runtime_status = ttk.Label(right, text="当前窗口没有执行任务。", foreground="#777777", wraplength=700)
        self.runtime_status.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        body = ttk.Frame(right)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.thread_panel = ttk.Frame(body)
        self.thread_panel.grid(row=0, column=0, sticky="nsew")
        self.transcript = tk.Text(self.thread_panel, wrap="word", font=("Microsoft YaHei UI", 11),
                                  relief="flat", padx=18, pady=16, spacing1=2, spacing3=4,
                                  bg="#ffffff", fg="#252525", state="disabled")
        scrollbar = ttk.Scrollbar(self.thread_panel, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.transcript.pack(fill="both", expand=True)
        self.new_panel = ttk.Frame(body, padding=24)
        self.new_panel.grid(row=0, column=0, sticky="nsew")
        ttk.Label(self.new_panel, text="你想做什么？", style="Title.TLabel").pack(anchor="w", pady=(32, 18))
        ttk.Label(self.new_panel, text="不必先写完整计划。说说你的想法，总管会与你讨论目标，\n再安排团队推进，并在过程中向你汇报。", wraplength=500).pack(anchor="w")
        if not personal:
            self.goal = tk.Text(self.new_panel, height=4, wrap="word")
            self.goal.pack(fill="x", pady=8)
            self.acceptance = tk.Text(self.new_panel, height=2, wrap="word")
            self.acceptance.pack(fill="x", pady=8)
            self.create_button = ttk.Button(self.new_panel, text="创建演示任务", command=self.create)
            self.create_button.pack(anchor="e")
        self.memory_panel = ttk.Frame(body, padding=24)
        self.memory_panel.grid(row=0, column=0, sticky="nsew")
        self.memory_text = tk.Text(self.memory_panel, height=10, wrap="word", relief="flat", font=("Microsoft YaHei UI", 11))
        self.memory_text.pack(fill="both", expand=True)
        ttk.Label(self.memory_panel, text="偏好名称 / 内容（同名保存会更新）").pack(anchor="w")
        self.memory_key = ttk.Entry(self.memory_panel)
        self.memory_key.insert(0, "沟通偏好")
        self.memory_key.pack(fill="x", pady=6)
        self.memory_value = ttk.Entry(self.memory_panel)
        self.memory_value.insert(0, "用简体中文，先给结论，再解释步骤")
        self.memory_value.pack(fill="x", pady=6)
        ttk.Button(self.memory_panel, text="保存偏好", command=self.remember).pack(anchor="e")
        self.project_actions = ttk.Frame(right)
        self.project_actions.grid(row=3, column=0, sticky="ew", pady=8)
        self.continue_button = ttk.Button(self.project_actions, text="继续任务", command=self.continue_project)
        self.continue_button.pack(side="left")
        self.confirm_button = ttk.Button(self.project_actions, text="确认目标并推进", command=self.confirm_project_goal)
        self.confirm_button.pack(side="left", padx=5)
        if self.personal:
            from .resource_ui import resource_requests
            self.resource_button = ttk.Button(self.project_actions, text='资源申请', command=lambda: resource_requests(self))
            self.resource_button.pack(side='left', padx=5)
            self.market_button = ttk.Button(self.project_actions, text='超市连接', command=self.connect_project_market)
            self.market_button.pack(side='left', padx=5)
        if not personal:
            ttk.Button(self.project_actions, text="验收", command=lambda: self.review_dialog(True)).pack(side="left", padx=5)
            ttk.Button(self.project_actions, text="提出改进", command=lambda: self.review_dialog(False)).pack(side="left")
        ttk.Button(self.project_actions, text='删除任务', command=self.delete_task).pack(side='left', padx=5)
        ttk.Button(self.project_actions, text="运行详情", command=self.show_details).pack(side="right")
        ttk.Button(self.project_actions, text="导出", command=self.export).pack(side="right", padx=5)
        # 验收编辑值由弹窗填写，普通项目输入统一进入下方对话框。
        self.feedback = ttk.Entry(self.project_actions)
        self.composer_panel = ttk.Frame(right, padding=10)
        self.composer_panel.grid(row=4, column=0, sticky="ew")
        self.composer_hint = ttk.Label(self.composer_panel, text="发送新要求，继续推进当前任务", foreground="#777777")
        self.composer_hint.pack(anchor="w", pady=(0, 6))
        self.project_prompt = tk.Text(self.composer_panel, height=3, wrap="word", font=("Microsoft YaHei UI", 11), relief="solid", borderwidth=1)
        self.project_prompt.pack(fill="x")
        self.chat_input = self.project_prompt
        self.project_prompt.bind('<Control-Return>', lambda event: self.send_message())
        send_row = ttk.Frame(self.composer_panel)
        send_row.pack(fill="x", pady=(8, 0))
        ttk.Label(send_row, text="Ctrl + Enter 发送 · Enter 换行", foreground="#888888").pack(side="left")
        self.chat_button = ttk.Button(send_row, text="发送 ↑", style="Send.TButton", command=self.send_message)
        self.chat_button.pack(side="right")
        self.update_button = self.chat_button
        if personal:
            self.create_button = self.chat_button
        self.stop_button = ttk.Button(send_row, text="停止", command=self.stop_job, state="disabled")
        self.stop_button.pack(side="right", padx=6)
        self.notice = ttk.Label(right, text="选择左侧任务继续，或新建一个任务。", foreground="#777777", wraplength=700)
        self.notice.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        right.bind('<Configure>', lambda event: [label.configure(wraplength=max(240, event.width-56))
                                                for label in (self.summary, self.runtime_status, self.notice)])
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.navigate('project' if self.current else 'new')
        if personal and not self.app.context_sharing_allowed():
            self.notice.configure(text='首次使用：在左下角“API 接入”测试主管登录，再到“权限设置”选择允许发送的内容。')

    def navigate(self, mode, pid=None):
        key = self.current if self.mode == 'project' else self.mode
        self.drafts[key] = self.project_prompt.get('1.0', 'end-1c')
        self.mode = mode
        if pid is not None:
            self.current = pid
        key = self.current if mode == 'project' else mode
        self.project_prompt.delete('1.0', 'end')
        self.project_prompt.insert('1.0', self.drafts.get(key, ''))
        for panel in (self.new_panel, self.memory_panel, self.thread_panel):
            panel.grid_remove()
        self.project_actions.grid_remove()
        self.composer_panel.grid_remove()
        if mode == 'new':
            self.new_panel.grid()
            self.summary.configure(text='与总管开始讨论')
            if self.personal:
                self.composer_panel.grid()
                self.composer_hint.configure(text='输入你的想法或模糊需求，总管会先与你讨论')
        elif mode == 'memory':
            self.memory_panel.grid()
            self.summary.configure(text='记忆与偏好')
        else:
            self.thread_panel.grid()
            self.composer_panel.grid()
            if mode == 'project' and self.current:
                self.project_actions.grid()
                self.composer_hint.configure(text='与总管讨论 · 随时补充要求、询问进度或调整方向')
                self.display(self.current)
            else:
                self.summary.configure(text='和总管聊聊')
                self.composer_hint.configure(text='与总管讨论想法 · 不自动创建任务')
                self.render_thread()

    def send_message(self):
        if self.mode == 'new':
            self.create()
        elif self.mode == 'chat':
            self.send_chat()
        elif self.mode == 'project':
            if self.personal:
                self.submit_update()
            else:
                self.notice.configure(text='本地演示不理解新提示词，请打开“启动我的总管.cmd”使用真实模型。')
        return 'break'

    def render_thread(self):
        if self.mode == 'chat':
            text = self.contents.get('和总管讨论') or '总管\n你好，想讨论什么？在下方输入消息。'
        elif self.mode == 'project' and self.current:
            p = self.app.get(self.current)
            if p.get('orchestration'):
                labels = {'user': '你', 'steward': '总管'}
                blocks = [labels.get(m['role'], m['role'])+'\n'+m['content'] for m in p.get('dialogue', [])]
                if p.get('incoming'):
                    blocks.append('总管\n已收到新消息，等待当前调用结束后优先处理。')
                if p.get('resource_requests'):
                    from .resources import overview, STATES
                    rows = overview(self.app, p)
                    blocks.append('资源授权状态（点击“资源申请”查看详情与处理）\n'+'\n'.join(
                        f"{r['id']} · {r['spec']['service']['model']} · {STATES[r['effective_state']]} · "
                        f"次数 {r['used_calls']}/{r['spec']['max_calls']}" for r in rows))
                if p.get('team'):
                    states = {'working': '工作中', 'blocked': '等待总管决策', 'delivered': '已交付，待总管审查',
                              'idle': '待分工', 'needs_replanning': '等待总管调整'}
                    blocks.append('团队状态\n'+'\n'.join(m['role']+' · '+states.get(m['status'], m['status']) for m in p['team']))
                if p.get('artifacts'):
                    blocks.append('已保存成果\n'+self.contents.get('成果草稿', ''))
                if p.get('files'):
                    blocks.append('已生成文件\n'+'\n'.join(f['path'] for f in p['files']))
                if p.get('error'):
                    blocks.append('本轮未完成\n'+p.get('error_message', p['error']))
                text = '\n\n────────────────────────\n\n'.join(blocks)
            else:
                text = self.legacy_transcript(p)
        else:
            return
        self.update_transcript(text)

    def legacy_transcript(self, p):
        blocks = ['你\n'+p['goal'], '完成标准\n'+p['acceptance']]
        blocks += ['你 · 补充要求\n'+u['text'] for u in p.get('user_updates', [])]
        blocks += ['总管 · 当前进展\n'+self.contents.get('任务与选择', ''),
                   '总管 · 已保存成果\n'+self.contents.get('成果草稿', '')]
        if p.get('error'):
            blocks.append('总管 · 本轮未完成\n'+p.get('error_message', p['error']))
        if p.get('feedback'):
            blocks.append('你 · 验收反馈\n'+p['feedback'])
        return '\n\n────────────────────────\n\n'.join(blocks)


    def update_transcript(self, text):
        # 轮询没有新内容时不改文本，避免打断用户滚动阅读。
        if self.transcript.get('1.0', 'end-1c') != text:
            position = self.transcript.yview()
            self.transcript.configure(state='normal')
            self.transcript.delete('1.0', 'end')
            self.transcript.insert('1.0', text)
            self.transcript.configure(state='disabled')
            if position[1] >= .99:
                self.transcript.see('end')
            else:
                self.transcript.yview_moveto(position[0])

    def review_dialog(self, accept):
        if not self.current or self.busy:
            return
        text = simpledialog.askstring('验收反馈', '请填写验收意见：', parent=self.root)
        if text is not None:
            self.feedback.delete(0, 'end')
            self.feedback.insert(0, text)
            self.review(accept)

    def show_details(self):
        if not self.current:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('运行详情')
        dialog.geometry('760x540')
        text = tk.Text(dialog, wrap='word', font=('Microsoft YaHei UI', 10))
        scroll = ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.insert('1.0', self.contents.get('运行记录', '暂无记录'))
        text.configure(state='disabled')

    def configure_models(self):
        from .orchestration import catalog
        resources = catalog(self.app)
        enabled = ', '.join(m['id'] for m in resources['models'])
        names = simpledialog.askstring('模型与工具',
            '已启用模型：'+enabled+'\n工具：读取/保存成果、项目笔记、导出文件、向总管提问。\n'
            '可输入允许负责人调用的 Codex 模型名称，用英文逗号分隔。\n'
            '启用即允许通过现有登录向这些模型发送项目上下文；可用性在调用时验证。\n'
            '留空恢复仅使用总管模型。价格与任务质量尚无实测数据。', parent=self.root)
        if names is not None:
            try:
                self.app.enable_worker_models([name.strip() for name in names.split(',') if name.strip()])
                self.notice.configure(text='模型目录已保存。当前运行保持原配置，下次启动运行时加载。')
            except ValueError as error:
                self.notice.configure(text=str(error))

    def connect_project_market(self):
        from .market_ui import market_connection
        if self.busy or not self.current:
            return market_connection(self)
        project = self.app.get(self.current)
        if project.get('goal_proposal'):
            # Bind only the confirmed scope. Do not start a worker between
            # confirming a goal and saving its first market connection.
            if not self.confirm_project_goal(advance=False):
                return None
        return market_connection(self)

    def confirm_project_goal(self, *, advance=True):
        if self.busy or not self.current:
            return False
        if self.project_prompt.get('1.0', 'end').strip():
            self.notice.configure(text='输入框还有讨论内容，请先发送；确认按钮只确认当前已展示的目标。')
            return False
        try:
            self.app.require_context_sharing()
            self.app.confirm_goal(self.current)
            self.refresh()
            if advance:
                self.start_job('run', self.current)
            else:
                self.notice.configure(text='目标已确认，尚未启动任务。保存超市连接后点击“继续任务”。')
            return True
        except ValueError as error:
            self.notice.configure(text=str(error))
            return False

    def check_setup(self):
        from .codex_model import codex_path
        try:
            codex_path()
            connection = "已找到模型调用程序。"
        except ValueError as error:
            connection = str(error)
        consent = "已授权个人上下文。" if self.app.context_sharing_allowed() else "尚未授权个人上下文。"
        used = self.app.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
        self.notice.configure(text=f"{connection} {consent} 本地调用名额 {used}/{self.app.global_budget()}。")
        if not self.busy:
            self.runtime_status.configure(text="本机检查不调用模型、不消耗额度；登录状态、模型权限和网络仍需真实调用验证。")

    def write(self, name, content):
        self.contents[name] = content
        if name == '长期记忆':
            self.memory_text.configure(state='normal')
            self.memory_text.delete('1.0', 'end')
            self.memory_text.insert('1.0', content)
            self.memory_text.configure(state='disabled')

    def refresh(self):
        for item in self.projects.get_children():
            self.projects.delete(item)
        for p in self.app.projects():
            if p.get("kind") == "conversation":
                if self.personal:
                    self.write("和总管讨论", "\n\n".join(
                        ("你：" if m["role"] == "user" else "总管：") + m["content"] +
                        ({'pending': '\n（处理中）', 'failed': '\n（本轮未完成，可重新发送）',
                          'not_sent': '\n（额度不足，尚未发送）'}.get(m.get('status'), '')) for m in p.get("messages", [])))
                continue
            self.projects.insert("", "end", iid=p["id"], text=f"{STATUSES[p['status']]} · {p['goal']}", values=(p["goal"], STATUSES[p["status"]]))
        memories = self.app.memories()
        self.write("长期记忆", "用户明确保存的偏好（可用下方同名保存修正）\n\n" +
                   ("\n\n".join(f"{key}\n{value['value']}\n来源：用户明确输入" for key, value in memories.items()) or "尚未保存偏好。"))
        if self.current and not self.projects.exists(self.current):
            self.current = None
        if self.personal and not self.current and self.projects.get_children():
            self.current = self.projects.get_children()[-1]
        if self.current:
            if self.projects.selection() != (self.current,):
                self.projects.selection_set(self.current)
            if self.mode == "project":
                self.display(self.current)
        self.render_thread()

    def select(self, _event=None):
        chosen = self.projects.selection()
        if chosen and (chosen[0] != self.current or self.mode == "project"):
            if chosen[0] != self.current:
                self.navigate("project", chosen[0])
            elif self.mode == "project":
                self.display(self.current)

    def open_clicked_task(self, event):
        pid = self.projects.identify_row(event.y)
        if pid:
            self.navigate("project", pid)

    def task_context_menu(self, event):
        pid = self.projects.identify_row(event.y)
        if pid:
            self.navigate('project', pid)
            self.projects.selection_set(pid)
        try:
            self.task_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.task_menu.grab_release()

    def delete_task(self):
        if not self.current:
            return
        try:
            if self.busy and self.active_project == self.current:
                raise ValueError('请先停止当前任务，再删除。')
            pid = self.current
            self.app.delete_project(pid)
            self.drafts.pop(pid, None)
            self.current = None
            self.mode = 'new'
            self.project_prompt.delete('1.0', 'end')
            self.refresh()
            self.navigate('project' if self.current else 'new')
            self.notice.configure(text='任务已移入回收站；右键任务列表可恢复，成果文件保留。')
        except ValueError as error:
            self.notice.configure(text=str(error))

    def show_trash(self):
        top = tk.Toplevel(self.root)
        top.title('任务回收站')
        top.geometry('540x360')
        items = [p for p in self.app.projects(include_deleted=True) if p['status'] == 'deleted']
        rows = tk.Listbox(top)
        rows.pack(fill='both', expand=True, padx=12, pady=12)
        for p in items:
            rows.insert('end', p['goal'])
        def restore():
            if rows.curselection():
                self.app.restore_project(items[rows.curselection()[0]]['id'])
                self.refresh()
                top.destroy()
        ttk.Button(top, text='恢复选中任务', command=restore).pack(pady=10)

    def display(self, pid):
        p = self.app.get(pid)
        if self.personal:
            self.market_button.configure(text='确认并连接超市' if p.get('goal_proposal') else '超市连接')
        suffix = "（模拟）" if p["mode"] == "demo" else "（真实调用，非Token账单）"
        done = sum(t['status'] == 'done' for t in p['tasks'])
        title = ' '.join(p['goal'].split())[:36]
        self.summary.configure(text=f"{title}\n{STATUSES[p['status']]} · 已保存 {done}/{len(p['tasks'])} 项草稿")
        if p.get('goal_proposal') and not p.get('orchestration'):
            self.continue_button.pack_forget()
            self.confirm_button.pack(side='left', padx=5)
        else:
            self.confirm_button.pack_forget()
            self.continue_button.pack(side='left')
        if self.projects.exists(pid):
            self.projects.item(pid, text=f"{STATUSES[p['status']]} · {p['goal']}", values=(p['goal'], STATUSES[p['status']]))
        if not self.busy:
            self.runtime_status.configure(text=("记录仍为处理中，但当前窗口没有持有执行器；运行状态待核验，请勿重复启动。"
                                                if p['status'] in ('planning', 'executing') else
                                                "当前窗口没有执行任务。已保存成果可查看或导出。"))
        lines = []
        if p.get('workflow') == 'agent-v1':
            lines += [f"第 {p.get('revision', 0)+1} 版\n",
                      "当前计划：\n" + '\n'.join(p.get('agent_plan', [])),
                      "\n最近决策：\n" + '\n'.join(f"{ACTION_LABELS.get(a['action'], a['action'])}：{a['reason']}" for a in p.get('action_history', [])[-8:])]
            if p.get('question'):
                lines.append('\n总管需要你补充：\n'+p['question']+'\n请在下方对话框输入并发送。')
            if p.get('submission'):
                lines.append('\n模型自检（尚未经你验收）：\n'+'\n'.join(p['submission']['criteria'])+
                             '\n限制：'+p['submission']['limitations'])
        for i, task in enumerate(p["tasks"], 1):
            if p.get('workflow') == 'agent-v1':
                lines.append(f"\n已保存成果 {i-1}：{task['title']}；是否达标需按验收条件检查。")
                continue
            route = task.get("routing", {})
            name = "本地提纲模板" if route.get("tool") == "outline_template" else ("真实模型生成" if p["mode"] != "demo" else "演示草稿生成器")
            lines.append(f"{i}. {task['title']}\n状态：{'已生成草稿' if task['status'] == 'done' else '待执行'}"
                         f"\n选用：{name}\n原因：{route.get('reason', '尚未选择')}\n"
                         "边界：选择仅覆盖当前工具目录，不代表全市场最优。\n")
        self.write("任务与选择", "\n".join(lines))
        self.write("成果草稿", "\n\n────────────\n\n".join(
            (f"成果 {i} · 第 {a.get('revision', 0)+1} 版 · {a.get('title', '')}\n\n" if p.get('workflow') else '')+
            a['content'] for i, a in enumerate(p['artifacts'])) or "尚无成果。")
        events = self.app.audit(pid)
        labels = {"created": "创建项目", "call_reserved": "预留调用额度", "planned": "保存任务计划",
                  "artifact_saved": "保存草稿成果", "reviewed": "记录用户验收", "failed": "执行失败",
                  "model_completed": "真实模型返回完成凭据", "paused": "已暂停，保留已存成果",
                  "resumed": "用户继续项目", "retry_requested": "用户重试未完成步骤"}
        labels.update(agent_action="总管动作", loop_paused="重复动作已暂停", user_update="收到用户补充",
                      project_budget="用户调整调用预算")
        text = "\n".join(f"{e['at'][:19]}  {labels.get(e['kind'], e['kind'])} " +
                         ACTION_LABELS.get(e['data'].get('action'), e['data'].get('action', '')) for e in events)
        text = f"调用名额 {p['calls']}/{p['max_calls']}{suffix}\n总管：{p['manager']}\n项目负责人：{p['worker']}\n\n" + text
        for event in events:
            if event["kind"] == "model_completed":
                data = event["data"]
                text += f"\n\n模型：{data['model']} / {data['reasoning_effort']}\n耗时：{data['elapsed_seconds']}秒\n用量：{data['usage']}"
        if p.get("error"):
            text += "\n\n未完成：" + p.get("error_message", p["error"])
        if p.get("feedback"):
            text += f"\n\n你的验收反馈：{p['feedback']}\n经验状态：候选；不自动推广。"
        for assessment in p.get('assessments', []):
            text += '\n\n主管决策摘要\n'+'\n'.join(f'{key}: {value}' for key, value in assessment.items())
        self.write("运行记录", text)
        self.render_thread()

    def create(self):
        if self.busy:
            self.notice.configure(text='当前窗口正在推进任务。可在该任务中随时发消息；新需求请在本轮结束后发送，草稿会保留。')
            return
        try:
            if self.personal:
                self.app.require_context_sharing()
            if self.personal:
                message = self.project_prompt.get('1.0', 'end').strip()
                if not message:
                    self.notice.configure(text='请先输入你想讨论的需求。')
                    return
                p = self.app.begin_request(message)
                self.project_prompt.delete('1.0', 'end')
            else:
                p = self.app.create(self.goal.get("1.0", "end").strip(), self.acceptance.get("1.0", "end").strip())
            self.navigate('project', p['id'])
            self.feedback.delete(0, "end")
            if self.personal:
                self.refresh()
                self.start_job("run", self.current)
                return
            self.app.run(self.current)
            self.mode = 'project'
            self.refresh()
            self.navigate('project')
            self.notice.configure(text="演示已推进：查看成果后，可以接受或提出改进。")
        except (ValueError, OSError) as error:
            messagebox.showerror("暂时无法创建", str(error), parent=self.root)

    def review(self, accept):
        if not self.current or self.busy:
            return
        try:
            self.app.review(self.current, accept, self.feedback.get())
            self.refresh()
            self.notice.configure(text="验收已保存。" if accept else
                                  "修改意见已保存。在下方输入具体要求并发送，即可继续。")
        except ValueError as error:
            messagebox.showinfo("需要补充", str(error), parent=self.root)

    def remember(self):
        try:
            self.app.remember(self.memory_key.get(), self.memory_value.get())
            self.refresh()
            self.notice.configure(text="偏好已保存；下一次模型调用会参考。" if self.personal else "偏好已保存。当前演示模型不理解偏好；真实模型接口会接收这些记忆。")
        except ValueError as error:
            messagebox.showinfo("需要补充", str(error), parent=self.root)

    def export(self):
        if self.current:
            path = self.app.export(self.current, self.app.db_path.parent / "exports")
            self.notice.configure(text=f"已导出：{path}")

    def close(self):
        if self.close_wait_id:
            self.root.after_cancel(self.close_wait_id)
            self.close_wait_id = None
        self.connection_jobs = [(control, thread) for control, thread in self.connection_jobs if thread.is_alive()]
        for control, thread in self.connection_jobs:
            control.cancel()
        if self.busy:
            self.closing = True
            self.stop_job()
            self.notice.configure(text="正在停止本轮运行，确认本机调用结束后自动关闭。")
            return
        if self.connection_jobs:
            self.closing = True
            self.notice.configure(text='正在停止连接测试，等待本机请求清理后关闭。')
            self.close_wait_id = self.root.after(100, self.close)
            return
        if self.job_id:
            self.root.after_cancel(self.job_id)
        self.app.close()
        self.root.destroy()

    def continue_project(self):
        if not self.current or self.busy:
            return
        if self.personal and self.project_prompt.get('1.0', 'end').strip():
            self.submit_update()
            return
        p = self.app.refresh_budget(self.current)
        if p["status"] not in ("new", "ready", "failed", "paused"):
            if self.personal and p['status'] in ('waiting_input', 'awaiting_review', 'rejected', 'accepted'):
                self.project_prompt.focus_set()
                self.notice.configure(text='请在下方输入下一步要求，再点击“发送”。已有成果会保留。')
            elif p['status'] == 'budget_exhausted':
                self.notice.configure(text='调用额度已用完，请通过“调用预算”调整项目和全局额度，再继续。')
            else:
                self.notice.configure(text='项目仍标记为运行中，请先在原运行窗口停止；异常退出的运行需核验后恢复。')
            return
        if self.personal:
            try:
                self.app.require_context_sharing()
            except ValueError as error:
                self.notice.configure(text=str(error))
                return
        if p['status'] == 'paused':
            self.app.resume(self.current)
        method = "retry" if p["status"] == "failed" else "run"
        if self.personal:
            self.start_job(method, self.current)
        else:
            getattr(self.app, method)(self.current)
            self.refresh()

    def send_chat(self):
        if self.busy:
            return
        message = self.chat_input.get('1.0', 'end').strip()
        if not message:
            return
        try:
            self.app.require_context_sharing()
        except ValueError as error:
            self.notice.configure(text=str(error))
            return
        if self.mode != 'chat':
            self.navigate('chat')
        self.start_job("chat", message)

    def submit_update(self):
        if self.busy and (not self.current or not self.app.get(self.current).get('orchestration')):
            self.notice.configure(text='此旧版任务请先停止再修改。输入内容会保留。')
            return
        if not self.current:
            self.notice.configure(text='请先在左侧选择一个项目。')
            return
        try:
            self.app.require_context_sharing()
            prompt = self.project_prompt.get('1.0', 'end').strip()
            if self.app.get(self.current).get('orchestration') and not prompt:
                self.notice.configure(text='请先输入要与总管讨论的内容。')
                return
            self.app.answer_project(self.current, prompt or self.feedback.get())
            self.project_prompt.delete('1.0', 'end')
            self.refresh()
            if self.busy:
                self.notice.configure(text='消息已保存。总管将在本次调用结束后优先处理，旧方向结果不会覆盖新要求。' if self.active_project == self.current else '消息已保存。当前运行结束后，可继续此任务处理。')
            else:
                self.start_job('run', self.current)
        except ValueError as error:
            self.notice.configure(text=str(error))

    def change_budget(self):
        if self.busy:
            return
        pid = self.app.chat_project()['id'] if self.mode == 'chat' else self.current
        if not pid:
            return
        p = self.app.get(pid)
        total = simpledialog.askinteger('本项目 / 对话调用预算',
                                       f"已使用 {p['calls']} 次。设置累计总名额（不是人民币金额）：",
                                       initialvalue=p['max_calls'], minvalue=p['calls'], maxvalue=100000, parent=self.root)
        if total is None:
            return
        used = self.app.db.execute('SELECT used FROM limits WHERE id=1').fetchone()[0]
        global_total = simpledialog.askinteger('全局调用预算',
                                              f'所有项目已使用 {used} 次。设置累计总名额；不会重置已用次数：',
                                              initialvalue=self.app.global_budget(), minvalue=used, maxvalue=100000, parent=self.root)
        if global_total is None:
            return
        try:
            self.app.set_budget(pid, total, global_total)
            self.refresh()
            self.notice.configure(text='调用预算已保存。需点击继续或发送消息才会调用模型。')
        except ValueError as error:
            self.notice.configure(text=str(error))

    def context_consent(self):
        if self.busy:
            return
        allowed = messagebox.askyesno(
            "个人上下文授权",
            "是否允许将你保存的偏好、近期对话及相关项目目标和状态，通过现有Codex登录发送给OpenAI的gpt-6-astra？\n\n"
            "这些内容用于回答和推进项目，调用会使用账户额度。程序不读取或发送登录凭证。\n\n"
            "选择‘是’保存授权；选择‘否’撤回授权并阻止后续个人上下文调用。",
            parent=self.root)
        self.app.set_context_sharing(allowed)
        self.notice.configure(text="个人上下文已授权，可以发送消息或推进项目。" if allowed else "个人上下文未授权，后续调用已停止。")

    def start_job(self, method, argument):
        self.busy = True
        self.control = RunControl()
        control = self.control
        self.active_project = argument if method != 'chat' else None
        self.active_message = argument if method == 'chat' else None
        self.create_button.configure(state="disabled")
        self.continue_button.configure(state="disabled")
        self.chat_button.configure(state="normal" if self.active_project and self.app.get(self.active_project).get('orchestration') else "disabled")
        self.stop_button.configure(state="normal")
        self.runtime_status.configure(text="正在准备本轮运行……")
        self.notice.configure(text="总管正在思考，真实调用可能需要一段时间；当前窗口仍可查看已有资料。")
        def work():
            from .personal import open_personal
            app = None
            try:
                app = open_personal(self.app.db_path, control=control)
                result = getattr(app, method)(argument)
                ok = True
            except Exception as error:
                ok, result = False, str(error) if isinstance(error, ValueError) else type(error).__name__
            finally:
                if app is not None:
                    app.close()
            # 清理结束后再通知界面，避免关闭窗口时遗留本轮资源。
            self.jobs.put((ok, result))
        threading.Thread(target=work, daemon=True).start()
        self.job_id = self.root.after(250, self.poll_job)

    def stop_job(self):
        if self.busy and self.control:
            self.control.cancel()
            self.stop_button.configure(state="disabled")
            self.notice.configure(text="正在停止本轮调用并保存暂停状态；已用额度可能仍会计费。")

    def show_runtime(self):
        state = self.control.snapshot()
        target = "当前对话" if self.active_project is None else f"项目 {self.active_project}"
        if state['stopping']:
            detail = "正在停止，请等待本机调用结束"
        elif state['pid'] is not None:
            detail = ("本机调用进程存活；尚未收到最终结果"
                      if state['checked_age'] is not None and state['checked_age'] < 5 else
                      "本机检查未更新，运行状态待核验")
        else:
            detail = "正在准备或保存结果"
        self.runtime_status.configure(text=f"{target} · {state['phase']} · 本轮 {state['elapsed']} 秒\n{detail}。存活不代表有效进展。")

    def poll_job(self):
        try:
            ok, result = self.jobs.get_nowait()
        except queue.Empty:
            if self.current and self.mode == 'project':
                self.display(self.current)
            self.show_runtime()
            self.job_id = self.root.after(500, self.poll_job)
            return
        self.job_id = None
        self.busy = False
        self.create_button.configure(state="normal")
        self.continue_button.configure(state="normal")
        self.chat_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.refresh()
        if ok and self.active_message is not None and result.get('status') == 'ready':
            if self.drafts.get('chat', '').strip() == self.active_message:
                self.drafts['chat'] = ''
            if self.mode == 'chat' and self.chat_input.get('1.0', 'end').strip() == self.active_message:
                self.chat_input.delete('1.0', 'end')
        self.runtime_status.configure(text="本轮本机执行已结束。" if ok else "本轮未完成，请查看下方原因。")
        if not ok:
            self.notice.configure(text="未完成：" + result)
        elif result.get("status") in ("failed", "budget_exhausted"):
            self.notice.configure(text="未完成：" + result.get("error_message", STATUSES[result["status"]]))
        elif result.get('status') == 'paused':
            reason = result.get('last_result', {}).get('reason', '')
            self.notice.configure(text=f"项目已暂停。{reason} 可继续未完成步骤；对话可重新发送。已用额度不退回。")
        elif result.get('status') == 'waiting_input':
            self.notice.configure(text='总管需要你补充信息，请在下方输入并发送。')
        elif result.get('status') == 'awaiting_review' or (result.get('kind') == 'conversation' and result.get('status') == 'ready'):
            self.notice.configure(text="本阶段已完成，成果已保留。你可以继续讨论或验收。")
        else:
            state = STATUSES.get(result.get('status'), '状态待核验')
            self.notice.configure(text=f'本窗口未完成新的执行。项目记录为“{state}”，请核验原执行窗口，避免重复启动。')
            self.runtime_status.configure(text='当前窗口没有持有模型调用；不能据此判断其他窗口已结束。')
        if self.closing:
            self.close()


def main(personal=False):
    root = tk.Tk()
    DemoWindow(root, personal=personal)
    root.mainloop()


if __name__ == "__main__":
    main()
