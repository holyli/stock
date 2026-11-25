"""信号去重状态：原子写 data/state.json，按 标的|周期 记录最后处理的 K 线。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from utils import PROJECT_DIR


class StateStore:
    def __init__(self, path: str | Path | None = None,
                 logger: logging.Logger | None = None):
        self.path = Path(path) if path else PROJECT_DIR / "data" / "state.json"
        self.logger = logger or logging.getLogger("wave-monitor")
        self._lock = threading.Lock()
        self.data: dict[str, dict] = {}
        self._load()

    @staticmethod
    def key(symbol: str, period: str) -> str:
        return f"{symbol}|{period}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.data = payload
        except (ValueError, OSError) as exc:
            self.logger.warning("状态文件损坏，按空状态继续: %s", exc)

    def should_push(self, symbol: str, period: str, bar_time: str) -> bool:
        rec = self.data.get(self.key(symbol, period)) or {}
        last = str(rec.get("last_bar_time", ""))
        return str(bar_time) > last

    def mark(self, symbol: str, period: str, bar_time: str, signal: str | None) -> None:
        self.data[self.key(symbol, period)] = {
            "last_bar_time": str(bar_time),
            "last_signal": signal or "NONE",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save()

    def save(self) -> None:
        """临时文件 + os.replace，避免定时任务中途退出写坏状态。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
