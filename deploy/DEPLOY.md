# 部署到 Oracle Cloud（Always Free, ARM）

把 Zuvio 點名助手放到 Oracle 免費 ARM 主機 24 小時常駐,不用再開著自己的電腦。

> 架構:Ubuntu 24.04 (ARM / aarch64) + snap Chromium + Python venv + systemd 服務。
> 帳密放環境變數(不寫進專案目錄),時區設台灣,崩潰自動重啟。

---

## 0. 事前須知(先讀)

- **機房 IP 可能更容易觸發 Zuvio 登入驗證碼。** 從海外機房登入,若 Zuvio 跳出 captcha,bot 無法(也不應)自動破解,會登入失敗。這點只能實測,本指南無法保證。
- 代簽到的校規/責任風險請見專案根目錄 `README.md` 的免責聲明。

---

## 1. 開一台 Oracle Always Free ARM 執行個體

1. 登入 Oracle Cloud Console → **Compute → Instances → Create instance**。
2. **Image**:選 **Canonical Ubuntu 24.04**(Arm-based 版本)。
3. **Shape**:點 Change shape → **Ampere → VM.Standard.A1.Flex**。免費額度內可給 **1 OCPU / 6 GB RAM**(甚至更多),對這支 bot 綽綽有餘。
   - 若顯示「Out of capacity」,ARM 免費額度常缺貨,換個可用區(AD)或過一陣子再試。
4. **SSH keys**:上傳或下載你的金鑰,之後用來連線。
5. 建立後記下 **Public IP**。預設使用者為 `ubuntu`。

> 本指南**不需要開任何 Ingress 連接埠**(bot 只對外連線,不對外提供服務)。保持預設即可。

連線:

```bash
ssh ubuntu@<你的PublicIP>
```

---

## 2. 設定時區(重要)

監控時段用的是主機本地時間,Oracle 預設是 UTC,不改會差 8 小時。

```bash
sudo timedatectl set-timezone Asia/Taipei
timedatectl   # 確認 Time zone 顯示 Asia/Taipei
```

---

## 3. 安裝 Chromium(snap)與 Python

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git
sudo snap install chromium
```

確認 snap 有裝好這兩個指令(服務會用到):

```bash
ls -l /snap/bin/chromium /snap/bin/chromium.chromedriver
```

兩個都要存在。先手動試跑一次,確定 Chromium 能無頭啟動:

```bash
chromium --headless=new --no-sandbox --dump-dom https://example.com | head
```

有印出 HTML 就代表 Chromium 正常。

---

## 4. 放上專案程式碼

把整個專案放到 `/home/ubuntu/zuvio-rollcall-bot`。任選一種:

```bash
# 方式 A:用 git（若你的程式碼在 GitHub）
git clone <你的repo網址> /home/ubuntu/zuvio-rollcall-bot

# 方式 B:從本機 scp 上傳（在你自己的電腦執行）
scp -r "C:\path\to\zuvio-rollcall-bot" ubuntu@<PublicIP>:/home/ubuntu/zuvio-rollcall-bot
```

建立虛擬環境並安裝依賴:

```bash
cd /home/ubuntu/zuvio-rollcall-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> 伺服器不需要 `customtkinter`(那是 GUI 用的);程式偵測不到它會自動走 CLI。
> 若你不想裝 GUI 套件,`pip install selenium webdriver-manager` 即可,加上 `--cli` 啟動。

---

## 5. 放設定檔(非機密)

`settings.json` 只放**非機密**:時段、課程、GPS。帳密走環境變數(下一步)。

```bash
cp deploy/settings.server.example.json settings.json
nano settings.json   # 視需要改 active_start / active_end / gps
```

**想只監控特定課程?** 伺服器沒有 GUI 可以勾選。最簡單的做法:
先在你自己的電腦用 GUI 跑一次、勾好課程,它會產生含 `monitored_courses`(課程 ID 清單)的 `settings.json`,把那份複製上來即可(複製後可把裡面的 `user`/`pass` 刪掉,因為伺服器用環境變數)。
`monitored_courses` 維持 `null` = 監控全部課程。

---

## 6. 放機密(帳密 / LINE)

```bash
sudo cp deploy/zuvio.env.example /etc/zuvio.env
sudo nano /etc/zuvio.env          # 填入真實帳密與（選填）LINE token / User ID
sudo chmod 600 /etc/zuvio.env     # 只有 root 能讀
sudo chown root:root /etc/zuvio.env
```

---

## 7. 安裝 systemd 服務

```bash
sudo cp deploy/zuvio.service /etc/systemd/system/zuvio.service
# 若專案不是放在 /home/ubuntu/zuvio-rollcall-bot，請先編輯路徑：
# sudo nano /etc/systemd/system/zuvio.service

sudo systemctl daemon-reload
sudo systemctl enable --now zuvio      # 立即啟動並設定開機自動啟動
```

查看狀態與即時日誌:

```bash
systemctl status zuvio          # 看服務是否 active (running)
journalctl -u zuvio -f          # 即時跟看日誌（Ctrl+C 離開）
```

如果 LINE 有設定,啟動成功時手機應該會收到「已啟動」通知(一天只發一次)。LINE 的設定方式見專案根目錄 `README.md` 的「LINE 通知設定」。

---

## 8. 日常操作

```bash
sudo systemctl restart zuvio    # 改了 settings.json 後重啟套用
sudo systemctl stop zuvio       # 暫停
sudo systemctl start zuvio      # 恢復
journalctl -u zuvio --since today   # 看今天的日誌
```

改 `/etc/zuvio.env` 或 `settings.json` 後,都要 `sudo systemctl restart zuvio` 才會生效。

---

## 疑難排解

| 症狀 | 可能原因 / 處理 |
|---|---|
| 服務一直在 30 秒重啟 | 看 `journalctl -u zuvio -n 50`。常見是登入失敗(帳密錯/被 captcha)或 Chromium 路徑不對。 |
| `無法啟動瀏覽器` | 確認 `/snap/bin/chromium` 與 `/snap/bin/chromium.chromedriver` 都存在;`snap install chromium` 是否成功。 |
| 時段整個差 8 小時 | 時區沒設,回到第 2 步 `timedatectl set-timezone Asia/Taipei` 後重啟服務。 |
| 找不到帳號或密碼就退出 | `/etc/zuvio.env` 沒填或沒被讀到;`sudo systemctl show zuvio -p EnvironmentFiles` 確認,並重新 `daemon-reload`。 |
| snap Chromium 啟動怪異 | 確認服務檔有 `Environment=HOME=/home/ubuntu`;snap 需要可寫的 HOME。 |
