# Run on GCP Windows VM as Administrator.
# Installs Python, registers SKCOM, starts uvicorn scheduled task.

param(
    [string]$AppDir = "C:\tmf-trading",
    [string]$PythonVersion = "3.12.8",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

function Require-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script as Administrator."
    }
}

function Find-SkcomDll([string]$root) {
    # Prefer ASCII paths first (zip encoding often corrupts Chinese folder "元件")
    $preferred = @(
        (Join-Path $root "CapitalAPI_2.13.58_PythonExample\CapitalAPI_2.13.58_PythonExample\PythonExampleV2\Quote\Quote\SKCOM.dll"),
        (Join-Path $root "CapitalAPI_2.13.58_PythonExample\PythonExampleV2\Quote\Quote\SKCOM.dll")
    )
    foreach ($p in $preferred) {
        if (Test-Path -LiteralPath $p) { return (Get-Item -LiteralPath $p) }
    }

    $candidates = Get-ChildItem -Path $root -Filter "SKCOM.dll" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\x86\\' }

    # Prefer Quote, then any x64-ish path, then first hit
    $ordered = $candidates | Sort-Object {
        $n = $_.FullName
        if ($n -match 'PythonExampleV2\\Quote\\Quote') { 0 }
        elseif ($n -match '\\x64\\') { 1 }
        else { 2 }
    }
    return $ordered | Select-Object -First 1
}

Require-Admin
Set-Location $AppDir

$pyExe = "C:\Python312\python.exe"
if (-not (Test-Path $pyExe)) {
    $installer = "$env:TEMP\python-$PythonVersion-amd64.exe"
    Write-Host "Downloading Python $PythonVersion ..."
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $installer
    Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 TargetDir=C:\Python312 Include_pip=1" -Wait
}

& $pyExe -m pip install --upgrade pip
& $pyExe -m pip install -r "$AppDir\requirements.txt"

if (-not (Test-Path "C:\Windows\System32\msvcr100.dll")) {
    $vcUrls = @(
        "https://download.microsoft.com/download/A/8/0/A80747C3-41BD-45DF-B505-E9710D8E9878/vcredist_x64.exe",
        "https://download.microsoft.com/download/1/6/5/165255E7-1014-0DAD-94F4-4CE47F864DE1/vcredist_x64.exe"
    )
    $vcExe = "$env:TEMP\vcredist_x64.exe"
    $ok = $false
    foreach ($vcUrl in $vcUrls) {
        try {
            Write-Host "Installing VC++ 2010 Redistributable (x64) from $vcUrl ..."
            Invoke-WebRequest -Uri $vcUrl -OutFile $vcExe -UseBasicParsing
            Start-Process -FilePath $vcExe -ArgumentList "/quiet /norestart" -Wait
            $ok = $true
            break
        } catch {
            Write-Warning "VC++ download failed: $vcUrl"
        }
    }
    if (-not $ok) {
        Write-Warning "VC++ 2010 install skipped. If SKCOM fails, install manually."
    }
}

$dll = Find-SkcomDll $AppDir
if (-not $dll) {
    throw "SKCOM.dll not found under $AppDir. Re-upload CapitalAPI package or set CAPITAL_DLL_PATH in .env."
}
$dllDir = $dll.DirectoryName
Write-Host "Using SKCOM.dll: $($dll.FullName)"

Get-ChildItem -LiteralPath $dllDir -Recurse -ErrorAction SilentlyContinue | Unblock-File
$installBat = Join-Path $dllDir "install.bat"
if (Test-Path -LiteralPath $installBat) {
    Write-Host "Running install.bat ..."
    & $installBat
} else {
    Write-Host "No install.bat; registering with regsvr32 ..."
    $reg = Start-Process -FilePath "regsvr32.exe" -ArgumentList "/s", "`"$($dll.FullName)`"" -Wait -PassThru
    if ($reg.ExitCode -ne 0) {
        Write-Warning "regsvr32 exit code $($reg.ExitCode). Try run install.bat manually from CapitalAPI x64 folder."
    }
}

if (-not (Test-Path "$AppDir\.env")) {
    Write-Warning ".env missing. Copy .env.example and set CAPITAL_USER_ID / CAPITAL_PASSWORD."
}

New-NetFirewallRule -DisplayName "TMF Trading API" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction SilentlyContinue | Out-Null

$taskName = "TMF-Trading-API"
$action = New-ScheduledTaskAction -Execute $pyExe -Argument "-m uvicorn backend.api:app --host 0.0.0.0 --port $Port" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host ""
Write-Host "Done. API: http://<VM-IP>:$Port"
Write-Host "Test: Invoke-RestMethod http://127.0.0.1:$Port/api/connection"
