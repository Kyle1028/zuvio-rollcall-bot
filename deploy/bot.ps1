<#
.SYNOPSIS
  Zuvio 點名助手的日常管理指令（背景模式下沒有視窗，用這支腳本操作）。

.DESCRIPTION
  用法（在專案目錄開 PowerShell）：
    .\deploy\bot.ps1 status    # 是否在跑、心跳時間、最近日誌（預設）
    .\deploy\bot.ps1 start     # 透過排程器啟動
    .\deploy\bot.ps1 stop      # 停止，並標記為使用者主動停止（看門狗不會把它拉起來）
    .\deploy\bot.ps1 restart   # 停止後重新啟動
    .\deploy\bot.ps1 log       # 即時跟看日誌（Ctrl+C 離開）
#>
param(
    [ValidateSet('status', 'start', 'stop', 'restart', 'log')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TaskName   = 'ZuvioRollcallBot'
$Heartbeat  = Join-Path $ProjectDir 'zuvio_heartbeat.json'
$LogFile    = Join-Path $ProjectDir 'zuvio_bot.log'

function Read-Heartbeat {
    if (-not (Test-Path $Heartbeat)) { return $null }
    try { return (Get-Content $Heartbeat -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Test-BotRunning {
    $hb = Read-Heartbeat
    if (-not $hb -or -not $hb.pid) { return $false }
    $p = Get-Process -Id $hb.pid -ErrorAction SilentlyContinue
    return ($null -ne $p) -and ($p.ProcessName -match '^(python|pythonw|Zuvio)$')
}

function Write-HeartbeatState([string]$State) {
    # 讓看門狗知道這是使用者主動停止。Python 的 json 不接受 BOM，所以用不帶 BOM 的 UTF-8 寫
    $epoch = [Math]::Round((Get-Date -Date (Get-Date).ToUniversalTime() -UFormat %s), 0)
    $obj = [ordered]@{
        pid = 0; ts = [double]$epoch
        time = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); state = $State
    }
    $json = $obj | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($Heartbeat, $json, (New-Object Text.UTF8Encoding $false))
}

function Stop-Bot {
    schtasks /End /TN $TaskName 2>&1 | Out-Null
    $hb = Read-Heartbeat
    if ($hb -and $hb.pid -and (Get-Process -Id $hb.pid -ErrorAction SilentlyContinue)) {
        taskkill /PID $hb.pid /T /F 2>&1 | Out-Null
    }
    Write-HeartbeatState 'exited_by_user'
    Write-Host "已停止 bot（看門狗不會自動重啟；要恢復請執行 start）。"
}

function Start-Bot {
    if (Test-BotRunning) { Write-Host "bot 已經在執行中。"; return }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "已透過排程器啟動 bot，約 10～20 秒後開始監控。"
}

function Show-Status {
    $running = Test-BotRunning
    $hb = Read-Heartbeat
    $state = if ($running) { '執行中' } else { '未執行' }
    Write-Host "bot：$state"
    if ($hb) { Write-Host "心跳：$($hb.time)（狀態 $($hb.state)，PID $($hb.pid)）" } else { Write-Host "心跳：無" }
    Get-ScheduledTask -TaskName 'Zuvio*' -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "排程：$($_.TaskName) = $($_.State)" }
    if (Test-Path $LogFile) {
        Write-Host "`n最近日誌："
        Get-Content $LogFile -Tail 8 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
    }
}

switch ($Action) {
    'status'  { Show-Status }
    'start'   { Start-Bot }
    'stop'    { Stop-Bot }
    'restart' { Stop-Bot; Start-Sleep -Seconds 2; Start-Bot }
    'log'     { Get-Content $LogFile -Tail 30 -Wait -Encoding UTF8 }
}
