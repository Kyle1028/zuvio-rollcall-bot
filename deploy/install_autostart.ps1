<#
.SYNOPSIS
  讓 Zuvio 點名助手在 Windows 登入時自動啟動（工作排程器）。

.DESCRIPTION
  註冊一個「登入時觸發」的排程工作，以 pythonw.exe（無主控台視窗）執行專案目錄的
  Zuvio.py --autostart：GUI 會自動按「開始自動點名」並縮小到工作列。

  用法（在專案目錄開 PowerShell）：
    .\deploy\install_autostart.ps1              # 安裝／更新
    .\deploy\install_autostart.ps1 -Uninstall   # 移除
    .\deploy\install_autostart.ps1 -UseExe      # 改用 dist\Zuvio.exe（需把 settings.json 放在 dist\）

  若出現「因為這個系統上已停用指令碼執行」，先執行：
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>
param(
    [switch]$Uninstall,
    [switch]$UseExe
)

$ErrorActionPreference = 'Stop'
$TaskName   = 'ZuvioRollcallBot'
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除排程工作 $TaskName。"
    } else {
        Write-Host "排程工作 $TaskName 不存在，無需移除。"
    }
    return
}

if ($UseExe) {
    $exe = Join-Path $ProjectDir 'dist\Zuvio.exe'
    if (-not (Test-Path $exe)) { throw "找不到 $exe，請先執行 Compile.bat 打包。" }
    $workDir = Join-Path $ProjectDir 'dist'
    $action  = New-ScheduledTaskAction -Execute $exe -Argument '--autostart' -WorkingDirectory $workDir
} else {
    # 找 python.exe 旁邊的 pythonw.exe（無主控台視窗）
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path $pythonw)) { throw "找不到 $pythonw" }
    $script = Join-Path $ProjectDir 'Zuvio.py'
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" --autostart" -WorkingDirectory $ProjectDir
}

# 登入時觸發，延遲 30 秒讓網路與 OneDrive 就緒
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT30S'

# 關鍵設定：
#   ExecutionTimeLimit 0 = 不限制執行時間（預設 3 天就會被強制結束）
#   不因電池供電而不啟動／中止；錯過登入觸發時盡快補跑
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "已註冊排程工作 $TaskName："
Write-Host "  觸發：使用者 $env:USERNAME 登入後 30 秒"
Write-Host "  執行：$($action.Execute) $($action.Arguments)"
Write-Host "  目錄：$($action.WorkingDirectory)"
Write-Host ""
Write-Host "立即測試：Start-ScheduledTask -TaskName $TaskName"
Write-Host "移除    ：.\deploy\install_autostart.ps1 -Uninstall"
