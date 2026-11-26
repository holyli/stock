"""扫描单实例互斥。

上游按 IP 做滑动窗口 + 并发频控，同一分钟起两个扫描进程等于把请求量翻倍，
双方的请求会互相把配额顶穿。这里用文件锁保证同一时刻只有一个扫描进程在跑：
后到的进程先等一会儿（等前一轮跑完），仍拿不到锁才放弃本次扫描。

用 fcntl.flock 而不是 pid 文件：进程崩溃时内核自动释放，不会留下死锁。
"""

from __future__ import annotations

import fcntl
import logging
import time
from pathlib import Path

from utils import PROJECT_DIR


class ScanLock:
    def __init__(self, path: Path | str | None = None, wait_seconds: float = 300.0,
                 poll_seconds: float = 2.0, logger: logging.Logger | None = None):
        self.path = Path(path) if path else PROJECT_DIR / "data" / ".scan.lock"
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.logger = logger or logging.getLogger("wave-monitor")
        self._fh = None
        self.waited = 0.0

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                self.waited = time.monotonic() - started
                if self.waited >= self.poll_seconds:
                    self.logger.info("等待上一轮扫描结束 %.0f 秒后拿到锁，继续本次扫描",
                                     self.waited)
                return True
            except BlockingIOError:
                if time.monotonic() - started >= self.wait_seconds:
                    self.logger.warning("另一扫描实例仍在运行（已等待 %.0f 秒），本次跳过",
                                        time.monotonic() - started)
                    fh.close()
                    return False
                time.sleep(self.poll_seconds)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "ScanLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
