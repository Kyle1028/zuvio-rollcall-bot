"""防卡死／LINE 通知行為測試（不登入 Zuvio、不啟動 Chrome，用假 driver 模擬）。

執行：python tests/test_resilience.py
"""
import os
import sys
import tempfile
import time
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['ZUVIO_LOG_FILE'] = os.path.join(tempfile.mkdtemp(), 'test.log')  # 不污染正式日誌
import Zuvio as Z  # noqa: E402
import urllib3  # noqa: E402
from selenium.common.exceptions import TimeoutException, WebDriverException  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# 1. is_driver_dead 分類
rte = urllib3.exceptions.ReadTimeoutError(None, "/session", "Read timed out. (read timeout=120)")
check("ReadTimeoutError 視為卡死", Z.is_driver_dead(rte))
check("invalid session id 視為卡死", Z.is_driver_dead(WebDriverException("invalid session id")))
check("一般 WebDriverException 不視為卡死", not Z.is_driver_dead(WebDriverException("element not interactable")))
check("ValueError 不視為卡死", not Z.is_driver_dead(ValueError("x")))
check("short_err 空訊息不崩潰", Z.short_err(WebDriverException("")) != "")

# 2. 通知節流 + 啟動通知一天一次
sent = []
Z.send_line = lambda tok, uid, msg: sent.append(msg) or True
bot = Z.ZuvioBot()
bot.line_token, bot.line_user_id = "tok", "Uxxx"
bot.notify("A", category="driver"); bot.notify("B", category="driver"); bot.notify("C", category="login")
check("同類異常 1 小時內只發 1 次", sent == ["A", "C"])
bot.notify("成功1"); bot.notify("成功2")
check("點名成功不節流", sent[-2:] == ["成功1", "成功2"])
n = len(sent)
bot.notify_startup(["課1"]); bot.notify_startup(["課1"])
check("啟動通知一天只發一次", len(sent) == n + 1)
check("未設定 LINE 時不發送", Z.ZuvioBot().notify("x") is False)


# 3. run_session：Chrome 卡死 → 第一門課就丟 DriverUnresponsive（而不是一門門白等）
class DeadDriver:
    page_source = ""
    current_url = "https://irs.zuvio.com.tw/student5/irs/index"
    def get(self, url): raise rte
    def execute_script(self, *a): raise rte
    def execute_async_script(self, *a): raise rte


bot3 = Z.ZuvioBot(); bot3.driver = DeadDriver(); bot3.is_active_time = lambda: True
t0 = time.time()
try:
    bot3.run_session({"1": "課A", "2": "課B"}); check("卡死時 run_session 丟出 DriverUnresponsive", False)
except Z.DriverUnresponsive:
    check("卡死時 run_session 丟出 DriverUnresponsive", True)
check("第一門課就判定，不逐門重試", time.time() - t0 < 2)


# 4. fetch 輪詢：開放／未開放／登出／fetch 失敗退回換頁
class FetchDriver:
    current_url = "https://irs.zuvio.com.tw/student5/irs/index"
    page_source = "簽到開放中"
    def __init__(self, result): self.result = result; self.navigated = []
    def execute_async_script(self, js, url): return self.result
    def get(self, url): self.navigated.append(url)
    def execute_script(self, *a): pass


ok = dict(ok=True, status=200, url="x", length=5000, page=True, login=False)
b = Z.ZuvioBot(); b.driver = FetchDriver(dict(ok, open=True))
check("fetch 回報開放 → True", b._rollcall_is_open("1") is True)
b.driver = FetchDriver(dict(ok, open=False))
check("fetch 回報未開放 → False，且不換頁", b._rollcall_is_open("1") is False and b.driver.navigated == [])
b.driver = FetchDriver(dict(ok=True, status=200, url="x", length=0, page=False, login=False, open=False))
try:
    b._rollcall_is_open("1"); check("登出（空回應）→ DriverUnresponsive", False)
except Z.DriverUnresponsive:
    check("登出（空回應）→ DriverUnresponsive", True)
b.driver = FetchDriver(dict(ok=False, error="TypeError: Failed to fetch"))
check("fetch 失敗 → 退回換頁判斷", b._rollcall_is_open("1") is True and len(b.driver.navigated) == 1)


# 5. 連續頁面逾時 → DriverUnresponsive
class TimeoutDriver:
    current_url = "https://irs.zuvio.com.tw/student5/irs/index"
    page_source = ""
    def execute_async_script(self, *a): raise TimeoutException("script timeout")
    def get(self, url): raise TimeoutException("page load")
    def execute_script(self, *a): raise TimeoutException("stop")


b5 = Z.ZuvioBot(); b5.driver = TimeoutDriver(); b5.is_active_time = lambda: True
b5.sleep_interruptible = lambda s: False
try:
    b5.run_session({str(i): f"課{i}" for i in range(6)}); check("連續 3 門課逾時 → DriverUnresponsive", False)
except Z.DriverUnresponsive as e:
    check("連續 3 門課逾時 → DriverUnresponsive", "3" in str(e))


# 6. _open_page：頁面載入逾時 → window.stop() 後繼續用目前內容判斷
class SlowPageDriver:
    page_source = "簽到開放中"; stopped = False
    def get(self, url): raise TimeoutException("page load timeout")
    def execute_script(self, js, *a):
        if "window.stop" in js: SlowPageDriver.stopped = True


b6 = Z.ZuvioBot(); b6.driver = SlowPageDriver(); b6._open_page("https://example.com")
check("頁面逾時會呼叫 window.stop() 而不是丟例外", SlowPageDriver.stopped)

# 7. quit_driver：quit() 卡住時，逾時後強制結束
Z.DRIVER_QUIT_TIMEOUT = 1
killed = []
Z._kill_driver_process = lambda d: killed.append(True)
class HangQuit:
    def quit(self): time.sleep(30)
t0 = time.time(); Z.quit_driver(HangQuit())
check("quit 卡住時 1 秒逾時後強制結束", killed and time.time() - t0 < 3)

# 8. monitor_loop：run_session 卡死 → 關閉並重開 Chrome、重新登入；定期換新不算異常
b8 = Z.ZuvioBot(); b8.is_active_time = lambda: True
b8.load_config = lambda: {"user": "u", "pass": "p"}
calls = {"start": 0, "login": 0, "session": 0, "stop": 0}
def fake_start(): calls["start"] += 1; b8.driver = object()
def fake_login(u, p): calls["login"] += 1
def fake_session(c):
    calls["session"] += 1
    if calls["session"] == 1: raise Z.DriverUnresponsive("模擬卡死")
    if calls["session"] == 2: raise Z.DriverRecycle()
    b8.stop_event.set()
def fake_stop(): calls["stop"] += 1; b8.driver = None
b8.start_driver, b8.login, b8.run_session, b8.stop_driver = fake_start, fake_login, fake_session, fake_stop
b8.get_courses = lambda: {"1": "課A"}
b8.sleep_interruptible = lambda s: b8.stop_event.is_set()
b8.monitor_loop()
check("卡死與定期換新後都重開 Chrome 並重新登入（啟動 3 次）", calls["start"] == 3 and calls["login"] == 3)
check("每次都有先關閉舊的 Chrome", calls["stop"] >= 2)


# 9. 登入輪詢：第 2.5 秒才出現課程頁也能成功；完全沒出現則判定失敗（兩種模式都丟例外）
class LoginDriver:
    def __init__(self, ok_after): self.t0 = time.time(); self.ok_after = ok_after
    def get(self, url): pass
    def find_element(self, *a): return self
    def send_keys(self, *a): pass
    def execute_script(self, *a): pass
    @property
    def page_source(self):
        return "全部課程" if self.ok_after is not None and time.time() - self.t0 > self.ok_after else "loading"
class FakeWait:
    def until(self, cond): return LoginDriver(None)


b9 = Z.ZuvioBot(); b9.driver = LoginDriver(2.5); b9.wait = FakeWait()
t0 = time.time()
try:
    b9.login("u", "p"); ok = True
except Exception:
    ok = False
check("登入頁 2.5 秒後才出現也判定成功", ok and 2 < time.time() - t0 < 6)
Z.LOGIN_TIMEOUT = 2
for mode, cb in (("GUI", lambda s: None), ("CLI", None)):
    b9.status_cb = cb; b9.driver = LoginDriver(None)
    try:
        b9.login("u", "p"); r = "no exception"
    except SystemExit:
        r = "sys.exit"
    except Exception:
        r = "exception"
    check(f"{mode} 模式登入失敗 → 丟例外交給 monitor_loop 重試", r == "exception")

# 10. save_config 會清掉舊 Telegram 欄位
import json
b10 = Z.ZuvioBot(); b10.config_file = os.path.join(tempfile.mkdtemp(), "settings.json")
b10.save_config({"user": "u", "tg_token": "x", "tg_chat_id": "y", "tg_enabled": True, "line_token": "t"})
saved = json.load(open(b10.config_file, encoding="utf-8"))
check("儲存時清掉 tg_* 欄位、保留 LINE 欄位", "tg_token" not in saved and saved.get("line_token") == "t")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
