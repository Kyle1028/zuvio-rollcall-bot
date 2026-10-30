import datetime
import logging
import os
import random
import sys
import time
import traceback
import json
import urllib.error
import urllib.request
import queue
import threading
import argparse
import re
import subprocess

import urllib3
from selenium import webdriver
import selenium.webdriver.chrome.webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

try:
    import winsound
    BEEP_AVAILABLE = True
except ImportError:
    BEEP_AVAILABLE = False

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox
    GUI_AVAILABLE = True
    GUI_BASE_CLASS = ctk.CTk
except ImportError:
    GUI_AVAILABLE = False
    GUI_BASE_CLASS = object


# ──────────────────────────────────────────────
# [1] 常數與全域設定 (路徑動態解析，確保 exe 能抓到同目錄設定檔與日誌)
# ──────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    # PyInstaller 打包後的執行檔目錄
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 一般 Python 腳本目錄
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE_PATH = os.path.join(BASE_DIR, 'settings.json')
# 可用環境變數 ZUVIO_LOG_FILE 改寫日誌路徑（測試時用，避免把假事件寫進正式日誌）
LOG_FILE_PATH = os.environ.get('ZUVIO_LOG_FILE') or os.path.join(BASE_DIR, 'zuvio_bot.log')
# 心跳檔：bot 每 HEARTBEAT_INTERVAL 秒更新一次，供 deploy/watchdog.py 判斷程式是否還活著
HEARTBEAT_FILE_PATH = os.path.join(BASE_DIR, 'zuvio_heartbeat.json')
HEARTBEAT_INTERVAL = 30

URI = "https://irs.zuvio.com.tw/student5/irs/rollcall/{}"
GUI_LOG_QUEUE = None  # 用於與 GUI 執行緒通訊的日誌佇列

# 掃描節奏（秒）—— 想調整點名掃描速度改這裡即可
SCAN_PER_COURSE_DELAY = 0.5   # 未開放點名時，單門課查詢後的間隔
SCAN_AFTER_HIT_DELAY  = 2     # 點名成功或發生例外後的間隔
SCAN_ROUND_MIN_DELAY  = 10    # 每掃完一輪課程後的最短休息
SCAN_ROUND_MAX_DELAY  = 20    # 每掃完一輪課程後的最長休息

# 防卡死設定（秒）
PAGE_LOAD_TIMEOUT      = 20   # 單一頁面最長載入時間；超過就停止載入、用已載入的內容判斷
DRIVER_COMMAND_TIMEOUT = 60   # 與 chromedriver 單次溝通的上限；超過視為 Chrome 卡死（Selenium 預設 120）
DRIVER_QUIT_TIMEOUT    = 15   # 正常關閉 Chrome 的等待上限；超過就強制結束程序
LOGIN_TIMEOUT          = 20   # 登入後最多等這麼久確認是否進到課程頁
SCRIPT_TIMEOUT         = 30   # execute_async_script（fetch 輪詢）的上限
CONSECUTIVE_TIMEOUT_LIMIT = 3 # 連續這麼多門課逾時就視為 Chrome 卡死、重開
DRIVER_RECYCLE_MINUTES = 60   # 同一個 Chrome 最多連續用這麼久就換新的（預防長時間運作後退化）
CHROME_MARKER = '--zuvio-rollcall-bot'  # 加在 Chrome 啟動參數上的標記，用來辨認並清除本程式留下的孤兒 Chrome

# LINE 通知節流（免費方案每月訊息數有限，異常類通知不能洗版）
NOTIFY_ALERT_COOLDOWN  = 3600 # 同類異常通知的最短間隔（秒）
DRIVER_RESTART_ALERT   = 3    # 30 分鐘內 Chrome 重開達此次數才發通知
LOGIN_FAIL_ALERT       = 3    # 連續登入失敗達此次數才發通知

# GUI 主題色票（集中管理，方便日後統一調整或做淺色主題）
COLOR_ACCENT  = "#1F6AA5"     # 標題／主按鈕強調色
COLOR_DIVIDER = "#3A3A3A"     # 分隔線／次要 hover 色

# GUI console 日誌最多保留行數，超過則自動捨去最舊的行，避免長時間執行吃記憶體
MAX_CONSOLE_LINES = 500


# ──────────────────────────────────────────────
# [2] 日誌模組
# ──────────────────────────────────────────────
# 使用明確的 FileHandler 指定 encoding，避免依賴 basicConfig 的 encoding 參數
# （該參數為 Python 3.9+ 才支援，直接寫在 basicConfig 會導致 3.8 執行時 TypeError）
_log_handler = logging.FileHandler(LOG_FILE_PATH, encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[_log_handler],
)

def log(msg: str, level: str = 'info'):
    """同時輸出到終端、日誌檔，並在 GUI 模式下傳送到 UI 佇列。"""
    print(msg)
    getattr(logging, level)(msg)
    if GUI_LOG_QUEUE is not None:
        GUI_LOG_QUEUE.put((level, msg))


# ──────────────────────────────────────────────
# [3] LINE 通知模組（LINE Messaging API）
# ──────────────────────────────────────────────
# LINE Notify 已於 2025/3/31 停止服務，因此改用 Messaging API 的 push message：
#   - Channel access token：LINE Developers 後台 → 你的 Messaging API channel →
#     Messaging API 分頁 → Channel access token (long-lived) → Issue
#   - User ID：同一個 channel 的 Basic settings 分頁最下方「Your user ID」（U 開頭 33 碼）
#   - 記得用手機加這個官方帳號為好友，否則推播會失敗
# 免費方案每月可發訊息數有限，所以只在重要事件發送（見 ZuvioBot.notify 的節流）。
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def send_line(channel_token: str, user_id: str, message: str) -> bool:
    """透過 LINE Messaging API 推播一則純文字訊息給指定使用者。"""
    try:
        payload = json.dumps({
            "to": user_id,
            "messages": [{"type": "text", "text": message[:5000]}],
        }).encode("utf-8")
        req = urllib.request.Request(
            LINE_PUSH_URL, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {channel_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        log(f"  [LINE 發送失敗] HTTP {e.code}：{detail}", level='warning')
        return False
    except Exception as e:
        log(f"  [LINE 發送失敗] {e}", level='warning')
        return False


def verify_line(channel_token: str, user_id: str) -> bool:
    msg = "✅ Zuvio 點名助手已連上 LINE！\n點名成功時會在這裡通知你。"
    return send_line(channel_token, user_id, msg)


# ──────────────────────────────────────────────
# [3.5] GPS 解析與轉換工具
# ──────────────────────────────────────────────
def parse_gps(gps_str: str):
    """
    將 GPS 字串解析為浮點數 (緯度, 經度)。
    支援兩種格式：
    1. 度分秒 DMS (例如: 25°01'18.6"N 121°27'48.2"E)
    2. 十進位 Decimal (例如: 25.021833, 121.463389)
    """
    gps_str = gps_str.strip()
    
    # 1. 檢查是否為十進位格式: 緯度, 經度 (例如: 25.021833, 121.463389)
    decimal_match = re.match(r'^([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)$', gps_str)
    if decimal_match:
        lat = float(decimal_match.group(1))
        lon = float(decimal_match.group(2))
        return lat, lon
        
    # 2. 檢查是否為度分秒 DMS 格式: 例如 25°01'18.6"N 121°27'48.2"E
    dms_pattern = r'(\d+)\s*[°\s]\s*(\d+)\s*[\'\s]\s*(\d+(?:\.\d+)?)\s*["”\s]\s*([NSns])\s*[,;\s]\s*(\d+)\s*[°\s]\s*(\d+)\s*[\'\s]\s*(\d+(?:\.\d+)?)\s*["”\s]\s*([EWew])'
    dms_match = re.search(dms_pattern, gps_str)
    if dms_match:
        lat_d = float(dms_match.group(1))
        lat_m = float(dms_match.group(2))
        lat_s = float(dms_match.group(3))
        lat_dir = dms_match.group(4).upper()
        
        lat = lat_d + (lat_m / 60.0) + (lat_s / 3600.0)
        if lat_dir == 'S':
            lat = -lat
            
        lon_d = float(dms_match.group(5))
        lon_m = float(dms_match.group(6))
        lon_s = float(dms_match.group(7))
        lon_dir = dms_match.group(8).upper()
        
        lon = lon_d + (lon_m / 60.0) + (lon_s / 3600.0)
        if lon_dir == 'W':
            lon = -lon
            
        return lat, lon
        
    raise ValueError("無法解析 GPS 格式")


# ──────────────────────────────────────────────
# [4] 瀏覽器工廠
# ──────────────────────────────────────────────
def build_driver(lat: float = None, lon: float = None) -> webdriver.Chrome:
    """建立並回傳一個新的 Chrome WebDriver 實例。"""
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-software-rasterizer')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument('--log-level=3')
    # Chrome 會忽略不認識的參數；這個標記只用來在工作管理員／清理程序裡辨認「這是本程式開的 Chrome」
    options.add_argument(CHROME_MARKER)

    # 設定 Geolocation 權限為允許 (1: Allow, 2: Block, 0: Ask)
    prefs = {
        "profile.default_content_setting_values.geolocation": 1
    }
    options.add_experimental_option("prefs", prefs)

    # 若透過環境變數指定了瀏覽器本體路徑（Linux/Oracle 上用 snap 版 Chromium 時必填），
    # 則明確指定，避免 Selenium 找不到或抓到錯誤的瀏覽器。
    chrome_bin = os.environ.get('ZUVIO_CHROME_BIN', '').strip()
    if chrome_bin:
        options.binary_location = chrome_bin

    # 同理，可用環境變數指定 chromedriver 路徑（例如 snap 的
    # /snap/bin/chromium.chromedriver），省去線上下載、也避免版本不合。
    chromedriver_path = os.environ.get('ZUVIO_CHROMEDRIVER', '').strip()

    driver = None
    errors = []

    # 嘗試方式 0：若指定了 chromedriver 路徑，優先使用（最可靠，離線也能跑）
    if chromedriver_path:
        try:
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            errors.append(f"指定的 chromedriver 啟動失敗: {e}")

    # 嘗試方式 1：使用 Selenium 4 內建的 Selenium Manager
    if driver is None:
        try:
            driver = webdriver.Chrome(options=options)
        except Exception as e:
            errors.append(f"Selenium Manager 啟動失敗: {e}")

    # 嘗試方式 2：使用 webdriver_manager 下載並載入驅動
    if driver is None:
        try:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
        except Exception as e:
            errors.append(f"webdriver_manager 啟動失敗: {e}")

    if driver is None:
        # 如果皆失敗，則拋出清楚的異常提示，供 GUI/CLI 捕獲
        error_msg = (
            "無法啟動瀏覽器。請確認已安裝 Google Chrome／Chromium；\n"
            "在 Linux（如 Oracle 主機）上，請用環境變數 ZUVIO_CHROME_BIN 與 "
            "ZUVIO_CHROMEDRIVER 指定 snap Chromium 的路徑。\n"
            "詳細錯誤資訊如下：\n" + "\n".join(errors)
        )
        raise RuntimeError(error_msg)

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })

    if lat is not None and lon is not None:
        driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
            "latitude": lat,
            "longitude": lon,
            "accuracy": 100
        })

    # 防卡死：頁面最多等 PAGE_LOAD_TIMEOUT 秒；與 chromedriver 的每次溝通最多等
    # DRIVER_COMMAND_TIMEOUT 秒（Selenium 預設 120 秒，Chrome 卡死時每個動作都要白等兩分鐘）
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(SCRIPT_TIMEOUT)
    client_config = getattr(driver.command_executor, '_client_config', None)
    if client_config is not None:
        client_config.timeout = DRIVER_COMMAND_TIMEOUT

    return driver


def cleanup_orphan_chromes():
    """清除本程式先前留下、但父程序已不在的無頭 Chrome／chromedriver（僅 Windows）。

    程式若被強制結束（關閉終端機、taskkill、當機），底下的 Chrome 常常不會跟著結束，
    累積起來會吃光記憶體並拖垮之後新開的 Chrome。只處理帶有 CHROME_MARKER 的 Chrome
    與父程序已消失的 chromedriver，不會動到使用者自己開的瀏覽器或正在運作中的實例。
    """
    if os.name != 'nt':
        return
    ps = (
        "$ps = Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'chrome.exe','chromedriver.exe' };"
        "$alive = @(Get-Process | Select-Object -ExpandProperty Id);"
        "$v = $ps | Where-Object { ($alive -notcontains $_.ParentProcessId) -and "
        f"( $_.Name -eq 'chromedriver.exe' -or $_.CommandLine -like '*{CHROME_MARKER}*' ) }};"
        "$v | ForEach-Object { taskkill /PID $_.ProcessId /T /F 2>&1 | Out-Null };"
        "($v | Measure-Object).Count"
    )
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, text=True, timeout=30, creationflags=0x08000000,
        )
        n = int((r.stdout or '0').strip().splitlines()[-1] or 0)
        if n:
            log(f"[驅動] 已清除 {n} 個先前殘留的無頭 Chrome／chromedriver 程序。")
    except Exception as e:
        log(f"[驅動] 清理殘留 Chrome 程序時發生錯誤（略過）：{short_err(e)}", level='warning')


class DriverUnresponsive(Exception):
    """Chrome / chromedriver 已沒有回應，必須關掉重開。"""


class DriverRecycle(Exception):
    """Chrome 已連續運作太久，依排程換一個新的（不是故障）。"""


# 出現在 WebDriverException 訊息中、代表瀏覽器已經掛掉的關鍵字
_DEAD_DRIVER_HINTS = (
    "invalid session id", "chrome not reachable", "disconnected",
    "no such window", "session deleted", "target window already closed",
)


def short_err(e: BaseException, limit: int = 150) -> str:
    """取例外訊息的第一行（Selenium 的訊息常夾帶很長的 stacktrace）。"""
    lines = str(e).strip().splitlines()
    return (lines[0] if lines else type(e).__name__)[:limit]


def is_driver_dead(e: BaseException) -> bool:
    """判斷例外是否代表 Chrome 已卡死／斷線（而不是單一頁面的問題）。"""
    # 與 chromedriver 的本機連線逾時或斷線（例：localhost:xxxxx Read timed out）
    if isinstance(e, (urllib3.exceptions.HTTPError, ConnectionError, TimeoutError)):
        return True
    if isinstance(e, WebDriverException):
        msg = str(e).lower()
        return any(hint in msg for hint in _DEAD_DRIVER_HINTS)
    return False


def _kill_driver_process(driver: webdriver.Chrome):
    """強制結束 chromedriver 及其底下的 Chrome 程序。"""
    proc = getattr(getattr(driver, 'service', None), 'process', None)
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            # /T 連同子程序（Chrome）一起結束；CREATE_NO_WINDOW 避免 --noconsole 的 exe 閃出黑窗
            subprocess.run(
                ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                capture_output=True, timeout=15, creationflags=0x08000000,
            )
        else:
            subprocess.run(['pkill', '-KILL', '-P', str(proc.pid)], capture_output=True, timeout=15)
            proc.kill()
    except Exception:
        pass


def quit_driver(driver: webdriver.Chrome):
    """安全地關閉 driver。

    Chrome 卡死時 driver.quit() 本身也會卡住，所以在背景執行緒呼叫、最多等
    DRIVER_QUIT_TIMEOUT 秒；逾時就直接強制結束程序，避免殘留的 Chrome 吃記憶體。
    """
    def _quit():
        try:
            driver.quit()
        except Exception:
            pass

    t = threading.Thread(target=_quit, daemon=True)
    t.start()
    t.join(DRIVER_QUIT_TIMEOUT)
    if t.is_alive():
        log("[驅動] Chrome 未在時限內關閉，強制結束程序。", level='warning')
        _kill_driver_process(driver)


# ──────────────────────────────────────────────
# [5] 自動點名機器人核心類別
# ──────────────────────────────────────────────
class ZuvioBot:
    def __init__(self):
        self.config_file = CONFIG_FILE_PATH
        self.line_token   = None
        self.line_user_id = None
        # 通知節流狀態：類別 → 上次發送時間；以及「今天是否已發過啟動通知」
        self._notify_last = {}
        self._startup_notified_date = None
        self._page_timeout_warned = False
        self.driver      = None
        self.driver_started_at = None
        self._last_heartbeat = 0.0
        self.wait        = None
        self.active_start = 8
        self.active_end   = 18
        # 課程名稱排除關鍵字（含任一關鍵字的課程不納入監控）；預設為空＝不過濾任何課程
        self.course_exclude_keywords = []

        # 執行緒與 GUI 控制變數
        self.stop_event  = threading.Event()
        self.status_cb   = None
        self.courses_cb  = None
        self.monitored_course_ids = None

    # ── 驅動管理 ────────────────────────────────
    def start_driver(self):
        """建立新的 driver 與 WebDriverWait，若啟用 GPS 則傳入模擬座標。"""
        log("[驅動] 正在啟動 Chrome...")
        lat, lon = None, None
        cfg = self.load_config()
        if cfg.get('gps_enabled', False) and cfg.get('gps_str', '').strip():
            try:
                lat, lon = parse_gps(cfg['gps_str'])
                log(f"[驅動] 已啟用 GPS 模擬定位：緯度 {lat:.6f}, 經度 {lon:.6f}")
            except Exception as e:
                log(f"[驅動] GPS 座標解析失敗，將使用系統預設位置。原因: {e}", level='warning')

        cleanup_orphan_chromes()
        self.driver = build_driver(lat, lon)
        self.wait   = WebDriverWait(self.driver, 20)
        self.driver_started_at = time.time()
        log("[驅動] Chrome 啟動完成。")

    def stop_driver(self):
        """安全關閉 driver 並釋放資源。"""
        if self.driver:
            log("[驅動] 正在關閉 Chrome，釋放記憶體...")
            quit_driver(self.driver)
            self.driver = None
            self.wait   = None
            log("[驅動] Chrome 已關閉。")

    # ── 設定管理 ─────────────────────────────
    def setup_config(self) -> dict:
        """命令列模式的首次設定引導。"""
        print("\n[首次設定] 請依照提示輸入相關資訊")
        print("-" * 40)

        email    = input("Zuvio 帳號（Email）: ").strip()
        # 密碼不做 strip，避免把使用者密碼中合法的前後空白去掉
        password = input("Zuvio 密碼         : ")

        print("\n── LINE 通知設定（LINE Messaging API）──")
        print("  如果不想設定，直接按 Enter 略過。")
        print("  Channel access token：LINE Developers → 你的 channel → Messaging API 分頁 → Issue")
        print("  User ID             ：同一個 channel 的 Basic settings 最下方「Your user ID」")
        print("  並記得用手機把這個官方帳號加為好友。")
        print("-" * 40)

        line_token   = input("Channel access token: ").strip()
        line_user_id = input("User ID（U 開頭）    : ").strip()

        cfg = {
            'user': email, 'pass': password,
            'active_start': 8, 'active_end': 18,
            'line_token': line_token, 'line_user_id': line_user_id,
        }

        if line_token and line_user_id:
            print("\n正在驗證 LINE 設定...")
            if verify_line(line_token, line_user_id):
                print(">>> [成功] LINE 通知設定正確！請確認手機是否收到測試訊息。")
                cfg['line_enabled'] = True
            else:
                print(">>> [警告] LINE 驗證失敗，請確認 token、User ID，以及是否已加官方帳號為好友。")
                if input("是否重新輸入？(y/N): ").strip().lower() == 'y':
                    return self.setup_config()
                cfg['line_enabled'] = False
        else:
            print(">>> [略過] 未設定 LINE，將不發送通知。")
            cfg['line_enabled'] = False

        self.save_config(cfg)

        return cfg

    def save_config(self, cfg: dict):
        """以原子方式寫入設定檔。

        先寫入同目錄的暫存檔，再用 os.replace 取代正式檔；os.replace 在同一
        檔案系統上是原子操作，可避免 GUI 主執行緒與背景監控執行緒同時讀寫時，
        讀到寫入到一半的半成品 JSON 而造成設定毀損。
        """
        # 已改用 LINE：順手清掉舊版遺留的 Telegram 欄位
        for legacy_key in ('tg_token', 'tg_chat_id', 'tg_enabled'):
            cfg.pop(legacy_key, None)
        tmp_path = self.config_file + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.config_file)

    def apply_runtime_config(self, cfg: dict):
        """把設定檔內容套用到 bot 的執行期屬性（時段、課程排除關鍵字）。"""
        self.active_start = cfg.get('active_start', 8)
        self.active_end = cfg.get('active_end', 18)
        self.course_exclude_keywords = cfg.get('course_exclude_keywords', []) or []

    @staticmethod
    def _apply_env_overrides(cfg: dict) -> dict:
        """用環境變數覆蓋設定（供雲端/伺服器非互動部署使用）。

        伺服器上建議把機密放在環境變數（例如 systemd 的 EnvironmentFile），
        而不要把密碼明文寫進 settings.json。有設定對應的環境變數時就以它為準：
          ZUVIO_USER / ZUVIO_PASS               → 帳號、密碼
          ZUVIO_LINE_TOKEN / ZUVIO_LINE_USER_ID → LINE（兩者皆有時自動啟用通知）
        在一般桌面（GUI）使用時不設這些變數，行為與原本完全相同。
        """
        env_user = os.environ.get('ZUVIO_USER')
        env_pass = os.environ.get('ZUVIO_PASS')
        env_token = os.environ.get('ZUVIO_LINE_TOKEN')
        env_line_user = os.environ.get('ZUVIO_LINE_USER_ID')

        if env_user is not None:
            cfg['user'] = env_user
        if env_pass is not None:
            cfg['pass'] = env_pass
        if env_token is not None:
            cfg['line_token'] = env_token
        if env_line_user is not None:
            cfg['line_user_id'] = env_line_user
        # 兩個 LINE 欄位都有值時，自動視為啟用通知
        if cfg.get('line_token') and cfg.get('line_user_id') and (env_token or env_line_user):
            cfg['line_enabled'] = True
        return cfg

    def load_config(self) -> dict:
        """載入設定檔，若不存在則回傳預設值（由主控程式處理 CLI 設定引導）。

        最後一律套用環境變數覆蓋，讓伺服器部署可完全不把機密寫進檔案。
        """
        cfg = None
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                # 補足預設時段設定
                if 'active_start' not in cfg:
                    cfg['active_start'] = 8
                if 'active_end' not in cfg:
                    cfg['active_end'] = 18
            except Exception as e:
                log(f"[設定] 讀取設定檔失敗，將改用預設值：{e}", level='warning')
                cfg = None

        if cfg is None:
            # 空白/預設設定
            cfg = {
                'user': '',
                'pass': '',
                'active_start': 8,
                'active_end': 18,
                'line_token': '',
                'line_user_id': '',
                'line_enabled': False
            }

        return self._apply_env_overrides(cfg)

    # ── 狀態與課程回呼 ───────────────────────────
    def update_status(self, status: str):
        if self.status_cb:
            self.status_cb(status)

    def update_courses(self, courses: dict):
        if self.courses_cb:
            self.courses_cb(courses)

    # ── 心跳（供看門狗判斷是否存活）──────────────────
    def write_heartbeat(self, state: str = 'running', force: bool = False):
        """把目前狀態寫進心跳檔。

        state：
          running         正常運作中（含時段外休眠）→ 看門狗發現心跳過期或程序消失會重啟
          stopped_by_user 使用者按了停止      → 看門狗不會動它
          exited_by_user  使用者關閉程式      → 看門狗不會動它
          fatal           需要人工處理的錯誤（例如帳密錯誤）→ 看門狗不會一直重啟
        一般呼叫每 HEARTBEAT_INTERVAL 秒最多寫一次；force=True 立即寫。
        """
        now = time.time()
        if not force and now - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        try:
            tmp = HEARTBEAT_FILE_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({
                    'pid': os.getpid(),
                    'ts': now,
                    'time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'state': state,
                }, f, ensure_ascii=False)
            os.replace(tmp, HEARTBEAT_FILE_PATH)
        except Exception:
            pass  # 心跳寫不進去不該影響監控本身

    # ── 可中斷的休眠 ─────────────────────────────
    def sleep_interruptible(self, seconds: float) -> bool:
        """可隨時被 stop_event 中斷的休眠。若被中斷回傳 True。"""
        step = 0.5
        slept = 0.0
        while slept < seconds:
            if self.stop_event.is_set():
                return True
            self.write_heartbeat()  # 所有迴圈都會經過這裡，順便更新心跳（內部有節流）
            time.sleep(step)
            slept += step
        return False

    def is_active_time(self) -> bool:
        """判斷現在是否在監控時段內，支援跨午夜時段（例如 22 點 ～ 6 點）。"""
        hour = datetime.datetime.now().hour
        if self.active_start == self.active_end:
            # 起訖相同視為全天監控，避免排程邏輯陷入無限等待
            return True
        if self.active_start < self.active_end:
            # 一般同日時段，例如 8 ～ 18
            return self.active_start <= hour < self.active_end
        # 跨午夜時段，例如 22 ～ 6：涵蓋 22:00～23:59 以及 00:00～05:59
        return hour >= self.active_start or hour < self.active_end

    def sleep_until_next_window(self):
        """計算並等待到下一個監控時段開始，支援 GUI 即時更新倒數。"""
        now = datetime.datetime.now()
        
        # 判斷今天的監控時段是否還沒開始
        today_start = datetime.datetime.combine(now.date(), datetime.time(self.active_start, 0))
        if now < today_start:
            wake_time = today_start
        else:
            tomorrow = now.date() + datetime.timedelta(days=1)
            wake_time = datetime.datetime.combine(tomorrow, datetime.time(self.active_start, 0))
        
        wait_secs = (wake_time - now).total_seconds()

        log(f"\n[排程] 現在 {now.strftime('%H:%M')}，超出監控時段。")
        log(f"[排程] 下次啟動時間：{wake_time.strftime('%Y-%m-%d %H:%M')}，"
            f"將休眠 {int(wait_secs // 3600)} 小時 {int((wait_secs % 3600) // 60)} 分鐘。\n")

        self.update_status(f"💤 下次啟動：{wake_time.strftime('%H:%M')}")

        while datetime.datetime.now() < wake_time:
            if self.stop_event.is_set():
                break
            remaining = (wake_time - datetime.datetime.now()).total_seconds()
            h = int(remaining // 3600)
            m = int((remaining % 3600) // 60)
            s = int(remaining % 60)
            
            if not self.status_cb:
                print(f"\r  💤 距離下次啟動還有 {h:02d}:{m:02d}:{s:02d} ...", end='', flush=True)
            else:
                self.update_status(f"💤 剩餘時間 {h:02d}:{m:02d}:{s:02d}")
                
            if self.sleep_interruptible(5):
                break
        
        if not self.status_cb:
            print()

    # ── 通知 ────────────────────────────────────
    def notify(self, message: str, category: str = None) -> bool:
        """發送 LINE 通知。

        指定 category 時會節流：同一類別在 NOTIFY_ALERT_COOLDOWN 秒內只發一次，
        避免異常狀況把免費方案的每月訊息額度洗光。點名成功等重要訊息不帶 category。
        """
        if not (self.line_token and self.line_user_id):
            return False
        if category:
            now = time.time()
            if now - self._notify_last.get(category, 0) < NOTIFY_ALERT_COOLDOWN:
                return False
            self._notify_last[category] = now
        return send_line(self.line_token, self.line_user_id, message)

    def notify_startup(self, course_names: list):
        """啟動通知：一天只發一次（Chrome 重開、重新登入時不重複發）。"""
        if not (self.line_token and self.line_user_id):
            return
        today = datetime.date.today()
        if self._startup_notified_date == today:
            return
        now_str          = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        course_list_text = '\n'.join(f"  • {name}" for name in course_names)
        msg = (
            f"🚀 Zuvio 點名助手已啟動\n"
            f"時間：{now_str}\n\n"
            f"監控課程（{len(course_names)} 門）：\n"
            f"{course_list_text}\n\n"
            f"監控時段：每日 {self.active_start:02d}:00 – {self.active_end:02d}:00\n"
            f"點名開放時將自動簽到並通知您。"
        )
        if send_line(self.line_token, self.line_user_id, msg):
            self._startup_notified_date = today
            log(">>> [LINE] 啟動通知已發送。")
        else:
            log(">>> [LINE] 啟動通知發送失敗。", level='warning')

    # ── 開啟頁面（防卡死）──────────────────────────
    def _open_page(self, url: str):
        """開啟頁面；超過 PAGE_LOAD_TIMEOUT 仍沒載完就停止載入，用已載入的內容繼續。

        有些頁面的資源一直載不完，會讓 driver.get() 卡住；點名狀態通常早就渲染好了，
        所以逾時後呼叫 window.stop() 停止載入即可，不必整頁放棄。
        Chrome 若已卡死，window.stop() 也會失敗並丟出例外，交給呼叫端判斷是否重開。
        """
        try:
            self.driver.get(url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")
            if not self._page_timeout_warned:
                log(f"[提醒] 頁面載入超過 {PAGE_LOAD_TIMEOUT} 秒，已停止載入並以目前內容判斷"
                    "（此提醒每次啟動只顯示一次）。", level='warning')
                self._page_timeout_warned = True

    # ── 點名狀態輪詢（不換頁）────────────────────
    # 在目前頁面內用 fetch 抓點名頁的 HTML 來判斷狀態。每次約 0.1 秒，而且不觸發
    # 整頁導覽，Chrome 不會因為一天幾千次換頁而逐漸退化（實測連續換頁約半小時後
    # 就會開始每頁載入逾時）。只有真的「簽到開放中」才切到該頁按簽到。
    _FETCH_STATUS_JS = """
        const url = arguments[0];
        const done = arguments[arguments.length - 1];
        fetch(url, {credentials: 'include', cache: 'no-store', redirect: 'follow'})
          .then(r => r.text().then(t => done({
              ok: true, status: r.status, url: r.url, length: t.length,
              open: t.includes('簽到開放中'),
              // 正常的點名頁一定有簽到按鈕（或至少有「簽到」字樣）；
              // 登入失效時 Zuvio 回 200 但內容是空的，或被導到登入頁
              page: t.includes('submit-make-rollcall') || t.includes('簽到'),
              login: t.includes('login-btn') || t.includes('id="password"'),
          })))
          .catch(e => done({ok: false, error: String(e)}));
    """

    def _rollcall_is_open(self, c_id: str) -> bool:
        """回傳該課程目前是否「簽到開放中」。

        fetch 失敗（例如網路瞬斷）時退回原本的換頁判斷；偵測到已被登出時丟出
        DriverUnresponsive，讓 monitor_loop 重開 Chrome 並重新登入。
        """
        url = URI.format(c_id)
        # fetch 需要在 Zuvio 同網域的頁面上執行才會帶 cookie
        if "zuvio.com.tw" not in (self.driver.current_url or ""):
            self._open_page("https://irs.zuvio.com.tw/student5/irs/index")

        result = self.driver.execute_async_script(self._FETCH_STATUS_JS, url)
        if not isinstance(result, dict) or not result.get('ok'):
            err = result.get('error') if isinstance(result, dict) else result
            log(f"[輪詢] fetch 失敗（{err}），改用換頁方式判斷。", level='warning')
            self._open_page(url)
            return "簽到開放中" in self.driver.page_source

        if result.get('login') or '/login' in (result.get('url') or '') or not result.get('page'):
            raise DriverUnresponsive(
                f"登入狀態已失效（回應 HTTP {result.get('status')}、{result.get('length', 0)} 字元），需要重新登入"
            )
        if result.get('status') != 200:
            log(f"[輪詢] 點名頁回應 HTTP {result.get('status')}，本輪視為未開放。", level='warning')
            return False
        return bool(result.get('open'))

    # ── 登入 ────────────────────────────────────
    def login(self, email: str, password: str):
        self._open_page("https://irs.zuvio.com.tw/")
        self.wait.until(EC.presence_of_element_located((By.ID, "email"))).send_keys(email)
        self.driver.find_element(By.ID, "password").send_keys(password)
        self.driver.execute_script(
            "arguments[0].click();",
            self.driver.find_element(By.ID, "login-btn")
        )

        # 不再固定等 5 秒：每秒檢查一次，一進到課程頁就算成功，最多等 LOGIN_TIMEOUT 秒
        log("正在驗證登入狀態...")
        logged_in = False
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            source = self.driver.page_source
            if "全部課程" in source or "我的課表" in source:
                logged_in = True
                break
            if self.sleep_interruptible(1):
                return

        if logged_in:
            log(">>> [成功] 登入成功！")
        else:
            # 不刪設定檔、也不結束程式：交給 monitor_loop 每 30 秒重試，連續失敗會發 LINE 提醒。
            # （驗證碼或網路瞬斷造成的失敗常常重試就好；真的是帳密錯誤再依提示修正）
            log(">>> [失敗] 登入失敗，可能是帳密錯誤或觸發驗證碼。", level='error')
            log(f">>> 若持續失敗，請確認 {self.config_file} 的 user／pass（或環境變數）是否正確。", level='error')
            raise Exception("登入失敗，請確認帳密。")

    # ── 取得課程 ─────────────────────────────────
    def _is_excluded_course(self, name: str) -> bool:
        """名稱含任一排除關鍵字則回傳 True。"""
        return any(kw and kw in name for kw in self.course_exclude_keywords)

    def get_courses(self) -> dict:
        self._open_page("https://irs.zuvio.com.tw/student5/irs/index")
        elements = self.wait.until(EC.presence_of_all_elements_located(
            (By.CLASS_NAME, 'i-m-p-c-a-c-l-c-b-t-course-name')
        ))
        return {
            x.get_attribute("data-course-id"): x.text
            for x in elements
            if not self._is_excluded_course(x.text)
        }

    # ── 單次監控 session ─────────────────────────
    def run_session(self, courses: dict):
        # 計算實際監控數量
        active_count = sum(1 for c_id in courses if self.monitored_course_ids is None or c_id in self.monitored_course_ids)
        log(f"[監控] 開始掃描，設定監控 {active_count}/{len(courses)} 門課程。")
        consecutive_driver_errors = 0
        consecutive_timeouts = 0

        while self.is_active_time() and not self.stop_event.is_set():
            # 同一個 Chrome 用太久就換新的，避免長時間運作後退化
            if self.driver_started_at and time.time() - self.driver_started_at > DRIVER_RECYCLE_MINUTES * 60:
                raise DriverRecycle()

            for c_id, c_name in courses.items():
                if self.stop_event.is_set():
                    return
                if not self.is_active_time():
                    log(f"[排程] 已到 {self.active_end:02d}:00，結束本日監控。")
                    return

                # 若限制監控特定課程且此課程不在列表中，則略過
                if self.monitored_course_ids is not None and c_id not in self.monitored_course_ids:
                    continue

                try:
                    if not self._rollcall_is_open(c_id):
                        consecutive_driver_errors = 0
                        consecutive_timeouts = 0
                        if self.sleep_interruptible(SCAN_PER_COURSE_DELAY):
                            return
                        continue

                    # 真的開放了才切到點名頁，再確認一次後按簽到
                    self._open_page(URI.format(c_id))
                    if "簽到開放中" not in self.driver.page_source:
                        log(f"[略過] {c_name}：切換到點名頁後狀態已非開放，下一輪再確認。", level='warning')
                        continue

                    self.driver.execute_script(
                        "arguments[0].click();",
                        self.driver.find_element(By.ID, "submit-make-rollcall")
                    )
                    consecutive_timeouts = 0

                    now_str = datetime.datetime.now().strftime('%H:%M:%S')
                    log(f"[{now_str}] ★ {c_name} | 點名成功！")

                    if BEEP_AVAILABLE:
                        winsound.Beep(1500, 800)

                    self.notify(
                        f"📋 Zuvio 點名成功！\n課程：{c_name}\n時間：{now_str}"
                    )
                    consecutive_driver_errors = 0

                except NoSuchElementException:
                    log(
                        f"[略過] {c_name}：找不到簽到按鈕，"
                        "可能尚未開放或頁面結構改變。",
                        level='warning'
                    )

                except TimeoutException:
                    consecutive_timeouts += 1
                    log(
                        f"[略過] {c_name}：頁面載入逾時（連續第 {consecutive_timeouts} 次），等待下一輪重試。",
                        level='warning'
                    )
                    # 連續多門課都逾時，幾乎可以確定是 Chrome 本身卡住了，不是單一頁面的問題
                    if consecutive_timeouts >= CONSECUTIVE_TIMEOUT_LIMIT:
                        raise DriverUnresponsive(f"連續 {consecutive_timeouts} 門課頁面載入逾時")

                except WebDriverException as e:
                    # Chrome 已卡死／斷線：不要再一門一門白等，交給 monitor_loop 重開
                    if is_driver_dead(e):
                        raise DriverUnresponsive(f"{c_name}：{short_err(e)}") from e
                    consecutive_driver_errors += 1
                    log(
                        f"[嚴重] WebDriver 錯誤（第 {consecutive_driver_errors} 次）"
                        f"｜課程：{c_name}｜{short_err(e, 200)}",
                        level='error'
                    )
                    if consecutive_driver_errors >= 5:
                        raise DriverUnresponsive("連續 5 次 WebDriver 錯誤") from e

                except Exception as e:
                    if is_driver_dead(e):
                        raise DriverUnresponsive(f"{c_name}：{short_err(e)}") from e
                    tb = traceback.format_exc()
                    log(f"[未知錯誤] {c_name}：{e}\n{tb}", level='error')

                if self.sleep_interruptible(SCAN_AFTER_HIT_DELAY):
                    return

            if self.sleep_interruptible(random.randint(SCAN_ROUND_MIN_DELAY, SCAN_ROUND_MAX_DELAY)):
                return

    # ── 監控主迴圈（CLI 與 GUI 共用）────────────────
    def monitor_loop(self):
        """背景監控主迴圈，CLI 與 GUI 模式共用。

        以 is_active_time() 判斷是否在監控時段；時段外呼叫 sleep_until_next_window()
        等待，時段內則啟動 Chrome、登入、抓課程並執行 run_session()。
        update_status()/update_courses() 在 CLI 模式（無 status_cb）下自動為 no-op，
        因此同一份流程可同時服務兩種模式。
        """
        log("=" * 45)
        log(" Zuvio 自動點名助手 - 開始監控")
        log("=" * 45)
        self.write_heartbeat('running', force=True)

        start_failures = 0     # 連續 Chrome 啟動失敗次數
        login_failures = 0     # 連續登入／取課失敗次數
        restart_times = []     # 近 30 分鐘內因卡死而重開 Chrome 的時間點

        while not self.stop_event.is_set():
            if not self.is_active_time():
                self.sleep_until_next_window()
                if self.stop_event.is_set():
                    break
                continue

            self.update_status("🔵 啟動 Chrome 中...")
            try:
                self.start_driver()
                start_failures = 0
            except Exception as e:
                start_failures += 1
                log(f"[驅動] Chrome 啟動失敗（連續第 {start_failures} 次）：{short_err(e, 300)}", level='error')
                self.update_status("⚠️ 啟動瀏覽器失敗")
                if start_failures >= 3:
                    self.notify(
                        f"⚠️ Zuvio 點名助手：Chrome 已連續 {start_failures} 次啟動失敗，"
                        "目前無法監控，請檢查電腦／主機。",
                        category='driver',
                    )
                if self.sleep_interruptible(10):
                    break
                continue

            try:
                self.update_status("🟡 登入 Zuvio 中...")
                cfg = self.load_config()
                self.apply_runtime_config(cfg)
                self.login(cfg['user'], cfg['pass'])

                if self.stop_event.is_set():
                    self.stop_driver()
                    break

                self.update_status("🔵 載入課程清單...")
                course_list = self.get_courses()
                self.monitored_course_ids = cfg.get('monitored_courses', None)
                self.update_courses(course_list)
                login_failures = 0

            except Exception as e:
                login_failures += 1
                log(f"[錯誤] 登入或取得課程失敗（連續第 {login_failures} 次）：{short_err(e, 300)}", level='error')
                self.update_status("⚠️ 登入或讀取失敗")
                if login_failures >= LOGIN_FAIL_ALERT:
                    self.notify(
                        f"⚠️ Zuvio 點名助手：已連續 {login_failures} 次登入失敗，"
                        "可能是帳密錯誤或遇到驗證碼，目前沒有在監控。",
                        category='login',
                    )
                self.stop_driver()
                if self.sleep_interruptible(30):
                    break
                continue

            if self.stop_event.is_set():
                self.stop_driver()
                break

            # 印出監控課程
            monitored_names = []
            for c_id, name in course_list.items():
                if self.monitored_course_ids is None or c_id in self.monitored_course_ids:
                    log(f"  • 鎖定監控中: {name}")
                    monitored_names.append(name)

            self.notify_startup(monitored_names)

            # 開始執行監控 session
            self.update_status("🟢 監控中")
            try:
                self.run_session(course_list)
            except DriverRecycle:
                log(f"[驅動] Chrome 已連續運作 {DRIVER_RECYCLE_MINUTES} 分鐘，依排程換新的以保持穩定。")
                self.update_status("🔄 定期更換 Chrome 中...")
                self.stop_driver()
                if self.sleep_interruptible(2):
                    break
                continue
            except DriverUnresponsive as e:
                # Chrome 卡死：關掉（必要時強制結束）後回到迴圈開頭，重開 Chrome 並重新登入
                now = time.time()
                restart_times = [t for t in restart_times if now - t < 1800] + [now]
                log(f"[驅動] Chrome 沒有回應（{e}），重新啟動瀏覽器並重新登入...", level='warning')
                self.update_status("🔄 Chrome 卡住，重新啟動中...")
                if len(restart_times) >= DRIVER_RESTART_ALERT:
                    self.notify(
                        f"⚠️ Zuvio 點名助手：30 分鐘內 Chrome 已卡住並重開 {len(restart_times)} 次，"
                        "監控可能不穩定，請留意。",
                        category='driver',
                    )
                self.stop_driver()
                if self.sleep_interruptible(5):
                    break
                continue

            # 關閉瀏覽器，等待下一次排程
            self.stop_driver()

            if self.stop_event.is_set():
                break

    # 保留舊名稱作為別名，維持相容
    def run_gui_loop(self):
        """GUI 背景執行緒入口（沿用舊名稱，實際走共用的 monitor_loop）。"""
        self.monitor_loop()


# ──────────────────────────────────────────────
# [6] CustomTkinter 介面設計類別
# ──────────────────────────────────────────────
class ZuvioGUI(GUI_BASE_CLASS):
    def __init__(self, bot_instance, autostart: bool = False):
        if not GUI_AVAILABLE:
            return
        super().__init__()
        self.bot = bot_instance
        self.bot_thread = None
        self.autostart = autostart
        
        # 視窗基本設定
        self.title("Zuvio 自動點名助手 - 2026 GUI版")
        self.geometry("980x680")
        self.minsize(900, 600)
        
        # 主題設定
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        # 註冊執行緒安全的日誌佇列
        global GUI_LOG_QUEUE
        self.log_queue = queue.Queue()
        GUI_LOG_QUEUE = self.log_queue
        
        # 版面配置
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        
        # 綁定變數
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.start_hour_var = tk.StringVar(value="8")
        self.end_hour_var = tk.StringVar(value="18")
        self.line_token_var = tk.StringVar()
        self.line_user_id_var = tk.StringVar()
        self.line_enabled_var = tk.BooleanVar(value=False)
        self.gps_enabled_var = tk.BooleanVar(value=False)
        self.gps_str_var = tk.StringVar()
        self.status_var = tk.StringVar(value="🔴 已停止")
        
        # 註冊 Bot 的狀態與課程回呼
        self.bot.status_cb = self.on_bot_status_change
        self.bot.courses_cb = self.on_bot_courses_loaded
        
        # 建立 UI 元件
        self.create_widgets()
        self.load_settings_into_ui()
        
        # 啟動日誌佇列監聽
        self.poll_log_queue()
        
        # 綁定視窗關閉事件
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # --autostart（開機自動啟動用）：視窗縮小到工作列，並在介面就緒後自動按「開始」
        if self.autostart:
            self.after(300, self.iconify)
            self.after(1500, self.start_bot)

    def create_widgets(self):
        # 1. 頂部狀態標題欄 (Row 0, 跨兩欄)
        self.header_frame = ctk.CTkFrame(self, corner_radius=10)
        self.header_frame.grid(row=0, column=0, columnspan=2, padx=15, pady=(15, 10), sticky="ew")
        
        self.title_label = ctk.CTkLabel(
            self.header_frame, 
            text="🚀 Zuvio 自動點名助手", 
            font=ctk.CTkFont(size=22, weight="bold")
        )
        self.title_label.pack(side="left", padx=20, pady=15)
        
        self.subtitle_label = ctk.CTkLabel(
            self.header_frame, 
            text="v1.2 Stable",
            font=ctk.CTkFont(size=12), 
            text_color="grey"
        )
        self.subtitle_label.pack(side="left", padx=(0, 20), pady=(20, 15))
        
        # 狀態 Badge 容器
        self.status_frame = ctk.CTkFrame(self.header_frame, height=40, fg_color="transparent")
        self.status_frame.pack(side="right", padx=20, pady=10)
        
        self.status_title = ctk.CTkLabel(self.status_frame, text="狀態：", font=ctk.CTkFont(size=14, weight="bold"))
        self.status_title.pack(side="left")
        
        self.status_badge = ctk.CTkLabel(
            self.status_frame, 
            textvariable=self.status_var, 
            font=ctk.CTkFont(size=14, weight="bold"), 
            corner_radius=6, 
            fg_color="#333333", 
            width=150, 
            height=28
        )
        self.status_badge.pack(side="left", padx=5)
        
        # 2. 左側設定欄位 (Row 1, Column 0)
        self.left_frame = ctk.CTkScrollableFrame(self, label_text="⚙️ 系統設定", label_font=ctk.CTkFont(size=14, weight="bold"))
        self.left_frame.grid(row=1, column=0, padx=(15, 7), pady=(0, 15), sticky="nsew")
        
        # Zuvio 帳號設定
        self.acc_lbl = ctk.CTkLabel(self.left_frame, text="Zuvio 帳號設定", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLOR_ACCENT)
        self.acc_lbl.pack(anchor="w", padx=10, pady=(10, 5))
        
        self.email_entry = ctk.CTkEntry(self.left_frame, placeholder_text="Zuvio 帳號 (Email)", textvariable=self.email_var, width=240)
        self.email_entry.pack(anchor="w", padx=10, pady=5)
        
        self.password_entry = ctk.CTkEntry(self.left_frame, placeholder_text="Zuvio 密碼", textvariable=self.password_var, show="*", width=240)
        self.password_entry.pack(anchor="w", padx=10, pady=5)
        
        self.show_pass_chk = ctk.CTkCheckBox(self.left_frame, text="顯示密碼", command=self.toggle_password_visibility, font=ctk.CTkFont(size=11))
        self.show_pass_chk.pack(anchor="w", padx=12, pady=2)
        
        # 時段設定
        self.hours_frame = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        self.hours_frame.pack(anchor="w", padx=10, pady=10)
        
        self.start_lbl = ctk.CTkLabel(self.hours_frame, text="監控起點：", font=ctk.CTkFont(size=12))
        self.start_lbl.grid(row=0, column=0, sticky="w", pady=2)
        self.start_combo = ctk.CTkComboBox(self.hours_frame, values=[str(i) for i in range(24)], variable=self.start_hour_var, width=65, height=24)
        self.start_combo.grid(row=0, column=1, sticky="w", pady=2)
        self.start_unit = ctk.CTkLabel(self.hours_frame, text=" 點", font=ctk.CTkFont(size=12))
        self.start_unit.grid(row=0, column=2, sticky="w", pady=2)
        
        self.end_lbl = ctk.CTkLabel(self.hours_frame, text="監控終點：", font=ctk.CTkFont(size=12))
        self.end_lbl.grid(row=1, column=0, sticky="w", pady=2)
        self.end_combo = ctk.CTkComboBox(self.hours_frame, values=[str(i) for i in range(24)], variable=self.end_hour_var, width=65, height=24)
        self.end_combo.grid(row=1, column=1, sticky="w", pady=2)
        self.end_unit = ctk.CTkLabel(self.hours_frame, text=" 點", font=ctk.CTkFont(size=12))
        self.end_unit.grid(row=1, column=2, sticky="w", pady=2)
        
        # GPS 定位模擬
        self.gps_divider = ctk.CTkFrame(self.left_frame, height=2, fg_color=COLOR_DIVIDER)
        self.gps_divider.pack(fill="x", padx=10, pady=15)
        
        self.gps_lbl = ctk.CTkLabel(self.left_frame, text="📍 GPS 定位模擬", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLOR_ACCENT)
        self.gps_lbl.pack(anchor="w", padx=10, pady=(5, 5))
        
        self.gps_switch = ctk.CTkSwitch(self.left_frame, text="啟用 GPS 模擬", variable=self.gps_enabled_var, font=ctk.CTkFont(size=12))
        self.gps_switch.pack(anchor="w", padx=10, pady=5)
        
        self.gps_entry = ctk.CTkEntry(
            self.left_frame, 
            placeholder_text="經緯度 (25.0218, 121.4633 或 25°01'18.6\"N...)", 
            textvariable=self.gps_str_var, 
            width=240
        )
        self.gps_entry.pack(anchor="w", padx=10, pady=5)
        
        # 分割線
        self.divider = ctk.CTkFrame(self.left_frame, height=2, fg_color=COLOR_DIVIDER)
        self.divider.pack(fill="x", padx=10, pady=15)
        
        # LINE 通知設定（LINE Messaging API）
        self.line_lbl = ctk.CTkLabel(self.left_frame, text="LINE 通知設定", font=ctk.CTkFont(size=14, weight="bold"), text_color=COLOR_ACCENT)
        self.line_lbl.pack(anchor="w", padx=10, pady=(5, 5))

        self.line_switch = ctk.CTkSwitch(self.left_frame, text="啟用 LINE 通知", variable=self.line_enabled_var, font=ctk.CTkFont(size=12))
        self.line_switch.pack(anchor="w", padx=10, pady=5)

        self.line_token_entry = ctk.CTkEntry(self.left_frame, placeholder_text="Channel access token", textvariable=self.line_token_var, show="*", width=240)
        self.line_token_entry.pack(anchor="w", padx=10, pady=5)

        self.line_user_entry = ctk.CTkEntry(self.left_frame, placeholder_text="User ID (U 開頭 33 碼)", textvariable=self.line_user_id_var, width=240)
        self.line_user_entry.pack(anchor="w", padx=10, pady=5)

        self.line_hint = ctk.CTkLabel(
            self.left_frame,
            text="※ 兩者皆在 LINE Developers 後台取得，\n   並需先用手機加官方帳號為好友。",
            font=ctk.CTkFont(size=11), text_color="grey", justify="left",
        )
        self.line_hint.pack(anchor="w", padx=10, pady=(0, 2))

        self.line_test_btn = ctk.CTkButton(self.left_frame, text="測試傳送通知", command=self.test_line_settings, fg_color="#2B2B2B", hover_color=COLOR_DIVIDER, font=ctk.CTkFont(size=12))
        self.line_test_btn.pack(anchor="w", padx=10, pady=(8, 15))
        
        # 儲存按鈕
        self.save_btn = ctk.CTkButton(self.left_frame, text="儲存所有設定", command=self.save_settings_from_ui, fg_color=COLOR_ACCENT, font=ctk.CTkFont(weight="bold"))
        self.save_btn.pack(anchor="w", padx=10, pady=(0, 20), fill="x")
        
        # 3. 右側監控與日誌欄 (Row 1, Column 1)
        self.right_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.right_frame.grid(row=1, column=1, padx=(7, 15), pady=(0, 15), sticky="nsew")
        self.right_frame.grid_rowconfigure(1, weight=1)
        self.right_frame.grid_rowconfigure(2, weight=2)
        self.right_frame.grid_columnconfigure(0, weight=1)
        
        # 控制按鈕區
        self.actions_frame = ctk.CTkFrame(self.right_frame, height=50)
        self.actions_frame.grid(row=0, column=0, pady=(0, 10), sticky="ew")
        
        self.start_btn = ctk.CTkButton(
            self.actions_frame, 
            text="▶ 開始自動點名", 
            command=self.start_bot, 
            fg_color="#2E7D32", 
            hover_color="#1B5E20", 
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.start_btn.pack(side="left", padx=15, pady=10, expand=True, fill="x")
        
        self.stop_btn = ctk.CTkButton(
            self.actions_frame, 
            text="⏹ 停止運行", 
            command=self.stop_bot, 
            fg_color="#C62828", 
            hover_color="#B71C1C", 
            font=ctk.CTkFont(size=14, weight="bold"), 
            state="disabled"
        )
        self.stop_btn.pack(side="left", padx=15, pady=10, expand=True, fill="x")
        
        # 鎖定課程面板
        self.courses_frame = ctk.CTkScrollableFrame(self.right_frame, label_text="🏫 鎖定監控課程", label_font=ctk.CTkFont(size=13, weight="bold"), height=100)
        self.courses_frame.grid(row=1, column=0, pady=(0, 10), sticky="nsew")
        self.courses_no_lbl = ctk.CTkLabel(self.courses_frame, text="（尚未開始監控，未載入課程）", text_color="grey", font=ctk.CTkFont(size=12))
        self.courses_no_lbl.pack(pady=15)
        
        # 日誌面板
        self.console_frame = ctk.CTkFrame(self.right_frame)
        self.console_frame.grid(row=2, column=0, sticky="nsew")
        self.console_frame.grid_rowconfigure(1, weight=1)
        self.console_frame.grid_columnconfigure(0, weight=1)
        
        self.console_lbl = ctk.CTkLabel(self.console_frame, text="📋 運行日誌 (Live Console Logs)", font=ctk.CTkFont(size=12, weight="bold"), anchor="w")
        self.console_lbl.grid(row=0, column=0, padx=10, pady=(5, 2), sticky="w")
        
        self.console_clear_btn = ctk.CTkButton(self.console_frame, text="清除", width=50, height=20, font=ctk.CTkFont(size=10), fg_color="#333333", command=self.clear_console)
        self.console_clear_btn.grid(row=0, column=0, padx=10, pady=(5, 2), sticky="e")
        
        self.console_text = ctk.CTkTextbox(self.console_frame, font=ctk.CTkFont(family="Courier", size=12))
        self.console_text.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        
        # 設定日誌顏色標籤 (Tkinter Text Tag 配置方式)
        self.console_text.tag_config("info", foreground="white")
        self.console_text.tag_config("success", foreground="#A3E635")
        self.console_text.tag_config("warning", foreground="#FACC15")
        self.console_text.tag_config("error", foreground="#F87171")
        self.console_text.tag_config("critical", foreground="#EF4444", background="#7F1D1D")
        self.console_text.configure(state="disabled")

    def toggle_password_visibility(self):
        if self.show_pass_chk.get() == 1:
            self.password_entry.configure(show="")
        else:
            self.password_entry.configure(show="*")

    def load_settings_into_ui(self):
        cfg = self.bot.load_config()
        self.email_var.set(cfg.get('user', ''))
        self.password_var.set(cfg.get('pass', ''))
        self.start_hour_var.set(str(cfg.get('active_start', 8)))
        self.end_hour_var.set(str(cfg.get('active_end', 18)))
        self.line_token_var.set(cfg.get('line_token', ''))
        self.line_user_id_var.set(cfg.get('line_user_id', ''))
        self.line_enabled_var.set(cfg.get('line_enabled', False))
        self.gps_enabled_var.set(cfg.get('gps_enabled', False))
        self.gps_str_var.set(cfg.get('gps_str', ''))
        
        # 同步 bot 的變數
        self.bot.active_start = int(self.start_hour_var.get())
        self.bot.active_end = int(self.end_hour_var.get())

    def save_settings_from_ui(self, show_alert=True) -> bool:
        email = self.email_var.get().strip()
        password = self.password_var.get()  # 不 strip，保留密碼中合法的前後空白
        start_hour = int(self.start_hour_var.get())
        end_hour = int(self.end_hour_var.get())
        line_token = self.line_token_var.get().strip()
        line_user_id = self.line_user_id_var.get().strip()
        line_enabled = self.line_enabled_var.get()
        gps_enabled = self.gps_enabled_var.get()
        gps_str = self.gps_str_var.get().strip()
        
        if not email or not password:
            messagebox.showerror("設定錯誤", "Zuvio 帳號與密碼為必填項目！")
            return False

        if gps_enabled:
            if not gps_str:
                messagebox.showerror("設定錯誤", "已啟用 GPS 模擬，但未輸入 GPS 座標位置！")
                return False
            try:
                parse_gps(gps_str)
            except ValueError as e:
                messagebox.showerror("設定錯誤", f"GPS 座標格式無效！\n{e}")
                return False
            
        cfg = self.bot.load_config()
        cfg['user'] = email
        cfg['pass'] = password
        cfg['active_start'] = start_hour
        cfg['active_end'] = end_hour
        cfg['line_token'] = line_token
        cfg['line_user_id'] = line_user_id
        cfg['line_enabled'] = line_enabled
        cfg['gps_enabled'] = gps_enabled
        cfg['gps_str'] = gps_str
        
        self.bot.save_config(cfg)

        # 同步至 bot 實例
        self.bot.active_start = start_hour
        self.bot.active_end = end_hour
        self.bot.line_token = line_token if line_enabled else None
        self.bot.line_user_id = line_user_id if line_enabled else None

        if show_alert:
            messagebox.showinfo("成功", "設定已儲存成功！")
        return True

    def test_line_settings(self):
        token = self.line_token_var.get().strip()
        user_id = self.line_user_id_var.get().strip()
        if not token or not user_id:
            messagebox.showerror("驗證失敗", "請輸入完整的 Channel access token 與 User ID。")
            return

        self.line_test_btn.configure(state="disabled", text="驗證中...")

        # 於背景線程執行 LINE 測試，防止 GUI 卡死
        def run_test():
            success = verify_line(token, user_id)
            self.after(0, lambda: self.on_line_test_complete(success))

        threading.Thread(target=run_test, daemon=True).start()

    def on_line_test_complete(self, success):
        self.line_test_btn.configure(state="normal", text="測試傳送通知")
        if success:
            messagebox.showinfo("成功", "LINE 驗證成功！\n請確認手機是否收到測試訊息。")
        else:
            messagebox.showerror(
                "失敗",
                "LINE 驗證失敗。\n請檢查 token 與 User ID，並確認已用手機加官方帳號為好友。\n"
                "（詳細原因見右側運行日誌）",
            )

    def clear_console(self):
        self.console_text.configure(state="normal")
        self.console_text.delete("1.0", tk.END)
        self.console_text.configure(state="disabled")

    def poll_log_queue(self):
        """定時掃描並列印日誌至 GUI 終端機。"""
        while not self.log_queue.empty():
            try:
                level, msg = self.log_queue.get_nowait()
                self.console_text.configure(state="normal")
                
                # 自動判斷日誌級別與關鍵字，渲染不同的顏色
                tag = "info"
                if "點名成功" in msg or "驗證成功" in msg or "啟動完成" in msg or "登入成功" in msg:
                    tag = "success"
                elif level in ("warning", "error", "critical"):
                    tag = level
                elif "錯誤" in msg or "失敗" in msg or "🛑" in msg:
                    tag = "error"
                elif "⚠️" in msg or "警告" in msg or "💤" in msg or "[排程]" in msg:
                    tag = "warning"
                    
                self.console_text.insert(tk.END, f"{msg}\n", tag)

                # 限制保留行數，超過上限就刪掉最舊的行，避免長時間執行持續吃記憶體
                line_count = int(self.console_text.index("end-1c").split(".")[0])
                if line_count > MAX_CONSOLE_LINES:
                    excess = line_count - MAX_CONSOLE_LINES
                    self.console_text.delete("1.0", f"{excess + 1}.0")

                self.console_text.see(tk.END)
                self.console_text.configure(state="disabled")
            except queue.Empty:
                break
        # 每 100ms 刷新一次
        self.after(100, self.poll_log_queue)

    def on_bot_status_change(self, status):
        # 此回呼可能從背景監控執行緒被呼叫，而 Tkinter 元件只能在主執行緒操作，
        # 因此透過 after(0, ...) 把實際的 UI 更新排程回主執行緒執行。
        self.after(0, self._apply_status_ui, status)

    def _apply_status_ui(self, status):
        self.status_var.set(status)
        # 動態調整 Badge 顏色
        if "監控中" in status:
            self.status_badge.configure(fg_color="#1B5E20", text_color="#A3E635")
        elif "休眠" in status or "排程" in status or "時間" in status or "剩餘" in status or "下次" in status:
            self.status_badge.configure(fg_color="#E65100", text_color="#FFE082")
        elif "錯誤" in status or "失敗" in status:
            self.status_badge.configure(fg_color="#B71C1C", text_color="#FF8A80")
        elif "已停止" in status:
            self.status_badge.configure(fg_color="#333333", text_color="white")
        else:
            self.status_badge.configure(fg_color="#0D47A1", text_color="#90CAF9")

    def on_bot_courses_loaded(self, courses):
        # 同樣可能由背景執行緒觸發，排程回主執行緒才能安全地增刪 widget。
        self.after(0, self._render_courses_ui, courses)

    def _render_courses_ui(self, courses):
        # 清除舊元件
        for child in self.courses_frame.winfo_children():
            child.destroy()
            
        if not courses:
            lbl = ctk.CTkLabel(self.courses_frame, text="（無監控中課程）", text_color="grey", font=ctk.CTkFont(size=12))
            lbl.pack(pady=15)
            return
            
        # 用於儲存核取方塊對應變數的字典
        self.course_checkboxes = {}
        
        # 若 bot.monitored_course_ids 為空或尚未設定，預設全選
        if self.bot.monitored_course_ids is None:
            self.bot.monitored_course_ids = list(courses.keys())
            
        for c_id, c_name in courses.items():
            var = tk.BooleanVar(value=(c_id in self.bot.monitored_course_ids))
            
            chk = ctk.CTkCheckBox(
                self.courses_frame, 
                text=f"{c_name}", 
                variable=var,
                font=ctk.CTkFont(size=12),
                command=self.on_course_check_toggled
            )
            chk.pack(anchor="w", padx=15, pady=4)
            self.course_checkboxes[c_id] = var

    def on_course_check_toggled(self):
        checked_ids = []
        for c_id, var in self.course_checkboxes.items():
            if var.get():
                checked_ids.append(c_id)
                
        # 即時同步至 bot 的監控目標
        self.bot.monitored_course_ids = checked_ids
        
        # 動態更新 settings.json 存檔
        try:
            cfg = self.bot.load_config()
            cfg['monitored_courses'] = checked_ids
            self.bot.save_config(cfg)
        except Exception as e:
            log(f"[設定] 儲存選取課程失敗: {e}", level='warning')

    def start_bot(self):
        # 自動存檔，確保背景執行緒讀到最新畫面輸入（不跳出成功提示）
        if not self.save_settings_from_ui(show_alert=False):
            return
            
        self.bot.stop_event.clear()
        
        # 禁用設定欄位，避免監控中修改
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.email_entry.configure(state="disabled")
        self.password_entry.configure(state="disabled")
        self.start_combo.configure(state="disabled")
        self.end_combo.configure(state="disabled")
        self.line_token_entry.configure(state="disabled")
        self.line_user_entry.configure(state="disabled")
        self.line_switch.configure(state="disabled")
        self.gps_switch.configure(state="disabled")
        self.gps_entry.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.line_test_btn.configure(state="disabled")
        
        self.status_var.set("🔵 啟動中...")
        self.on_bot_status_change("🔵 啟動中...")
        
        # 啟動背景執行緒
        self.bot_thread = threading.Thread(target=self.run_bot_thread, daemon=True)
        self.bot_thread.start()
        
    def run_bot_thread(self):
        """背景執行緒入口：外層是守護迴圈，監控迴圈發生未預期錯誤時自動重新開始。

        monitor_loop 只會在 stop_event 被設定（使用者按停止）時正常返回；
        其他任何例外都視為意外，記錄後等 30 秒再重來，而不是讓監控就此停擺。
        """
        try:
            while not self.bot.stop_event.is_set():
                try:
                    self.bot.monitor_loop()
                except Exception as e:
                    tb = traceback.format_exc()
                    log(f"[系統錯誤] 監控發生未預期錯誤，30 秒後自動重新開始：{e}\n{tb}", level='critical')
                    self.bot.notify(
                        f"⚠️ Zuvio 點名助手發生未預期錯誤，已自動重新開始監控。\n原因：{short_err(e, 120)}",
                        category='fatal',
                    )
                    self.bot.stop_driver()
                    if self.bot.sleep_interruptible(30):
                        break
        finally:
            # 走到這裡一定是使用者主動停止；告訴看門狗不要把它拉起來
            self.bot.write_heartbeat('stopped_by_user', force=True)
            self.after(0, self.on_bot_thread_finish)
            
    def on_bot_thread_finish(self):
        # 恢復設定欄位
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.email_entry.configure(state="normal")
        self.password_entry.configure(state="normal")
        self.start_combo.configure(state="normal")
        self.end_combo.configure(state="normal")
        self.line_token_entry.configure(state="normal")
        self.line_user_entry.configure(state="normal")
        self.line_switch.configure(state="normal")
        self.gps_switch.configure(state="normal")
        self.gps_entry.configure(state="normal")
        self.save_btn.configure(state="normal")
        self.line_test_btn.configure(state="normal")
        
        self.status_var.set("🔴 已停止")
        self.on_bot_status_change("🔴 已停止")

    def stop_bot(self):
        log("\n🛑 正在接收中斷訊號，停止運行中...")
        self.status_var.set("🟡 停止中...")
        self.on_bot_status_change("🟡 停止中...")
        self.stop_btn.configure(state="disabled")
        self.bot.stop_event.set()

    def on_closing(self):
        if self.stop_btn.cget("state") == "normal":
            if messagebox.askokcancel("關閉程式", "Zuvio 點名助手正在運行中，確定要結束程式嗎？"):
                self.bot.stop_event.set()
                self.bot.write_heartbeat('exited_by_user', force=True)
                self.bot.stop_driver()
                self.destroy()
        else:
            self.bot.write_heartbeat('exited_by_user', force=True)
            self.bot.stop_driver()
            self.destroy()


# ──────────────────────────────────────────────
# [7] 啟動入口
# ──────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zuvio 自動點名助手")
    parser.add_argument("--cli", action="store_true", help="以命令列 (CLI) 模式啟動")
    parser.add_argument("--autostart", action="store_true",
                        help="GUI 模式：啟動後自動開始監控並縮小視窗（供開機自動啟動使用）")
    args = parser.parse_args()

    # 如果指定 --cli，或是環境不支援 GUI（未安裝 customtkinter），則走命令列模式
    if args.cli or not GUI_AVAILABLE:
        if not GUI_AVAILABLE and not args.cli:
            print("\n⚠️ 偵測到未安裝 customtkinter，將以命令列 (CLI) 模式啟動。")
            print("💡 若要使用精美圖形介面 (GUI)，請執行: pip install customtkinter\n")
            
        # 以 pythonw（無主控台）執行時沒有終端機，不要清螢幕（os.system 會閃出一個黑窗）
        has_console = sys.stdout is not None and hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
        if has_console:
            os.system('cls' if os.name == 'nt' else 'clear')
        print("=" * 45)
        print(" Zuvio 自動點名助手 - 2026 重構穩定版 (CLI)")
        print("=" * 45)

        bot = ZuvioBot()

        # 讀取設定 (CLI)
        cfg = bot.load_config()
        if not cfg.get('user') or not cfg.get('pass'):
            # 伺服器/服務模式沒有 stdin，不能走互動式 setup_config()，否則會卡死。
            # 只有在真的有終端機 (tty) 時才引導設定；否則提示改用環境變數後退出。
            if sys.stdin is not None and sys.stdin.isatty():
                cfg = bot.setup_config()
            else:
                log(
                    "[設定] 找不到帳號或密碼，且目前非互動環境（無終端機）。\n"
                    "請設定環境變數 ZUVIO_USER / ZUVIO_PASS（或提供含帳密的 settings.json）後再啟動。",
                    level='critical',
                )
                sys.exit(1)
            
        bot.line_token   = cfg.get('line_token')   if cfg.get('line_enabled') else None
        bot.line_user_id = cfg.get('line_user_id') if cfg.get('line_enabled') else None
        bot.apply_runtime_config(cfg)

        # ── 主排程迴圈（與 GUI 共用同一份 monitor_loop）──────────
        # 外層同樣是守護迴圈：未預期錯誤 → 記錄、等 30 秒、重來；Ctrl+C 才真正結束
        try:
            while True:
                try:
                    bot.monitor_loop()
                    break
                except Exception as e:
                    tb = traceback.format_exc()
                    log(f"[系統錯誤] 監控發生未預期錯誤，30 秒後自動重新開始：{e}\n{tb}", level='critical')
                    bot.notify(
                        f"⚠️ Zuvio 點名助手發生未預期錯誤，已自動重新開始監控。\n原因：{short_err(e, 120)}",
                        category='fatal',
                    )
                    bot.stop_driver()
                    if bot.sleep_interruptible(30):
                        break
        except KeyboardInterrupt:
            log("\n[結束] 收到中斷信號，程式正在關閉...")
            bot.write_heartbeat('exited_by_user', force=True)
        except SystemExit:
            # 例如帳密錯誤：需要人工處理，標記後讓看門狗不要一直重啟
            bot.write_heartbeat('fatal', force=True)
            raise
        finally:
            bot.stop_driver()
            log("[結束] 程式已安全退出。")
    else:
        # 啟動 GUI 介面模式
        bot = ZuvioBot()
        app = ZuvioGUI(bot, autostart=args.autostart)
        app.mainloop()