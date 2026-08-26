# 開機排程任務用：啟動 Cloudflare quick tunnel，並把目前網址寫到 tunnel-url.txt
# 方便使用者在儀表板捷徑之外找到「目前對外網址」（quick tunnel 每次重啟都會換網址，
# 沒有固定網域，所以只能用這種方式讓使用者每次開機後自己去看最新的網址）。
param(
    [string]$AppDir = "C:\tmf-trading",
    [int]$Port = 8765
)

$cloudflared = Join-Path $AppDir "tools\cloudflared.exe"
$logPath = Join-Path $AppDir "tunnel.log"
$urlPath = Join-Path $AppDir "tunnel-url.txt"

if (Test-Path $urlPath) { Remove-Item $urlPath -Force -ErrorAction SilentlyContinue }
"（尚未取得對外網址，請稍候幾秒後重新開啟本檔案）" | Set-Content -Path $urlPath -Encoding ascii

& $cloudflared tunnel --url "http://localhost:$Port" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    Add-Content -Path $logPath -Value $line
    if ($line -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
        $Matches[0] | Set-Content -Path $urlPath -Encoding ascii
    }
}
