<#
.SYNOPSIS
  讓 Zuvio 點名助手在 Windows 登入時自動啟動，並安裝看門狗（工作排程器）。

.DESCRIPTION
  註冊兩個排程工作：
    ZuvioRollcallBot  登入後 30 秒以 pythonw.exe（無主控台視窗）執行 Zuvio.py：
                        預設        --autostart  GUI 自動按「開始」並縮小到工作列
                        -Tray       --autostart --tray  視窗隱藏，右下角系統匣圖示顯示狀態
                                                 與操作（建議；需 pip install pystray Pillow）
                        -Background --cli        完全無視窗在背景執行（狀態看 log／LINE，
                                                 管理用 deploy\bot.ps1）
                      程序異常結束時排程器會自動重啟（最多 3 次，間隔 1 分鐘）。
    ZuvioWatchdog     每 5 分鐘執行 deploy\watchdog.py：bot 心跳超過 10 分鐘沒更新或程序
                      消失就清掉殘留程序並重新啟動 bot（使用者自己按停止／關閉時不會重啟）。

  用法（在專案目錄開 PowerShell）：
    .\deploy\install_autostart.ps1              # 安裝／更新（GUI 縮小到工作列）
    .\deploy\install_autostart.ps1 -Tray        # 安裝／更新（系統匣圖示模式，建議）
    .\deploy\install_autostart.ps1 -Background  # 安裝／更新（完全無視窗背景執行）
    .\deploy\install_autostart.ps1 -Uninstall   # 移除兩個工作
    .\deploy\install_autostart.ps1 -UseExe      # bot 改用 dist\Zuvio.exe（需把 settings.json 放在 dist\）

  若出現「因為這個系統上已停用指令碼執行」，先執行：
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>
param(
    [switch]$Uninstall,
    [switch]$UseExe,
    [switch]$Background,
    [switch]$Tray
)

$ErrorActionPreference = 'Stop'
$BotTask      = 'ZuvioRollcallBot'
$WatchdogTask = 'ZuvioWatchdog'
$ProjectDir   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($Uninstall) {
    foreach ($name in @($WatchdogTask, $BotTask)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "已移除排程工作 $name。"
        } else {
            Write-Host "排程工作 $name 不存在，無需移除。"
        }
    }
    return
}

# 找 python.exe 旁邊的 pythonw.exe（無主控台視窗）
$python  = (Get-Command python.exe -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "找不到 $pythonw" }

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# ── 1. bot 本體：登入時啟動 ─────────────────────────────
$mode = if ($Tray) { '--autostart --tray' } elseif ($Background) { '--cli' } else { '--autostart' }
if ($UseExe) {
    $exe = Join-Path $ProjectDir 'dist\Zuvio.exe'
    if (-not (Test-Path $exe)) { throw "找不到 $exe，請先執行 Compile.bat 打包。" }
    $botAction = New-ScheduledTaskAction -Execute $exe -Argument $mode -WorkingDirectory (Join-Path $ProjectDir 'dist')
} else {
    $script    = Join-Path $ProjectDir 'Zuvio.py'
    $botAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" $mode" -WorkingDirectory $ProjectDir
}

# 登入時觸發，延遲 30 秒讓網路與 OneDrive 就緒
$botTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$botTrigger.Delay = 'PT30S'

# ExecutionTimeLimit 0 = 不限制執行時間（預設 3 天會被強制結束）
# RestartCount/Interval：程序異常結束時由排程器自動重啟
$botSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $BotTask -Action $botAction -Trigger $botTrigger `
    -Settings $botSettings -Principal $principal -Force | Out-Null

# ── 2. 看門狗：每 5 分鐘檢查一次 ─────────────────────────
$wdScript = Join-Path $ProjectDir 'deploy\watchdog.py'
$wdAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$wdScript`"" -WorkingDirectory $ProjectDir

# 從今天 0 點起每 5 分鐘重複一次，持續 10 年（等同永久）
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)

$wdSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $WatchdogTask -Action $wdAction -Trigger $wdTrigger `
    -Settings $wdSettings -Principal $principal -Force | Out-Null

$modeDesc = if ($Tray) { '系統匣圖示模式' } elseif ($Background) { '完全無視窗背景執行' } else { 'GUI 自動開始並縮小到工作列' }
Write-Host "已註冊排程工作："
Write-Host "  $BotTask：使用者 $env:USERNAME 登入後 30 秒啟動（$modeDesc）"
Write-Host "    $($botAction.Execute) $($botAction.Arguments)"
Write-Host "  $WatchdogTask：每 5 分鐘執行 deploy\watchdog.py"
Write-Host ""
Write-Host "管理 bot    ：.\deploy\bot.ps1 start | stop | restart | status | log"
Write-Host "看門狗試判斷：python deploy\watchdog.py --dry-run"
Write-Host "移除        ：.\deploy\install_autostart.ps1 -Uninstall"
