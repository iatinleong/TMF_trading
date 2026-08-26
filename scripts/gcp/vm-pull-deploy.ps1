# VM 端：从 GCS 拉取最新版并重启 API（保留 .env）
param(
    [string]$AppDir = "C:\tmf-trading",
    [string]$Bucket = "massive-rock-501112-g0-tmf-deploy",
    [string]$ManifestObject = "deploy-manifest.json",
    [string]$ZipObject = "releases/latest.zip",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$Log = "C:\tmf-deploy.log"
$ShaFile = Join-Path $AppDir ".deploy-sha"
$PyExe = "C:\Python312\python.exe"
$TaskName = "TMF-Trading-API"

function Log([string]$msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $Log -Value $line
}

function Get-GcpToken {
    $r = Invoke-RestMethod -Headers @{"Metadata-Flavor" = "Google"} `
        -Uri "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    return $r.access_token
}

function Get-GcsJson([string]$token, [string]$object) {
    $url = "https://storage.googleapis.com/$Bucket/$object"
    $headers = @{ Authorization = "Bearer $token" }
    return Invoke-RestMethod -Uri $url -Headers $headers
}

try {
    $token = Get-GcpToken
    $manifest = Get-GcsJson $token $ManifestObject
    $remoteSha = [string]$manifest.sha
    if (-not $remoteSha) { throw "manifest 缺少 sha" }

    $localSha = ""
    if (Test-Path $ShaFile) { $localSha = (Get-Content $ShaFile -Raw).Trim() }
    if ($localSha -eq $remoteSha) { return }

    Log "deploy start: $localSha -> $remoteSha"
    $zipPath = "$env:TEMP\tmf-latest.zip"
    $stage = "$env:TEMP\tmf-stage-$remoteSha"
    $url = "https://storage.googleapis.com/$Bucket/$ZipObject"
    Invoke-WebRequest -Uri $url -Headers @{ Authorization = "Bearer $token" } -OutFile $zipPath

    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive -Path $zipPath -DestinationPath $stage -Force

    if (-not (Test-Path $AppDir)) { New-Item -ItemType Directory -Path $AppDir | Out-Null }
    robocopy $stage $AppDir /E /XF .env /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed: $LASTEXITCODE" }

    if (Test-Path $PyExe) {
        & $PyExe -m pip install -r "$AppDir\requirements.txt" -q
    }

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    $action = New-ScheduledTaskAction -Execute $PyExe -Argument "-m uvicorn backend.api:app --host 0.0.0.0 --port $Port" -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    Set-Content -Path $ShaFile -Value $remoteSha -NoNewline
    Log "deploy done: $remoteSha"
} catch {
    Log "ERROR: $_"
}