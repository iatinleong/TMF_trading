$AppDir = "C:\tmf-trading"
$ZipPath = "C:\tmf-trading-deploy.zip"
$Bucket = "massive-rock-501112-g0-tmf-deploy"
$Object = "tmf-trading-deploy.zip"
$Log = "C:\tmf-startup.log"

function Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $Log -Value $line
}

try {
    Log "startup begin"
    $token = Invoke-RestMethod -Headers @{"Metadata-Flavor" = "Google"} `
        -Uri "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    $headers = @{ Authorization = "Bearer $($token.access_token)" }
    $url = "https://storage.googleapis.com/$Bucket/$Object"
    Invoke-WebRequest -Uri $url -Headers $headers -OutFile $ZipPath
    Log "downloaded zip"

    if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
    Expand-Archive -Path $ZipPath -DestinationPath $AppDir -Force
    Log "expanded to $AppDir"

    $bootstrap = Join-Path $AppDir "scripts\gcp\vm-bootstrap.ps1"
    if (Test-Path $bootstrap) {
        & $bootstrap -AppDir $AppDir
        Log "bootstrap done"
    } else {
        Log "bootstrap script missing"
    }

    $watcher = Join-Path $AppDir "scripts\gcp\setup-deploy-watcher.ps1"
    if (Test-Path $watcher) {
        & $watcher -AppDir $AppDir
        Log "deploy watcher registered"
    }
} catch {
    Log "ERROR: $_"
}