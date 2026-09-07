# 安裝完成後執行：註冊 SKCOM COM 元件、開防火牆、註冊開機自動啟動排程。
# 對應 scripts/gcp/vm-bootstrap.ps1 的邏輯，差別是這裡不裝 Python /
# pip install（本安裝包已內附凍結好的 tmf-backend.exe，不需要系統上有 Python）。
param(
    [string]$AppDir = "C:\tmf-trading",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

# 若被 32 位元 PowerShell 執行（例如 Google Cloud SDK Shell），強制改用 64 位元版重跑本腳本：
# 32 位元程序下 System32 會被 WOW64 重導向到 SysWOW64（VC2010 已裝與否的判斷會失真），
# regsvr32 也會把 COM 註冊寫進 32 位元登錄區，64 位元的 tmf-backend 讀不到，全部白做。
if (-not [Environment]::Is64BitProcess) {
    $ps64 = Join-Path $env:SystemRoot "Sysnative\WindowsPowerShell\v1.0\powershell.exe"
    & $ps64 -NoProfile -ExecutionPolicy Bypass -File $MyInvocation.MyCommand.Path -AppDir $AppDir -Port $Port
    exit $LASTEXITCODE
}

$Log = Join-Path $AppDir "install.log"

function Log([string]$msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $Log -Value $line
    Write-Host $msg
}

function Find-SkcomDll([string]$root) {
    # 順序必須跟 backend/broker/capital_futures.py 的 _DLL_CANDIDATE_RELATIVE_PATHS
    # 一致（Quote 優先），註冊的和程式載入的才會是同一份。
    $preferred = @(
        (Join-Path $root "CapitalAPI_2.13.58_PythonExample\CapitalAPI_2.13.58_PythonExample\PythonExampleV2\Quote\Quote\SKCOM.dll"),
        (Join-Path $root "CapitalAPI_2.13.58_PythonExample\CapitalAPI_2.13.58_PythonExample\元件\x64\SKCOM.dll")
    )
    foreach ($p in $preferred) {
        if (Test-Path -LiteralPath $p) { return (Get-Item -LiteralPath $p) }
    }
    $candidates = Get-ChildItem -Path $root -Filter "SKCOM.dll" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\x86\\' }
    $ordered = $candidates | Sort-Object {
        $n = $_.FullName
        if ($n -match 'PythonExampleV2\\Quote\\Quote') { 0 }
        elseif ($n -match '\\x64\\') { 1 }
        else { 2 }
    }
    return $ordered | Select-Object -First 1
}

try {
    Log "post_install start, AppDir=$AppDir"

    # 1) 解除封鎖 CapitalAPI 目錄下所有檔案（從安裝包解壓縮常被標記為「網路來源」而擋載入）
    $capitalDir = Join-Path $AppDir "CapitalAPI_2.13.58_PythonExample"
    if (Test-Path $capitalDir) {
        Get-ChildItem -LiteralPath $capitalDir -Recurse -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
        Log "Unblock-File done under $capitalDir"
    } else {
        Log "WARNING: $capitalDir not found"
    }

    # 2) 安裝 VC++ 2010 SP1 Redistributable (x64)
    #
    # 這是 SKCOM.dll 在全新機器上註冊失敗（Class not registered / module not found）的
    # 真正根因：用 PE 匯入表實際解析過，SKCOM.dll → CTSecuritiesATL.dll → mfc100.dll +
    # msvcr100.dll（VC2010），其餘依賴全是 Windows 內建 DLL。開發機因為歷史上裝過
    # VC2010 所以從沒踩到；全新 GCP VM 沒有，regsvr32 就會失敗。
    # 原始 scripts/gcp/vm-bootstrap.ps1 本來就有這一步，改寫成本檔時被誤刪，現補回。
    if (-not (Test-Path (Join-Path $env:SystemRoot "System32\msvcr100.dll"))) {
        # 先盡力用內附的官方 redist 安裝（正規路徑）；但 GCP Windows Server 實測遇過
        # vcredist exit 0 卻什麼都沒裝的幽靈狀態（MSI 無登記、修復/重裝都無效），
        # 所以這裡「不因失敗中斷」——後面註冊步驟有 app-local DLL 備援可完全繞開。
        $vcExe = Join-Path $AppDir "redist\vcredist2010_x64.exe"
        if (Test-Path $vcExe) {
            Log "安裝 VC++ 2010 Redistributable (x64): $vcExe"
            $vcProc = Start-Process -FilePath $vcExe -ArgumentList "/q /norestart" -Wait -PassThru
            Log "vcredist exit code: $($vcProc.ExitCode)"
        } else {
            Log "WARNING: 安裝包內未附 redist（$vcExe 不存在），跳過"
        }
        if (Test-Path (Join-Path $env:SystemRoot "System32\msvcr100.dll")) {
            Log "VC++ 2010 Redistributable 安裝完成"
        } else {
            Log "WARNING: VC++ 2010 系統層級安裝未生效，將改用 app-local DLL 備援"
        }
    } else {
        Log "VC++ 2010 runtime 已存在 (System32\msvcr100.dll)，略過安裝"
    }

    # 3) 註冊 SKCOM.dll（COM 元件）
    #
    # 註冊「後端實際載入的那一份」：backend/broker/capital_futures.py 的
    # _DLL_CANDIDATE_RELATIVE_PATHS 優先 PythonExampleV2\Quote\Quote，這裡必須一致，
    # 否則登錄檔指到 A 份、程式載入 B 份。不複製任何檔案進 System32/SysWOW64
    # （那是錯誤做法，會污染系統目錄且解決不了 mfc100 缺失）。
    $dll = Find-SkcomDll $AppDir
    if (-not $dll) {
        throw "找不到 SKCOM.dll，安裝包可能不完整"
    }
    $dllDir = $dll.DirectoryName

    # app-local 備援：系統層級 VC2010 不在的話，把 mfc100/msvcr100 放到 SKCOM.dll
    # 旁邊——PE 匯入表證實整條依賴鏈（含執行期載入的 SecuCompx64/libsolclient 等）
    # 只需要這兩個檔案，放在同資料夾載入器就找得到，與系統安裝狀態完全脫鉤。
    if (-not (Test-Path (Join-Path $env:SystemRoot "System32\msvcr100.dll"))) {
        $applocal = Join-Path $AppDir "redist\vc2010_applocal"
        if (Test-Path $applocal) {
            Copy-Item (Join-Path $applocal "*.dll") $dllDir -Force
            Log "app-local 備援：已複製 mfc100/msvcr100 到 $dllDir"
        } else {
            Log "WARNING: app-local 備援資料夾不存在（$applocal），註冊可能失敗"
        }
    }

    Log "註冊 SKCOM.dll: $($dll.FullName)"
    $reg = Start-Process -FilePath "regsvr32.exe" -ArgumentList "/s", "`"$($dll.FullName)`"" -WorkingDirectory $dllDir -Wait -PassThru
    Log "regsvr32 exit code: $($reg.ExitCode)"

    # 註冊完必須驗證 CLSID 真的進了登錄檔（SKCenterLib 的 CLSID，出自官方 readme），
    # 不能只看 regsvr32 有沒有跳錯——失敗就讓安裝流程明確失敗，不要靜默帶過。
    $clsidPath = "Registry::HKEY_CLASSES_ROOT\CLSID\{AC30BAB5-194A-4515-A8D3-6260749F8577}\InprocServer32"
    if (-not (Test-Path $clsidPath)) {
        throw "SKCOM.dll 註冊失敗（CLSID 未寫入登錄檔，regsvr32 exit=$($reg.ExitCode)）。請確認 VC++ 2010 已安裝後重試。"
    }
    Log "SKCOM CLSID 驗證通過: $((Get-ItemProperty $clsidPath).'(default)')"

    # 4) 防火牆規則（允許本機瀏覽器 / 區網存取儀表板）
    New-NetFirewallRule -DisplayName "TMF Trading API" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -ErrorAction SilentlyContinue | Out-Null
    Log "防火牆規則已設定（port $Port）"

    # 5) 登入時自動啟動排程任務
    #
    # 重要：這裡故意用「目前登入的使用者」+ Interactive 登入類型，而不是 SYSTEM +
    # AtStartup。實測發現用 SYSTEM 身分執行時，群益 SKCOM 登入固定回傳 602
    # （文件說是「憑證可能過期或未安裝」），即使同一台機器、同一個帳號在互動式
    # session 下登入完全正常。研判群益的雙因子/憑證綁定認的是目前登入使用者的
    # session context，SYSTEM 這個特殊帳號看不到有效憑證。因此排程任務必須綁定
    # 實際安裝這套軟體的使用者，並在該使用者登入時啟動，而非開機時以 SYSTEM 啟動。
    $exePath = Join-Path $AppDir "tmf-backend.exe"
    if (-not (Test-Path $exePath)) {
        throw "找不到 $exePath"
    }
    $taskName = "TMF-Trading-API"
    $currentUser = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Log "排程任務 $taskName 已註冊並啟動（使用者：$currentUser，登入時自動啟動）"

    # 2026-09-03：移除了 Cloudflare quick tunnel 自動註冊（見 git 歷史）。quick tunnel
    # 網址每 3 天左右會被 Cloudflare 邊緣節點強制斷線、且沒有正常運作保證，改用 GCP
    # 靜態外部 IP + 防火牆規則對外連線（見 docs/live_trading_flow.md），對外連線網址
    # 因此改為固定不變，不需要每次開機重新查詢。

    Log "post_install 完成。儀表板：http://127.0.0.1:$Port"
} catch {
    Log "ERROR: $_"
    throw
}
