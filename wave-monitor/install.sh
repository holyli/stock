#!/bin/bash
# 安装 wave-monitor 的定时任务（含「睡眠也能跑」的保活方案）。
#
# 做三件事：
#   1. 生成四个 launchd 任务：m60 扫描（盘中 5 个时点）、日线扫描（18:30）、
#      以及两段保活（09:25 钉到 15:15、18:25 钉 15 分钟）
#   2. 注册到当前用户的 GUI 域（不需要 root）
#   3. 用 pmset 设置每个交易日 09:25 自动唤醒（这一步需要管理员密码）
#
# 用法：bash install.sh                  # 全量安装（含 pmset 唤醒，需管理员密码）
#       SKIP_PMSET=1 bash install.sh    # 只重装 launchd 任务，不动唤醒计划
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJ/.venv/bin/python"
UID_NUM="$(id -u)"
LA="$HOME/Library/LaunchAgents"
WAKE_AT="${WAKE_AT:-09:25:00}"     # 可覆盖：WAKE_AT=08:55:00 bash install.sh

[ -x "$PY" ] || { echo "✗ 找不到可执行的 $PY"; exit 1; }
[ -f "$PROJ/wake_guard.sh" ] || { echo "✗ 缺少 wake_guard.sh（保活守卫）"; exit 1; }
chmod +x "$PROJ/wake_guard.sh" 2>/dev/null || true
mkdir -p "$PROJ/logs" "$LA"

gen() {  # $1=模板  $2=目标
  sed -e "s#__PYTHON__#$PY#g" -e "s#__PROJECT_DIR__#$PROJ#g" "$1" > "$2"
  # 沙箱/下载来的 plist 会带 com.apple.provenance，launchd 会拒绝注册（报 5: Input/output error）
  xattr -d com.apple.provenance "$2" 2>/dev/null || true
}

echo "== 1/4 生成 plist =="
gen "$PROJ/com.wave-monitor.plist.template"          "$LA/com.wave-monitor.plist"
gen "$PROJ/com.wave-monitor.daily.plist.template"    "$LA/com.wave-monitor.daily.plist"
gen "$PROJ/com.wave-monitor.awake.plist.template"    "$LA/com.wave-monitor.awake.plist"
gen "$PROJ/com.wave-monitor.awake.pm.plist.template" "$LA/com.wave-monitor.awake.pm.plist"
echo "   $LA/com.wave-monitor.plist        （m60，盘中 5 个时点）"
echo "   $LA/com.wave-monitor.daily.plist  （日线，交易日 18:30）"
echo "   $LA/com.wave-monitor.awake.plist  （盘中保活 09:25）"
echo "   $LA/com.wave-monitor.awake.pm.plist（傍晚保活 18:25）"

echo "== 2/4 注册 launchd =="
for label in com.wave-monitor com.wave-monitor.daily com.wave-monitor.awake com.wave-monitor.awake.pm; do
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  if launchctl bootstrap "gui/$UID_NUM" "$LA/$label.plist" 2>/dev/null; then
    echo "   ✓ $label 已注册"
  else
    echo "   ✗ $label 注册失败"
    echo "     若报 Input/output error，说明 plist 带了 com.apple.provenance 标记，"
    echo "     先执行：xattr -d com.apple.provenance \"$LA/$label.plist\"  然后重跑本脚本"
    exit 1
  fi
done
launchctl list | grep wave || true

echo "== 3/4 设置每个交易日 $WAKE_AT 自动唤醒（需要输入管理员密码）=="
if [ "${SKIP_PMSET:-0}" = "1" ]; then
  echo "   ⏭ SKIP_PMSET=1，跳过 pmset（唤醒计划保持不变）"
else
  sudo pmset repeat wakeorpoweron MTWRF "$WAKE_AT"
  echo "   ✓ 已设置"
fi

echo "== 4/4 验证 =="
echo "--- 唤醒计划 ---"
pmset -g sched | head -4
echo "--- 已注册任务 ---"
launchctl list | grep wave || echo "（未找到，请检查上面的注册输出）"
echo
echo "完成。日常生效方式："
echo "  · 交易日 $WAKE_AT 机器自动唤醒（合盖状态除外），caffeinate 保持到 15:15"
echo "  · 10:33 / 11:33 / 14:03 / 15:05 / 15:10 只跑 60 分钟，有信号才推企微"
echo "  · 18:30 单独跑一次日线买卖点（18:25 会先做 15 分钟保活）"
echo "  · 两个周期互不取数：60 分钟轮次不取日线，日线轮次不取 60 分钟"
echo "  · 日志：$PROJ/logs/launchd.out.log · launchd.daily.out.log · awake.out.log"
echo
echo "手动触发一次扫描："
echo "  60 分钟： launchctl kickstart -k gui/$UID_NUM/com.wave-monitor"
echo "  日线：   launchctl kickstart -k gui/$UID_NUM/com.wave-monitor.daily"
