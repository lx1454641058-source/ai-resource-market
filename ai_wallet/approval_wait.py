"""来源：本项目原创。宿主等待预算决定；仅查询，不调用模型、不执行付款。"""

import math
import threading
import time

from .agent_planner import TaskPlanner


def wait_for_budget(planner, task_id, *, timeout_seconds=30, poll_seconds=1, cancel_event=None):
    """在当前宿主进程内等待；不注册后台任务，不向模型增加工具或权限。

    ready 只表示当前预算可继续申请采购，不能当成此次追加已获批准。
    每次采购仍须使用原防重复编号，并由钱包重新检查实际授权。
    """
    if not isinstance(planner, TaskPlanner):
        raise ValueError('需要宿主已固定AI主体的TaskPlanner')
    for value, lower, upper, name in ((timeout_seconds, 0, 60, '等待秒数'),
                                      (poll_seconds, 0.25, 5, '检查间隔')):
        if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f'{name}必须是{lower}到{upper}之间的有限数字')
    if cancel_event is not None and not isinstance(cancel_event, threading.Event):
        raise ValueError('取消信号必须是threading.Event')
    cancelled = cancel_event if cancel_event is not None else threading.Event()
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancelled.is_set():
            return {'event': 'cancelled', 'result': {'mode': 'demo', 'next_action': 'stop'}}
        state = planner.status(task_id)
        if cancelled.is_set():
            return {'event': 'cancelled', 'result': {'mode': 'demo', 'next_action': 'stop'}}
        # 读取失败不等同拒绝或未付款；交给宿主核对，不自行重试任何写操作。
        if state['status'] in ('error', 'unavailable'):
            return {'event': 'unavailable', 'result': state}
        if state['status'] in ('closed', 'rejected', 'paused') or state['next_action'] == 'stop':
            return {'event': 'stopped', 'result': state}
        if state['next_action'] == 'purchase_within_task':
            return {'event': 'ready', 'result': state}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'event': 'timeout', 'result': state}
        # 等待期间释放数据库连接；取消信号可立即中止，不让模型反复读取页面。
        cancelled.wait(min(poll_seconds, remaining))
