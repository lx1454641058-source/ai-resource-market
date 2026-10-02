"""来源：本项目原创。单次桌面运行的取消与进程存活证据，不保存模型原文。"""

import subprocess
import threading
import time


class ModelCancelled(ValueError):
    """用户停止本机调用；不代表供应商已撤销计费。"""


class RunControl:
    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.phase = "准备调用"
        self.pid = None
        self.checked = None

    def cancel(self):
        self.cancelled.set()

    def check(self):
        if self.cancelled.is_set():
            raise ModelCancelled("用户已停止本机调用；已保存成果保留，已用额度不退回。")

    def snapshot(self):
        with self.lock:
            return {"phase": self.phase, "pid": self.pid,
                    "elapsed": int(time.monotonic() - self.started),
                    "checked_age": None if self.checked is None else time.monotonic() - self.checked,
                    "stopping": self.cancelled.is_set()}

    def run(self, command, *, input, cwd, timeout=None, phase="模型处理中"):
        self.check()
        started = time.monotonic()
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                              cwd=cwd, shell=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            with self.lock:
                self.pid, self.phase = process.pid, phase
            finished = threading.Event()
            output, errors = [], []
            def communicate():
                try:
                    output.append(process.communicate(input=input))
                except Exception as error:
                    errors.append(error)
                finally:
                    finished.set()
            # Windows 的 stdin 写入也可能阻塞。专用线程负责所有管道I/O，
            # 控制线程独立检查取消，不让大输入阻止停止和显式超时。
            io_thread = threading.Thread(target=communicate, daemon=True)
            io_thread.start()
            try:
                while True:
                    self.check()
                    if timeout is not None and time.monotonic() - started >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    with self.lock:
                        self.checked = time.monotonic()
                    if finished.wait(0.1):
                        self.check()
                        if errors:
                            raise errors[0]
                        stdout, stderr = output[0]
                        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            finally:
                # 只处理本控制器启动的模型进程；该接口已禁用外部工具与子智能体。
                if process.poll() is None:
                    process.kill()
                io_thread.join()
                with self.lock:
                    self.pid = None
                    self.checked = time.monotonic()
                    self.phase = "本机调用已结束"
