"""企业微信群机器人推送：markdown 消息、errcode 校验、滑动窗口限频。"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

import requests

from utils import load_secret

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
PERIOD_LABELS = {"m60": "60分钟", "daily": "日线"}
SIGNAL_LABELS = {"BUY": "B 买点", "SELL": "S 卖点"}


class Notifier:
    def __init__(self, cfg: dict, logger: logging.Logger | None = None):
        notify_cfg = cfg.get("notify", {}) or {}
        self.logger = logger or logging.getLogger("wave-monitor")
        self.dry_run = bool(cfg.get("general", {}).get("dry_run", True))
        self.max_per_minute = max(1, int(notify_cfg.get("max_per_minute", 15)))
        env_var = notify_cfg.get("webhook_key_env", "WECOM_WEBHOOK_KEY")
        self.key = load_secret(str(env_var))
        self._sent: deque[float] = deque()
        self._session = requests.Session()

    @property
    def ready(self) -> bool:
        return bool(self.key) or self.dry_run

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_minute:
            self.logger.warning("推送超过 %d 条/分钟限制，本次跳过", self.max_per_minute)
            return False
        self._sent.append(now)
        return True

    def send_signal(self, payload: dict[str, Any]) -> bool:
        return self._post(format_signal(payload))

    def send_alert(self, lines: list[str]) -> bool:
        content = "## 波段助手 · 运行告警\n" + "\n".join(f"> {line}" for line in lines[:20])
        return self._post(content)

    def _post(self, content: str) -> bool:
        if not self._rate_ok():
            return False
        if self.dry_run:
            self.logger.info("[dry-run] 不实际推送，消息内容:\n%s", content)
            return True
        if not self.key:
            self.logger.error("未配置 webhook key（环境变量未读到），推送失败")
            return False
        try:
            resp = self._session.post(
                WEBHOOK_URL,
                params={"key": self.key},
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if int(data.get("errcode", -1)) != 0:
                self.logger.error("企微返回错误: errcode=%s errmsg=%s",
                                  data.get("errcode"), data.get("errmsg"))
                return False
            self.logger.info("企微推送成功")
            return True
        except Exception as exc:  # noqa: BLE001 - 网络/JSON 异常统一记录
            self.logger.error("企微推送异常: %s", exc)
            return False


def fmt(value, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return f"{value}{suffix}"


def format_signal(p: dict[str, Any]) -> str:
    """构造企微 markdown。颜色：red=warning / green=info / gray=comment。"""
    signal = p.get("signal")
    color = "warning" if signal == "BUY" else "info"
    title = SIGNAL_LABELS.get(signal, "无信号")
    period = PERIOD_LABELS.get(p.get("period"), str(p.get("period")))
    hold = "黄柱·持有" if p.get("hold") else "蓝柱·观望"
    confirm = p.get("daily_confirm") or "日线：未取到"
    turnover = fmt(p.get("turnover"), "%")

    if p.get("signal_mode") == "ma7":
        try:
            c, m = float(p.get("close")), float(p.get("ma7"))
            gap = (c / m - 1) * 100
            if gap >= 0:
                dev = f"（已站上快线 {gap:+.2f}%）"
            elif signal == "BUY":
                dev = f"（距快线还差 {abs(gap):.2f}%，提前触发）"
            else:
                dev = f"（快线下方 {abs(gap):.2f}%）"
        except (TypeError, ValueError, ZeroDivisionError):
            dev = ""
        ma_line = (f"> 收盘={fmt(p.get('close'))} / MA7={fmt(p.get('ma7'))}{dev}"
                   f"；MA13={fmt(p.get('ma13'))}（参考）")
        if signal == "BUY" and not p.get("hold"):
            hold = "蓝柱·快线下方（提前买点，尚未转橙）"
    else:
        ma_line = (f"> MA7={fmt(p.get('ma7'))} / MA13={fmt(p.get('ma13'))}"
                   f"；R=MA7/MA13={fmt(p.get('ratio'), 4)}（阈值 rho={p.get('rho')}）")

    lines = [
        f"## 波段助手 · {period} {title}",
        f"**{p.get('name', '')}（{p.get('symbol', '')}）** <font color=\"{color}\">{title}</font>",
        f"> 触发时间：{p.get('time', '—')}（收盘后确认）",
        f"> 触发价：{fmt(p.get('close'))} 元",
        ma_line,
        f"> 换手率：{turnover}；状态：{hold}",
        f"> {confirm}",
        f"> 数据源：{p.get('source', '—')}",
        "<font color=\"comment\">指标为系数可调的复刻版本，仅供技术指标参考，不构成投资建议。</font>",
    ]
    return "\n".join(lines)
