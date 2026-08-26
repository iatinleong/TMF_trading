# Upload project to GCP Windows VM
# Usage: .\scripts\gcp\deploy.ps1 -VmName tmf-trading-vm -Zone asia-east1-a

param(
    [string]$VmName = $(if ($env:VM_NAME) { $env:VM_NAME } else { "tmf-trading-vm" }),
    [string]$Zone = $(if ($env:GCP_ZONE) { $env:GCP_ZONE } else { "asia-east1-a" }),
    [string]$RemoteDir = "C:\tmf-trading"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Archive = Join-Path $env:TEMP "tmf-trading-deploy.zip"
$Packer = Join-Path $PSScriptRoot "pack_deploy_zip.py"

Write-Host "Packing project (exclude .env, cache, large raw data)..."

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { $pyCmd = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pyCmd) { throw "python not found in PATH" }

& $pyCmd.Source $Packer $Root $Archive
if ($LASTEXITCODE -ne 0) {
    throw "pack failed, python exit code $LASTEXITCODE"
}
if (-not (Test-Path $Archive)) {
    throw "archive missing: $Archive"
}

$sizeMb = [math]::Round((Get-Item $Archive).Length / 1MB, 1)
Write-Host ("Pack done: {0} ({1} MB)" -f $Archive, $sizeMb)

# Windows VM often has no SSH; SCP times out. Prefer GCS (same path as CI/CD).
$Bucket = if ($env:GCP_BUCKET) { $env:GCP_BUCKET } else { "massive-rock-501112-g0-tmf-deploy" }
$UseGcs = $true
if ($env:DEPLOY_VIA_SCP -eq "1") { $UseGcs = $false }

if ($UseGcs) {
    Write-Host ("Uploading to gs://{0}/ ..." -f $Bucket)
    $sha = (Get-FileHash $Archive -Algorithm SHA256).Hash.Substring(0, 12)
    gcloud storage cp $Archive "gs://$Bucket/releases/latest.zip"
    if ($LASTEXITCODE -ne 0) { throw "gcloud storage cp latest.zip failed" }
    gcloud storage cp $Archive "gs://$Bucket/tmf-trading-deploy.zip"
    if ($LASTEXITCODE -ne 0) { throw "gcloud storage cp tmf-trading-deploy.zip failed" }
    $manifestPath = Join-Path $env:TEMP "deploy-manifest.json"
    $manifest = "{`"sha`":`"$sha`",`"deployed_at`":`"$((Get-Date).ToUniversalTime().ToString('o'))`",`"source`":`"deploy.ps1`"}"
    Set-Content -Path $manifestPath -Value $manifest -Encoding ascii
    gcloud storage cp $manifestPath "gs://$Bucket/deploy-manifest.json"
    if ($LASTEXITCODE -ne 0) { throw "gcloud storage cp manifest failed" }

    Write-Host ""
    Write-Host "GCS upload complete (sha=$sha)."
    Write-Host "RDP into the VM, then Admin PowerShell:"
    Write-Host ""
    Write-Host "  # Option A: auto pull (keeps .env)"
    Write-Host "  & C:\tmf-trading\scripts\gcp\vm-pull-deploy.ps1"
    Write-Host ""
    Write-Host "  # Option B: manual download + expand"
    Write-Host "  gcloud storage cp gs://$Bucket/tmf-trading-deploy.zip C:\Users\admin\tmf-trading-deploy.zip"
    Write-Host "  Expand-Archive -Path C:\Users\admin\tmf-trading-deploy.zip -DestinationPath $RemoteDir -Force"
    Write-Host "  # first time only:"
    Write-Host "  & '$RemoteDir\scripts\gcp\vm-bootstrap.ps1' -AppDir '$RemoteDir'"
    Write-Host ""
    Write-Host "  # ensure .env has LIVE_PRODUCT_CODE=TM2608"
    Write-Host "  notepad $RemoteDir\.env"
    Write-Host "  Restart-ScheduledTask -TaskName TMF-Trading-API"
    Write-Host ""
} else {
    Write-Host ("Uploading via SCP to VM {0} (zone={1}) ..." -f $VmName, $Zone)
    gcloud compute scp $Archive "${VmName}:C:/Users/admin/tmf-trading-deploy.zip" --zone=$Zone
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud compute scp failed, exit code $LASTEXITCODE"
    }
    Write-Host "SCP complete. Expand on VM then run vm-bootstrap.ps1"
}
