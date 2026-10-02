"""来源：本项目原创。展示不可变申请及用户批准/拒绝/撤销入口。"""
import tkinter as tk
from tkinter import ttk

from .resources import overview, describe, STATES


def resource_requests(window):
    from .settings_ui import dialog, api_connections, permissions
    if not window.current:
        window.notice.configure(text='请先选择一个任务。')
        return None
    pid = window.current
    top, frame = dialog(window, '当前任务的资源申请', '860x740')
    ttk.Label(frame, text='主管先提交具体申请，你批准后才允许项目调用。保存密钥或开启API开关不代替批准。', wraplength=800).pack(anchor='w')
    picker = ttk.Combobox(frame, state='readonly')
    picker.pack(fill='x', pady=8)
    content = ttk.Frame(frame)
    content.pack(fill='both', expand=True)
    body = tk.Text(content, wrap='word', height=25, font=('Microsoft YaHei UI', 10))
    scroll = ttk.Scrollbar(content, command=body.yview)
    body.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    body.pack(side='left', fill='both', expand=True)
    info = ttk.Label(frame, text='', wraplength=800)
    info.pack(fill='x', pady=6)
    rows = []
    buttons = {}

    def selected(_event=None):
        index = picker.current()
        row = rows[index] if 0 <= index < len(rows) else None
        body.configure(state='normal')
        body.delete('1.0', 'end')
        body.insert('1.0', describe(row) if row else '主管尚未提交资源申请。你可以回到对话讨论目标及需要的资源。')
        body.configure(state='disabled')
        for name, button in buttons.items():
            allowed = row and ((name == 'approve' and row['effective_state'] == 'pending') or
                              (name == 'reject' and row['state'] == 'pending') or
                              (name == 'revoke' and row['state'] in ('approved', 'uncertain')))
            button.configure(state='normal' if allowed else 'disabled')

    def refresh():
        previous = picker.current()
        rows[:] = overview(window.app, window.app.get(pid))
        picker.configure(values=[r['id']+' · '+STATES[r['effective_state']]+' · '+r['spec']['service']['model'] for r in rows])
        if rows:
            picker.current(previous if 0 <= previous < len(rows) else len(rows)-1)
        selected()

    def decide(choice):
        index = picker.current()
        if not 0 <= index < len(rows):
            return
        try:
            window.app.decide_resource(pid, rows[index]['id'], choice)
            info.configure(text='决定已保存。主管会据此继续安排；撤销不能收回已经发送的请求。')
            refresh()
            window.refresh()
            if not window.busy and window.app.get(pid)['status'] == 'ready':
                window.start_job('run', pid)
        except ValueError as error:
            info.configure(text=str(error))

    controls = ttk.Frame(frame)
    controls.pack(fill='x', pady=8)
    for name, label in [('approve', '批准上述范围并继续'), ('reject', '拒绝，交回主管调整'), ('revoke', '撤销后续调用授权')]:
        buttons[name] = ttk.Button(controls, text=label, command=lambda c=name: decide(c))
        buttons[name].pack(side='left', padx=3)
    setup = ttk.Frame(frame)
    setup.pack(fill='x')
    ttk.Button(setup, text='打开 API 接入', command=lambda: api_connections(window)).pack(side='left')
    ttk.Button(setup, text='打开权限设置', command=lambda: permissions(window)).pack(side='left', padx=6)
    ttk.Button(setup, text='刷新申请状态', command=refresh).pack(side='left')
    picker.bind('<<ComboboxSelected>>', selected)
    refresh()
    return top
