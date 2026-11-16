#!/usr/bin/env bash
# Linux 看門狗（由 zuvio-watchdog.timer 每 5 分鐘執行）：
#   bot 心跳超過 STALE 秒沒更新 → systemctl restart zuvio，並發 LINE 通知（每小時最多一則）
#   狀態為 stopped_by_user / exited_by_user / fatal → 不動它
# systemd 的 Restart=always 已處理「程序死掉」；這支補的是「程序活著但卡住」。
set -u
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
RUNTIME_DIR="${ZUVIO_RUNTIME_DIR:-$HOME/.local/state/zuvio}"
HB="$RUNTIME_DIR/zuvio_heartbeat.json"
STAMP="$RUNTIME_DIR/watchdog_last_notify"
STALE="${STALE:-600}"
NOTIFY_COOLDOWN=3600
LOG="$APP_DIR/zuvio_bot.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [WARNING] [看門狗] $*" >> "$LOG"; logger -t zuvio-watchdog "$*"; }

if [ ! -f "$HB" ]; then
    # 從未啟動或剛部署：交給 systemd（服務沒在跑就拉起來）
    systemctl is-active --quiet zuvio || { log "服務未執行且無心跳，啟動服務"; sudo systemctl start zuvio; }
    exit 0
fi

read -r STATE TS < <(python3 - "$HB" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d.get("state", "running"), int(float(d.get("ts", 0))))
EOF
)

case "$STATE" in
    stopped_by_user|exited_by_user|fatal) exit 0 ;;
esac

AGE=$(( $(date +%s) - TS ))
if [ "$AGE" -le "$STALE" ]; then
    exit 0
fi

log "心跳已 $((AGE / 60)) 分鐘未更新，重新啟動 zuvio 服務"
sudo systemctl restart zuvio

# LINE 通知（節流）：用專案 venv 的 Python 直接呼叫 Zuvio.send_line。
# LINE token 由 systemd 透過 EnvironmentFile=/etc/zuvio.env 帶入環境變數（檔案是 root-only，
# 這裡不能、也不該用 bash 去 source：密碼含 $ 或反引號時會被 shell 解讀）
NOW=$(date +%s)
LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ $((NOW - LAST)) -ge "$NOTIFY_COOLDOWN" ]; then
    (cd "$APP_DIR" && ZUVIO_LOG_FILE=/dev/null "$APP_DIR/.venv/bin/python" - "$AGE" <<'EOF'
import sys, Zuvio as Z
cfg = Z.ZuvioBot().load_config()
if cfg.get("line_enabled") and cfg.get("line_token") and cfg.get("line_user_id"):
    Z.send_line(cfg["line_token"], cfg["line_user_id"],
                f"🔁 Zuvio 點名助手看門狗（Oracle 主機）\n心跳已 {int(sys.argv[1]) // 60} 分鐘未更新，已自動重新啟動服務。")
EOF
    ) && echo "$NOW" > "$STAMP"
fi
