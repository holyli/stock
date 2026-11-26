#!/bin/bash
# 保活守卫：只在 A 股或港股交易日启动 caffeinate 把机器钉醒，
# 非交易日直接退出，让机器跑完就回去睡。
#
# 用法：
#   wake_guard.sh                  # 默认钉 21000 秒（09:25 → 15:15，覆盖盘中 5 个 60 分钟时点）
#   wake_guard.sh --minutes 900    # 钉 15 分钟（18:25 → 18:40，覆盖 18:30 的日线轮次）
#
# 由 launchd 任务 com.wave-monitor.awake（09:25）与 com.wave-monitor.awake.pm（18:25）调用。
set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJ/.venv/bin/python"
LOG="$PROJ/logs/awake.out.log"
HOLD_SECONDS=21000
HOLD_LABEL="15:15"

if [ "${1:-}" = "--minutes" ] && [ -n "${2:-}" ]; then
    HOLD_SECONDS=$(( $2 * 60 ))
    HOLD_LABEL="$(date -v +"$2"M "+%H:%M" 2>/dev/null || echo "+${2} 分钟")"
fi
mkdir -p "$PROJ/logs"

stamp() { date "+%Y-%m-%d %H:%M:%S"; }

if "$PY" - "$PROJ" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
from utils import trading_day_status
st = trading_day_status()
print(f'{st["date"]} 交易日检查：A股={st["cn"]} 港股={st["hk"]} → {"扫描" if st["any"] else "休市"}')
sys.exit(0 if st["any"] else 1)
EOF
then
    echo "$(stamp) 交易日，启动 caffeinate 保活至 ${HOLD_LABEL}（${HOLD_SECONDS}s）" >> "$LOG"
    exec /usr/bin/caffeinate -ims -t "$HOLD_SECONDS"
else
    echo "$(stamp) 非交易日，不保活，caffeinate 跳过" >> "$LOG"
    exit 0
fi
