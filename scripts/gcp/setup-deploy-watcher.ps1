# 在 VM 注册每 3 分钟检查 GCS 是否有新版本的排程任务
param(
    [string]$AppDir = "C:\tmf-trading"
)

$ErrorActionPreference = "Stop"
$pullScript = Join-Path $AppDir "scripts\gcp\vm-pull-deploy.ps1"
if (-not (Test-Path $pullScript)) {
    throw "找不到 $pullScript，请先完成首次部署。"
}

$taskName = "TMF-Deploy-Watcher"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$pullScript`" -AppDir `"$AppDir`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 3) -RepetitionDuration ([TimeSpan]::MaxValue)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "已注册 $taskName（每 3 分钟检查 GitHub 推送的新版本）"