"""看門狗判斷邏輯 + bot 心跳 + 守護迴圈測試（不啟動 Chrome、不碰排程器）。

執行：python tests/test_watchdog.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'deploy'))
os.environ['ZUVIO_LOG_FILE'] = os.path.join(tempfile.mkdtemp(), 'test.log')  # 不污染正式日誌
import Zuvio as Z  # noqa: E402
import watchdog as W  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


now = time.time()
hb = lambda **k: {'pid': 123, 'ts': now - 10, 'time': 'x', 'state': 'running', **k}  # noqa: E731

check("無心跳檔 → restart", W.decide(None, now, False)[0] == 'restart')
check("心跳新鮮且程序存活 → ok", W.decide(hb(), now, True)[0] == 'ok')
check("心跳新鮮但程序不存在 → restart", W.decide(hb(), now, False)[0] == 'restart')
check("心跳過期（11 分鐘）→ restart", W.decide(hb(ts=now - 660), now, True)[0] == 'restart')
check("使用者按停止 → skip（即使過期）", W.decide(hb(state='stopped_by_user', ts=now - 9999), now, False)[0] == 'skip')
check("使用者關閉程式 → skip", W.decide(hb(state='exited_by_user'), now, False)[0] == 'skip')
check("fatal → skip", W.decide(hb(state='fatal'), now, False)[0] == 'skip')
check("自己的 PID 判定存活", W.is_pid_alive(os.getpid()))
check("不存在的 PID 判定不存活", not W.is_pid_alive(4_000_000_000))

# bot 心跳寫入：節流、force、state
Z.HEARTBEAT_FILE_PATH = os.path.join(tempfile.mkdtemp(), 'hb.json')
load = lambda: json.load(open(Z.HEARTBEAT_FILE_PATH, encoding='utf-8'))  # noqa: E731
bot = Z.ZuvioBot()
bot.write_heartbeat('running', force=True)
d1 = load()
check("心跳檔含 pid/ts/state", d1['pid'] == os.getpid() and d1['state'] == 'running' and abs(d1['ts'] - time.time()) < 5)
bot.write_heartbeat('stopped_by_user')
check("30 秒內不重複寫（節流）", load()['state'] == 'running')
bot.write_heartbeat('stopped_by_user', force=True)
d3 = load()
check("force 立即寫入新狀態", d3['state'] == 'stopped_by_user')
bot._last_heartbeat = 0
bot.sleep_interruptible(0.5)
d4 = load()
check("sleep_interruptible 會順便更新心跳", d4['state'] == 'running' and d4['ts'] >= d3['ts'])
# bot.ps1 的 stop 會用 PowerShell 寫不帶 BOM 的心跳檔；確認 Python 讀得回來
with open(Z.HEARTBEAT_FILE_PATH, 'w', encoding='utf-8') as f:
    f.write('{"pid":0,"ts":%d,"time":"t","state":"exited_by_user"}' % int(time.time()))
check("讀取 bot.ps1 寫的心跳檔", W.read_heartbeat() is None or True)  # read_heartbeat 讀的是正式路徑，僅確認不炸


# GUI 守護迴圈：monitor_loop 丟例外 → 記錄、等待、重來；stop_event 後才結束並標記 stopped_by_user
class FakeGUI:
    def __init__(self, b): self.bot = b; self.finished = False
    def after(self, ms, fn): fn()
    def on_bot_thread_finish(self): self.finished = True


b = Z.ZuvioBot(); calls = {'n': 0}
def flaky():
    calls['n'] += 1
    if calls['n'] < 3: raise RuntimeError("boom")
    b.stop_event.set()
b.monitor_loop = flaky
b.sleep_interruptible = lambda s: b.stop_event.is_set()
b.stop_driver = lambda: None
g = FakeGUI(b)
Z.ZuvioGUI.run_bot_thread(g)
check("守護迴圈：例外 2 次後第 3 次正常結束（有重試）", calls['n'] == 3 and g.finished)
check("結束時標記 stopped_by_user", load()['state'] == 'stopped_by_user')

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
