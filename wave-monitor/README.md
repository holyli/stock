# 波段助手买点监控

复刻同花顺 Mac 客户端「波段助手」的核心行为：`R = MA7/MA13` 穿越阈值 `rho`（系数比 `k2/k1`），再叠加「收阳 + 换手达标」的持有过滤；在 60 分钟与日线两个周期分别计算，命中买卖点时推送企业微信群机器人。

> **重要前提**：同花顺该指标的乘数从未公开，本实现为「行为等效、系数可调、尽量贴近截图」的复刻版，不保证与客户端 B/S 点完全一致。仅供技术指标参考，不构成投资建议。

## 0. 2026-11-24 口径修正：60 分钟改为「收盘价 vs MA7」

起因：光线传媒 60 分钟图上，同花顺在 **11/23** 标了买点，本程序没标。逐根核对后结论如下。

| 项目 | 11/23 11:30 | 11/24 10:30 |
| --- | --- | --- |
| 收盘 / 开盘 | 10.33 / 10.19（阳线） | 11.47 / 10.86（阳线） |
| MA7 | **10.3286** | 10.6086 |
| MA13 | 10.6038 | 10.6031 |
| R = MA7/MA13 | **0.9740** | 1.0005 |
| 换手 | 0.32% | 1.89% |
| 同花顺 | **标「买」** | 已持有（无标记） |
| 旧口径（rho=0.994 金叉） | 无信号（R 比阈值低 2.0%） | 标「买」 |

- 11/23 11:30 是下跌以来**第一次收盘站上 MA7**（10.33 > 10.3286），同花顺的蓝柱正在这根翻橙、并标「买」。
- 而 MA7/MA13 金叉要等到 **11/24 10:30**，比同花顺晚 3 根——这正是「卖点相对准、买点几乎全错」的根因：卖点（趋势破坏）用金叉口径只是滞后，买点（底部转折）用金叉口径则系统性错过。
- 另外用固定 `rho` 反推也走不通：要命中 11/23 得令 `rho≈0.9739`，但那样全月只剩 2 个信号，与截图十余个买卖点明显不符，故排除。

**落地**：`signal_mode: ma7`（收盘上穿 MA7 标买、下穿标卖，柱色=是否站上 MA7），目前只对 `m60` 生效，日线仍用 `cross` 口径等日线截图复核。

**买点提前量**（2026-11-25 追加）：严格「等收盘站上 MA7」在急跌后要 2~6 根才成立，买点明显滞后。加 `ma7_lead_pct` 后，买点改为「收盘回到 MA7 下方 1% 以内即标买」，柱色与卖点仍按严格口径不变。实测（各 60 根，相对近 6 根最低点）：

| 提前量 | 宁德中位滞后 | 宁德买点 | 光线中位滞后 | 光线买点 |
| --- | --- | --- | --- | --- |
| 0（严格） | 3.0 根 | 6 | 1.0 根 | 5 |
| **1.0%（采用）** | **1.0 根** | 5 | **1.0 根** | 5 |
| 1.5% | 1.0 根 | 5 | 1.5 根 | 4（漏掉 11/09 买点） |

宁德每一笔都比严格口径提前（11/10 14:00→11/09 15:00、11/23 14:00→11/23 11:30 等），代价是光线多一笔 11/18 15:00 的假买。

```yaml
indicator:
  signal_mode: cross
  signal_mode_by_period:
    m60: ma7
    daily: cross
```

对比图：`python3 tools/compare_chart.py --code 300251 --name 光线传媒 --period m60 --out output/光线传媒_60m_信号口径对比.html`


## 1. 安装

```bash
cd /Users/holyli/stock/wave-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖已锁定：akshare 1.20.65 / pandas 2.3.3 / PyYAML 6.0.3 / python-dotenv 1.2.1 / tenacity 9.1.4 / requests 2.34.2，在 Python 3.13.12 上实测通过。akshare 1.20.46+ 把 `stock_zh_a_hist` 更名为 `stock_zh_a_hist_em`，代码已做双名兼容。

**行情源优先级**：本机 `westock` CLI（`~/.local/bin/westock`）→ akshare → 东财 push2his。westock 日线为前复权，m60 近 1 个月且强制不复权，换手率字段 `exchange` 在分钟线是千分位、已自动除以 100。东财 push2 被限流时脚本会立即停止重试，避免延长封禁。

## 2. 配置

1. 编辑 `config.yaml`：把 `symbols` 改成你自己的自选股清单（代码 6 位字符串）。
2. 把群机器人 key 写入 `~/.env`（**不要**写进项目文件、不要提交 git）：

```bash
echo 'WECOM_WEBHOOK_KEY=你的key' >> ~/.env
chmod 600 ~/.env
```

3. 调参期保持 `general.dry_run: true`：消息只写日志，不会真发。确认一两天后再改成 `false`。

配置关键项见 `config.example.yaml` 注释；指标说明见 `CONTEXT.md`，实施细节见 `PLAN.md`。

## 3. 常用命令

```bash
PY=/Users/holyli/stock/wave-monitor/.venv/bin/python

$PY main.py --once                       # 单次扫描（交易日 + 收线后才有意义）
$PY main.py --once --period m60 --force  # 忽略去重，验证消息内容
$PY main.py --backtest --code 300750 --period m60   # 导出指标 CSV 供截图对照
.venv/bin/python -m unittest discover -s tests -v   # 跑单测
```

回测 CSV 默认写到 `data/backtest_<代码>_<周期>.csv`，含 `time,close,ma7,ma13,fast,slow,ratio,above,cross_up,cross_down,hold,state,buy,sell`，并在终端列出复刻的买卖点。注意：分钟线数据源只提供近 1 个月左右（westock 约 60 根），长周期需另找数据源。

## 4. 定时（macOS launchd）

```bash
sed -e 's|__PROJECT_DIR__|/Users/holyli/stock/wave-monitor|g' \
    -e 's|__PYTHON__|/Users/holyli/stock/wave-monitor/.venv/bin/python|g' \
    /Users/holyli/stock/wave-monitor/com.wave-monitor.plist.template \
    > ~/Library/LaunchAgents/com.wave-monitor.plist
launchctl load ~/Library/LaunchAgents/com.wave-monitor.plist
```

触发时点（周一至周五）：**10:33 / 11:33 / 14:03 / 15:05** 扫 60 分钟，**15:10** 判日线。launchd 不继承 shell PATH，模板中 Python 与脚本都是绝对路径；密钥由脚本从 `~/.env` 读取，不写进 plist。

卸载：`launchctl unload ~/Library/LaunchAgents/com.wave-monitor.plist`

## 5. 调参流程

1. `backtest` 导出 CSV（含 `ratio`、`above`、`hold`、`state`、`buy`、`sell`），列出脚本复刻的买卖点。
2. 同花顺 Mac 打开同标的、同周期、**前复权 qfq** 的图，截图上的「买/卖」时间。
3. 只有比值 `rho = k2/k1` 有效，同比例缩放两个系数不改变任何信号。金叉偏晚 → 调小 `rho`；偏早 → 调大 `rho`。**步长 0.001**。
4. `signal_mode` 当前设为 `cross`：只在金叉/死叉首点出信号，**已关闭二买二卖**。`state` 模式会捕捉「从观望重新进入持有」的二次买点，覆盖率高但信号密集（光线买 70%/卖 65%、宁德买 75%/卖 50%），需要时再切回。黄/蓝柱不符 → 调 `turnover_threshold_*` 与 `require_bullish_candle`。
5. 日线用宁德 + 光线两样本标定：`rho_by_period.daily = 1.000`、换手阈值 0.5%。60 分钟采用**买卖双阈值死区**：买入 `rho=0.994`（金叉偏早）、卖出 `rho_sell=1.000`（死叉提前且不易抖动），换手阈值 0.1%。单用 0.994 会造成卖点滞后 3~5 根并在阈值附近反复触发假卖；单用 1.002 则宁德 live 数据一个信号都没有。双阈值在两者间取平衡。若切回 `state` 模式则用 `rho=0.995` / 0.25%。
6. 写回 `config.yaml`，继续 `dry_run: true` 跑 1–2 天，确认无误后开启推送。

## 6. 运行机制与风险

- 每次扫描：交易日判断 → 逐标的逐周期拉数（westock 优先，失败降级 akshare / 东财 push2his；识别到 push2 限流立即停重试）→ 闭合校验（丢弃盘中未收盘 K 线）→ 算信号 → 按 `标的|周期|K线时间` 去重 → 企微 markdown 推送（限 15 条/分钟）→ 原子写状态。
- 日线状态作为 60 分钟消息的二次确认标签，不改变买卖点判定。
- 免费行情接口可能变字段或限频；已做版本锁定、列名适配层、双通道降级与失败告警。
- 均线金叉死叉本质滞后，收线后才确认；信号仅供参考，市场有风险，决策需独立判断。

## 7. 文件说明

| 文件 | 职责 |
| --- | --- |
| `main.py` / `backtest.py` | 主入口 / 回测导出 |
| `indicator.py` / `fetcher.py` / `notifier.py` | 纯指标计算 / 双通道行情 / 企微推送 |
| `state_store.py` / `utils.py` | 去重写状态 / 配置日志交易日历闭合校验 |
| `config.yaml` / `config.example.yaml` / `.env.example` | 实际配置 / 模板 / 密钥示例 |
| `com.wave-monitor.plist.template` | launchd 模板 |
| `tests/test_indicator.py` | 合成数据单测 |
