"""
Zuvio 點名助手看門狗

由工作排程器每 5 分鐘執行一次（見 deploy/install_autostart.ps1）。
讀取 bot 寫的心跳檔 zuvio_heartbeat.json：
  - 心跳超過 STALE_SECONDS 沒更新、或心跳裡的程序已不存在 → 清掉殘留程序，重新啟動 bot
  - 狀態是 stopped_by_user / exited_by_user / fatal → 是使用者或需要人工處理的情況，不動它
重啟時會寫一筆 log 到 zuvio_bot.log，並發 LINE 通知（每小時最多一則）。

手動執行可用 --dry-run 只看判斷結果、不做任何動作。
"""
import argparse
import ctypes
import datetime
import json
import os
import subprocess
import sys
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)
os.chdir(REPO_DIR)

import Zuvio as Z  # noqa: E402  （沿用同一份日誌、LINE 設定與孤兒 Chrome 清理）

TASK_NAME = 'ZuvioRollcallBot'
STALE_SECONDS = 600                      # 心跳超過 10 分鐘沒更新就視為卡死
NOTIFY_COOLDOWN = 3600                   # 看門狗的 LINE 通知每小時最多一則
STATE_FILE = os.path.join(Z.BASE_DIR, 'zuvio_watchdog_state.json')
SKIP_STATES = ('stopped_by_user', 'exited_by_user', 'fatal')

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def log(msg: str, level: str = 'info'):
    Z.log(f"[看門狗] {msg}", level=level)


# ──────────────────────────────────────────────
# 判斷邏輯（純函式，方便測試）
# ──────────────────────────────────────────────
def decide(heartbeat, now: float, pid_alive: bool):
    """回傳 (action, reason)，action 為 'ok' / 'skip' / 'restart'。"""
    if not heartbeat:
        return 'restart', '找不到心跳檔（bot 可能從未啟動或已被移除）'
    state = heartbeat.get('state', 'running')
    if state in SKIP_STATES:
        return 'skip', f'狀態為 {state}，不自動重啟'
    age = now - float(heartbeat.get('ts', 0))
    if age > STALE_SECONDS:
        return 'restart', f'心跳已 {int(age // 60)} 分鐘未更新（最後：{heartbeat.get("time", "?")}）'
    if not pid_alive:
        return 'restart', f'程序 PID {heartbeat.get("pid")} 已不存在'
    return 'ok', f'心跳 {int(age)} 秒前更新，程序存活'


# ──────────────────────────────────────────────
# 系統操作
# ──────────────────────────────────────────────
def read_heartbeat():
    try:
        with open(Z.HEARTBEAT_FILE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def is_pid_alive(pid) -> bool:
    """用 Win32 API 檢查程序是否存在且仍在執行（不會誤殺，不像 os.kill）。"""
    if not pid or os.name != 'nt':
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def is_bot_process(pid) -> bool:
    """確認該 PID 真的是本程式（避免 PID 被回收給別的程式後誤殺）。"""
    ps = (f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}';"
          "if ($p) { $p.Name + '|' + $p.CommandLine }")
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        out = (r.stdout or '').strip()
        name, _, cmd = out.partition('|')
        return name.lower() in ('python.exe', 'pythonw.exe', 'zuvio.exe') and 'zuvio' in cmd.lower()
    except Exception:
        return False


def kill_tree(pid):
    subprocess.run(['taskkill', '/PID', str(int(pid)), '/T', '/F'],
                   capture_output=True, timeout=30, creationflags=0x08000000)


def start_bot_task():
    """透過排程器重新啟動（會在使用者的桌面工作階段啟動 GUI --autostart）。"""
    flags = dict(capture_output=True, text=True, timeout=60, creationflags=0x08000000)
    subprocess.run(['schtasks', '/End', '/TN', TASK_NAME], **flags)   # 任務若仍標記為執行中，先結束
    r = subprocess.run(['schtasks', '/Run', '/TN', TASK_NAME], **flags)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or '').strip()[:200])


def notify_throttled(message: str):
    """LINE 通知，每 NOTIFY_COOLDOWN 秒最多一則（看門狗每次都是新程序，所以狀態存檔）。"""
    now = time.time()
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            last = float(json.load(f).get('last_notify', 0))
    except Exception:
        last = 0
    if now - last < NOTIFY_COOLDOWN:
        return
    cfg = Z.ZuvioBot().load_config()
    if cfg.get('line_enabled') and cfg.get('line_token') and cfg.get('line_user_id'):
        if Z.send_line(cfg['line_token'], cfg['line_user_id'], message):
            try:
                with open(STATE_FILE, 'w', encoding='utf-8') as f:
                    json.dump({'last_notify': now}, f)
            except Exception:
                pass


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Zuvio 點名助手看門狗")
    parser.add_argument('--dry-run', action='store_true', help='只顯示判斷結果，不做任何動作')
    args = parser.parse_args()

    hb = read_heartbeat()
    pid = hb.get('pid') if hb else None
    alive = is_pid_alive(pid) and is_bot_process(pid)
    action, reason = decide(hb, time.time(), alive)

    if args.dry_run:
        print(f"action={action} reason={reason} pid={pid} alive={alive}")
        return 0

    if action == 'ok':
        return 0
    if action == 'skip':
        return 0

    log(f"偵測到 bot 異常：{reason}。正在重新啟動...", level='warning')
    if alive:
        kill_tree(pid)
    Z.cleanup_orphan_chromes()
    try:
        start_bot_task()
    except Exception as e:
        log(f"重新啟動失敗：{e}", level='error')
        notify_throttled(f"🛑 Zuvio 點名助手看門狗：bot 異常（{reason}），而且重新啟動失敗，請手動檢查。")
        return 1
    log("已透過排程器重新啟動 bot。")
    now_str = datetime.datetime.now().strftime('%H:%M')
    notify_throttled(f"🔁 Zuvio 點名助手看門狗 {now_str}\nbot 異常（{reason}），已自動重新啟動。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
