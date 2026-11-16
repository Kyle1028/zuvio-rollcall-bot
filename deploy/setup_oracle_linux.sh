#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  Zuvio 點名助手：Linux 主機一鍵安裝（Oracle Linux 9 / Ubuntu 22.04+，x86_64 或 aarch64）
#
#  在主機上、專案目錄裡執行：
#      bash deploy/setup_oracle_linux.sh
#  做的事：設時區 → 裝 Chromium + chromedriver + Python → 建 venv → 準備 settings.json 與
#  /etc/zuvio.env → 安裝 systemd 服務（開機自啟、崩潰重啟）→ 安裝看門狗 timer（每 5 分鐘）
#  可重複執行（冪等）；不會覆蓋已存在的 settings.json 與 /etc/zuvio.env。
# ──────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"
RUN_HOME="$HOME"
RUNTIME_DIR="$RUN_HOME/.local/state/zuvio"
PY=""

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ── 1. 時區 ───────────────────────────────────────────────────
say "設定時區 Asia/Taipei"
sudo timedatectl set-timezone Asia/Taipei || true

# ── 2. 套件 ───────────────────────────────────────────────────
if command -v dnf >/dev/null 2>&1; then
    say "Oracle Linux / RHEL 系：安裝 EPEL、Chromium、Python"
    if grep -qi 'oracle' /etc/os-release; then
        sudo dnf install -y oracle-epel-release-el9 >/dev/null
    else
        sudo dnf install -y epel-release >/dev/null
    fi
    sudo dnf install -y chromium chromedriver git >/dev/null
    # 優先用新版 Python（3.12 → 3.11 → 系統 python3）
    for cand in python3.12 python3.11 python3; do
        if sudo dnf install -y "$cand" >/dev/null 2>&1 && command -v "$cand" >/dev/null; then PY="$cand"; break; fi
    done
    [ "$PY" = "python3" ] && sudo dnf install -y python3-pip >/dev/null || true
    CHROME_BIN="$(command -v chromium-browser || command -v chromium)"
    CHROMEDRIVER="$(command -v chromedriver)"
elif command -v apt-get >/dev/null 2>&1; then
    say "Ubuntu / Debian 系：安裝 snap Chromium、Python"
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv python3-pip git snapd >/dev/null
    sudo snap install chromium >/dev/null
    PY="python3"
    CHROME_BIN="/snap/bin/chromium"
    CHROMEDRIVER="/snap/bin/chromium.chromedriver"
else
    echo "不支援的發行版（需要 dnf 或 apt-get）"; exit 1
fi
echo "Python=$PY  Chrome=$CHROME_BIN  chromedriver=$CHROMEDRIVER"
[ -x "$CHROME_BIN" ] && [ -x "$CHROMEDRIVER" ] || { echo "找不到 Chromium 或 chromedriver"; exit 1; }

say "確認 Chromium 能無頭啟動"
"$CHROME_BIN" --headless=new --no-sandbox --disable-gpu --dump-dom https://example.com 2>/dev/null | grep -q "Example Domain" \
    && echo "Chromium OK" || { echo "Chromium 無頭啟動失敗"; exit 1; }

# ── 3. Python venv（伺服器只需要 selenium 與 webdriver-manager）──────
say "建立 Python 虛擬環境"
cd "$APP_DIR"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q "selenium>=4.15,<5" "webdriver-manager>=4.0,<5"
.venv/bin/python -c "import selenium; print('selenium', selenium.__version__)"

# ── 4. 設定檔與機密 ───────────────────────────────────────────
say "準備設定檔"
mkdir -p "$RUNTIME_DIR"
if [ ! -f settings.json ]; then
    cp deploy/settings.server.example.json settings.json
    echo "已建立 settings.json（預設 8～18 點、監控全部課程），需要再 nano settings.json"
else
    echo "settings.json 已存在，保留"
fi
if [ ! -f /etc/zuvio.env ]; then
    sudo cp deploy/zuvio.env.example /etc/zuvio.env
    sudo chmod 600 /etc/zuvio.env; sudo chown root:root /etc/zuvio.env
    echo "已建立 /etc/zuvio.env（範本）——請 sudo nano /etc/zuvio.env 填入 ZUVIO_USER / ZUVIO_PASS（與選填的 LINE）"
    NEED_ENV=1
else
    echo "/etc/zuvio.env 已存在，保留"
    NEED_ENV=0
fi

# ── 5. systemd 服務 ───────────────────────────────────────────
say "安裝 systemd 服務 zuvio"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@HOME@|$RUN_HOME|g" \
    -e "s|@CHROME_BIN@|$CHROME_BIN|g" -e "s|@CHROMEDRIVER@|$CHROMEDRIVER|g" -e "s|@RUNTIME_DIR@|$RUNTIME_DIR|g" \
    deploy/zuvio.service | sudo tee /etc/systemd/system/zuvio.service >/dev/null

# ── 6. 看門狗 timer（每 5 分鐘）────────────────────────────────
say "安裝看門狗 timer"
chmod +x deploy/watchdog_linux.sh
sudo tee /etc/systemd/system/zuvio-watchdog.service >/dev/null <<EOF
[Unit]
Description=Zuvio 點名助手看門狗（檢查心跳，卡住就重啟 zuvio）

[Service]
Type=oneshot
User=$RUN_USER
Environment=HOME=$RUN_HOME
Environment=ZUVIO_RUNTIME_DIR=$RUNTIME_DIR
Environment=APP_DIR=$APP_DIR
ExecStart=/usr/bin/bash $APP_DIR/deploy/watchdog_linux.sh
EOF
sudo tee /etc/systemd/system/zuvio-watchdog.timer >/dev/null <<EOF
[Unit]
Description=每 5 分鐘執行 Zuvio 看門狗

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
# 看門狗需要免密碼重啟服務
echo "$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart zuvio, /usr/bin/systemctl start zuvio" \
    | sudo tee /etc/sudoers.d/zuvio-watchdog >/dev/null
sudo chmod 440 /etc/sudoers.d/zuvio-watchdog

sudo systemctl daemon-reload
sudo systemctl enable zuvio-watchdog.timer >/dev/null
sudo systemctl start zuvio-watchdog.timer

# ── 7. 啟動 ───────────────────────────────────────────────────
if [ "${NEED_ENV:-0}" = "1" ]; then
    say "尚未填入帳密，先不啟動服務。填好 /etc/zuvio.env 後執行："
    echo "    sudo systemctl enable --now zuvio"
else
    say "啟動服務"
    sudo systemctl enable --now zuvio
    sleep 3
    systemctl --no-pager --lines=0 status zuvio | head -3
fi

say "完成。常用指令："
cat <<'EOF'
    systemctl status zuvio            # 服務狀態
    journalctl -u zuvio -f            # 即時日誌
    tail -f zuvio_bot.log             # 程式日誌
    sudo systemctl restart zuvio      # 改設定後重啟
    systemctl list-timers zuvio*      # 看門狗排程
EOF
