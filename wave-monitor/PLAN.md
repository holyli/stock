# 波段助手买点监控 + 企业微信通知 实施计划（stock/wave-monitor）

## 一、目标与已确认需求

把同花顺 Mac 版「波段助手」指标（MA7/MA13 乘系数后金叉/死叉判买卖，开收盘价+换手率判持有/观望）复刻为自建监控脚本：定时拉行情、算信号、命中时推送企业微信群。

用户已拍板的四项决定：

1. **系数**：数学上只有 `rho = k2/k1` 有效（等价 `MA7/MA13` 穿越阈值），做成可调参数 + 截图标定值（日线 1.001、60 分钟 1.000），支持按周期覆盖
2. **周期**：60分钟与日线**两个都算、分别推送**；日线状态作为 60 分钟消息的二次确认标签
3. **标的**：多只自选股，从配置文件读取（用户填清单，代码不硬编码）
4. **推送**：企业微信群机器人 webhook（markdown 消息）

关键前提：**该指标乘数官方从未公开**，网上只有求助帖无源码。所以复刻结果是"行为等效、系数可调、尽量贴近截图"，无法保证与同花顺完全一致。环境：Python 3.13.12，akshare 未装。

## 二、文件清单（均在 /Users/holyli/stock/wave-monitor/）

| 文件 | 职责 |
| --- | --- |
| `main.py` | 主入口，编排 拉数→校验→算信号→去重→推送→日志；支持 `--once` / `--backtest` |
| `indicator.py` | 纯计算（无 IO）：MA7/MA13、CROSS、持有/观望规则，便于单测与回测复用 |
| `fetcher.py` | akshare 拉数、列名适配层、串行延迟、退避重试、东财 push2his 降级备选 |
| `notifier.py` | 企微 markdown 构造 + webhook POST + errcode 校验 + 频率控制 |
| `state_store.py` | 信号去重状态（`data/state.json`，原子写） |
| `utils.py` | 配置加载、日志、交易日历/时间工具 |
| `backtest.py` | 历史回测 + 导出信号 CSV，供截图对照调参 |
| `config.yaml` | 实际配置（用户填自选股清单与参数） |
| `config.example.yaml` | 带注释的配置模板 |
| `.env.example` | `WECOM_WEBHOOK_KEY=...` 示例（真实值存 `~/.env`） |
| `.gitignore` | 排除 `.env`、`logs/`、`data/`、`__pycache__/` |
| `requirements.txt` | 锁定 akshare / pandas / pyyaml / python-dotenv / tenacity 版本（须兼容 Py3.13） |
| `com.wave-monitor.plist.template` | launchd 模板（替换绝对路径后装入 `~/Library/LaunchAgents/`） |
| `tests/test_indicator.py` | 合成数据单测：金叉/死叉、去重、持有观望 |
| `README.md` | 安装、配置、装 launchd、调参步骤、免责说明 |
| `CONTEXT.md` | 需求背景、四项决策、调研结论（已随本次迁移写入） |

## 三、配置设计（config.yaml 要点）

- `general`: `timezone: Asia/Shanghai`、`log_level`、`dry_run`（调参期先 true）
- `fetch`: 串行间隔 0.8s、重试 3 次、退避 [1,3,8]s、`history_bars: 120`
- `indicator`: `rho: 1.000` + `rho_by_period: {m60: 1.000, daily: 1.000}`；`hold_rule`（`require_bullish_candle: true`、`turnover_threshold_60m: 1.0` 推测、`turnover_threshold_daily: 0.5` 标定）——状态机见 `indicator.py` 模块注释
- `periods`: `m60.enabled/push: true`、`daily.enabled/push: true`、`use_as_confirm: true`
- `dedup`: `merge_cross_period: false`（严格分别推送）、`key_by_bar_time: true`
- `notify`: `webhook_key_env: WECOM_WEBHOOK_KEY`（只存变量名）、`max_per_minute: 15`
- `symbols`: 用户填列表 `- {code: "300750", name: "宁德时代"}`，先用占位示例，清单由用户最终填入

系数方向（写进 README）：增大 K1 → 金叉提前/死叉滞后（偏多）；增大 K2 → 金叉滞后/死叉提前（偏空）。一次只动一个，步长 0.01。

## 四、指标算法（indicator.py，纯函数）

```
ma7 = close.rolling(7, min_periods=7).mean()
ma13 = close.rolling(13, min_periods=13).mean()
R = ma7 / ma13
cross_up   = (R > rho) & (R.shift(1) <= rho)
cross_down = (R < rho) & (R.shift(1) >= rho)
above = R > rho
hold  = above & (turnover >= thresh) & (close > open if require_bullish)  # 黄柱持有
state = "持有" if hold else "观望"
# signal_mode=state（默认）：捕捉二买/二卖
buy   = hold & ~hold.shift(1)
sell  = ~hold & hold.shift(1)
# signal_mode=cross：仅首次金叉/死叉
buy   = cross_up & hold
sell  = cross_down
```

- 列名适配为标准列 `time,open,close,high,low,turnover,volume`，按 time 升序。
- 只在**最新一根已闭合 K 线**上判定信号，不对历史段重复推送。
- 去重：`state.json` 以 `symbol|period` 记录 `last_bar_time` + `last_signal`；`bar_time <= last_bar_time` 则不推；原子写（临时文件 + `os.replace`）。

## 五、推送与调度

- 企微 webhook：`POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=KEY`，body `{"msgtype":"markdown","markdown":{"content":...}}`，校验 `errcode==0`；颜色 info绿/comment灰/warning红；滑动窗口限 15 条/分。
- 消息内容：标的代码名称、周期、买/卖点、触发价、MA7/MA13 与系数值、换手率、持有/观望状态、日线确认标签、时间、风险提示。
- launchd `StartCalendarInterval` 五时点：**10:33 / 11:33 / 14:03 / 15:05（60分钟收线后延迟）+ 15:10（日线）**；Python 与脚本均绝对路径；webhook key 从 `~/.env` 读，不写进 plist。
- 单次流程：加载配置 → 交易日判断（非交易日跳过）→ 逐标的逐周期：拉数 → 列名适配 → **闭合校验**（最后一根 time∈{10:30,11:30,14:00,15:00} 且成交量稳定，否则跳过/用上一根）→ 算信号 → 去重 → 推送 → 写状态与日志。单标的失败不影响其他，失败汇总告警。

## 六、调参与验证

1. `backtest.py` 拉近 N 根 60分钟/日线，导出 `time,close,ma7,ma13,fast,slow,cross_up,cross_down,hold` CSV，列出脚本复刻的 B/S 点时间价格。
2. 同花顺 Mac 打开同标的同周期截图（**统一用前复权 qfq**），把截图 B 点时间与回测 CSV 对比：脚本金叉偏晚 → 增大 K1 或减小 K2；偏早 → 反向。一次一个系数、步长 0.01。
3. 用同样方法核对黄柱/蓝柱区间，调 `turnover_threshold_*` 与 `require_bullish`。
4. 调好后写回 config.yaml，先 `dry_run: true` 跑 1-2 天，确认无误再开推送。

## 七、已验证 vs 推测

- 已验证：akshare 60分钟/日线含换手率与列名、收线四时点、企微 webhook 格式/颜色/限频、launchd 优于 cron、退避 1/3/8s、qfq 复权口径。
- 推测需调参：系数默认值、持有/观望规则与换手率阈值、日线二次确认策略、跨周期合并策略。

## 八、风险与免责

均线金叉/死叉信号本质滞后，收线后才确认；系数与持有规则为推测复刻，无法保证与同花顺一致；akshare/东财为免费爬虫接口，字段变更或限频时需维护（已做版本锁定、适配层、降级与重试）。脚本输出仅为技术指标参考，不构成投资建议，市场有风险，决策需独立判断。
