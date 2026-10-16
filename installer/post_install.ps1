# 安裝完成後執行：註冊 SKCOM COM 元件、開防火牆、註冊開機自動啟動排程。
# 對應 scripts/gcp/vm-bootstrap.ps1 的邏輯，差別是這裡不裝 Python /
# pip install（本安裝包已內附凍結好的 tmf-backend.exe，不需要系統上有 Python）。
param(
    [string]$AppDir = "C:\tmf-trading",
    [int]$Port = 8765,
    # 2026-10-16：開機自動啟動排程要用的 Windows 帳號密碼，留空則退回舊的
    # AtLogOn/Interactive 模式（見下面註冊排程任務那段的說明）。這個值只會傳給
    # Register-ScheduledTask 做一次性登入驗證，不寫進任何檔案或 log。
    [string]$TaskPassword = ""
)

$ErrorActionPreference = "Stop"

# 若被 32 位元 PowerShell 執行（例如 Google Cloud SDK Shell），強制改用 64 位元版重跑本腳本：
# 32 位元程序下 System32 會被 WOW64 重導向到 SysWOW64（VC2010 已裝與否的判斷會失真），
# regsvr32 也會把 COM 註冊寫進 32 位元登錄區，64 位元的 tmf-backend 讀不到，全部白做。
if (-not [Environment]::Is64BitProcess) {
    $ps64 = Join-Path $env:SystemRoot "Sysnative\WindowsPowerShell\v1.0\powershell.exe"
    & $ps64 -NoProfile -ExecutionPolicy Bypass -File $MyInvocation.MyCommand.Path -AppDir $AppDir -Port $Port -TaskPassword $TaskPassword
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
    # 2026-10-12 實盤事故：VM 上的 tmf-backend.exe 在 10/10 20:49 乾淨地停掉，前後沒有任何
    # 錯誤或例外（Center/Quote/Order.log、position_audit.log 全部正常運作到那一秒就斷了），
    # 停掉的時間點量出來是啟動後 2天23時59分40秒——幾乎精準踩在 72 小時整。原因是
    # Register-ScheduledTask 原本沒傳 -Settings，用的是 New-ScheduledTaskSettingsSet 的
    # 內建預設值，而這個預設值的 ExecutionTimeLimit 剛好是 PT72H（3天）——跟 Windows
    # Task Scheduler 新增工作時 GUI 上「如果工作執行時間超過：3天」那個預設勾選是同一件事。
    # 這個服務要 24/7 常駐，不該有任何執行時間上限；順便加上失敗自動重啟（3次、間隔1分鐘），
    # 減少未來真的當掉時沒人注意到的空窗期（已有過整整36小時沒人發現的前例）。
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    # 2026-10-16 實盤事故：上面那個修正解決了「跑太久被砍」，但 AtLogOn 觸發只有
    # 「這個使用者真的登入」才會啟動排程——VM 本身重開機（Windows更新/GCP維護/任何
    # 原因）如果沒人手動登入，這個排程永遠沒有機會啟動。10/15 03:36 乾淨停止後，
    # 空了超過一天沒人發現，根因就是這個洞，不是程式本身壞掉。
    #
    # 查證過：S4U 登入類型不存密碼、沒有 DPAPI/憑證存取權限（Microsoft 官方文件
    # taskschedulerschema-logontype-simpletype 明寫），大概率會重演 SYSTEM 的 602；
    # 只有 -LogonType Password（Windows 原生支援的「不管有沒有人登入都執行」模式，
    # 密碼由 Windows 自己加密存成 LSA secret，不是我們這邊存在任何檔案裡）才會走跟
    # 真正互動登入一樣的 DPAPI 解鎖路徑，有機會摸到憑證，且比「開機自動登入」更安全
    # （不會留一個沒鎖的桌面在那邊讓 RDP 直接撿走）。
    #
    # ⚠️ 這個模式還沒有在「完全沒人登入、真的重開機」的情況下實測驗證過群益登入不會
    # 回 602——SKCOM 是第三方 COM 元件，理論上可能檢查某些只有真正互動 session 才有
    # 的東西（例如 window station）。裝完務必找非交易時段真的重開機測一次（不要馬上
    # 登入），確認服務自己起來、群益登入正常，才能放心依賴這個機制。如果驗證後還是
    # 602，退回下面 else 分支的舊模式，改用開機自動登入（AutoAdminLogon）當備援，
    # 並把這個帳號設成低權限、限制 RDP 來源IP。
    #
    # $TaskPassword 留空則退回舊的 AtLogOn/Interactive（能用，但保留「VM重開機沒人
    # 登入就不會自動恢復」的限制），給不想在安裝精靈輸入密碼的人當選項。
    if ($TaskPassword) {
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $trigger.Delay = "PT1M"  # 開機後延遲1分鐘，等網路/憑證服務就位
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -User $currentUser -Password $TaskPassword -RunLevel Highest -Settings $settings -Force | Out-Null
        $TaskPassword = $null  # 盡早清掉，不留在變數裡
        Log "排程任務 $taskName 已註冊並啟動（使用者：$currentUser，開機自動啟動、不需要登入，無執行時間上限）"
    } else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Log "排程任務 $taskName 已註冊並啟動（使用者：$currentUser，登入時自動啟動，無執行時間上限）——未設定開機自動啟動密碼，VM重開機後需要手動登入才會恢復"
    }
    Start-ScheduledTask -TaskName $taskName

    # 2026-09-03：移除了 Cloudflare quick tunnel 自動註冊（見 git 歷史）。quick tunnel
    # 網址每 3 天左右會被 Cloudflare 邊緣節點強制斷線、且沒有正常運作保證，改用 GCP
    # 靜態外部 IP + 防火牆規則對外連線（見 docs/live_trading_flow.md），對外連線網址
    # 因此改為固定不變，不需要每次開機重新查詢。

    Log "post_install 完成。儀表板：http://127.0.0.1:$Port"
} catch {
    Log "ERROR: $_"
    throw
}
