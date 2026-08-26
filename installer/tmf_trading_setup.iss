; TMF 交易系統 — 給非工程師使用者的桌面安裝程式骨架
;
; 打包內容：PyInstaller 凍結的 tmf-backend.exe（含 Python runtime）、frontend 靜態頁面、
; CapitalAPI_2.13.58_PythonExample（SKCOM.dll 等 COM 元件）。
;
; 2026-08-26：群益帳密、永豐金鑰改成集中存在 Supabase 一張表（見
; backend/secrets_store.py），後端開機時自己去抓，不再需要安裝精靈問帳密。
; 安裝精靈只問「測試/正式環境」，寫出的 .env 帶著一把寫死的 Supabase
; SUPABASE_SECRET_KEY（見 local_secrets.iss，不進版本控管），後端靠這把鑰匙
; 去解鎖其餘密鑰。然後執行 post_install.ps1（註冊 SKCOM COM 元件、開防火牆、
; 註冊開機自動啟動排程）。
;
; 編譯方式：
;   "C:\Users\user\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\tmf_trading_setup.iss
; 前提：
;   1. 已用 PyInstaller 把 run_server.py 打包到 dist\tmf-backend\（見 requirements.txt + run_server.py）
;   2. dist\tmf-backend\ 旁邊已放好 frontend\ 資料夾（不是靠 PyInstaller --add-data，見 app_base_dir 註解）
;   3. installer\local_secrets.iss 存在（含真實 Supabase 密鑰，不進版本控管，見該檔案說明）

#include "local_secrets.iss"

#define AppName "TMF 交易系統"
#define AppVersion "0.1.0"
#define AppExeName "tmf-backend.exe"
#define AppPort "8765"

; 專案根目錄（本 .iss 檔案位於 installer\ 底下，往上一層即為專案根）
#define ProjectRoot AddBackslash(SourcePath) + "..\"
#define DistDir ProjectRoot + "dist\tmf-backend"
#define CapitalApiDir ProjectRoot + "CapitalAPI_2.13.58_PythonExample"

[Setup]
AppId={{8F2B6E2A-6C1B-4A6D-9C3E-TMF-TRADING-0001}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName=C:\tmf-trading
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=TMF-Trading-Setup
Compression=lzma2
SolidCompression=yes
; 尚未申請程式碼簽章憑證：使用者安裝時 Windows SmartScreen 會跳「未知發行者」警告，
; 這是預期行為，需要在使用手冊裡先說明「這是正常的，點『其他資訊』→『仍要執行』」。
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
; Inno Setup 官方沒有內建繁中翻譯檔（需另外下載 ChineseTraditional.isl 才能讓「下一步」
; 這類系統按鈕顯示中文），這裡先用內建英文，我們自訂的頁面文字本身仍是中文。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}frontend\*"; DestDir: "{app}\frontend"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#CapitalApiDir}\*"; DestDir: "{app}\CapitalAPI_2.13.58_PythonExample"; Excludes: "CapitalLog\*,*.log"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "post_install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "run_tunnel.ps1"; DestDir: "{app}"; Flags: ignoreversion
; Cloudflare quick tunnel：讓儀表板可以從外部（手機/別台電腦）連進來，不需要使用者
; 自己申請 Cloudflare 帳號。網址每次開機都會換，見 run_tunnel.ps1 寫出的 tunnel-url.txt。
Source: "{#ProjectRoot}tools\cloudflared.exe"; DestDir: "{app}\tools"; Flags: ignoreversion
; VC++ 2010 SP1 redist（SKCOM.dll 的硬性依賴）直接內附，避免安裝時依賴微軟下載連結
;（舊連結已 404 過一次，內附才是可靠做法）
Source: "redist\vcredist2010_x64.exe"; DestDir: "{app}\redist"; Flags: ignoreversion
; app-local 備援：GCP VM 實測過 vcredist 靜默 exit 0 卻沒真的安裝（MSI 幽靈狀態），
; 這兩個 DLL 會在該情況下被 post_install 複製到 SKCOM.dll 旁邊，繞開系統安裝
Source: "redist\vc2010_applocal\*.dll"; DestDir: "{app}\redist\vc2010_applocal"; Flags: ignoreversion

[Dirs]
Name: "{app}\data"

[Run]
; 必須用 {sys}（64 位元安裝模式下指向真正的 System32）明確指定 64 位元 PowerShell：
; Inno 的 Setup.exe 本體是 32 位元程序，裸寫 "powershell.exe" 會抓到 SysWOW64 的
; 32 位元版 → regsvr32 註冊寫進 32 位元登錄區，64 位元後端讀不到 → Class not registered；
; 且 32 位元下 System32 路徑被重導向，連 VC2010 已裝與否的判斷都會失真。
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\post_install.ps1"" -AppDir ""{app}"" -Port {#AppPort}"; \
  StatusMsg: "設定 SKCOM 元件、防火牆與開機自動啟動..."; \
  Flags: runhidden waituntilterminated 64bit

; 解除安裝時反向清理 post_install.ps1 做過的系統層級變更；
; 光刪 {app} 資料夾不會動到這些（排程任務/防火牆規則/COM 登錄檔都不在資料夾裡）。
; 同樣全部用 {sys} 明確指定 64 位元版本。
[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""TMF-Trading-API"""; \
  Flags: runhidden 64bit; RunOnceId: "TmfStopTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""TMF-Trading-API"" /F"; \
  Flags: runhidden 64bit; RunOnceId: "TmfDeleteTask"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""TMF Trading API"""; \
  Flags: runhidden 64bit; RunOnceId: "TmfDeleteFirewallRule"
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""TMF-Trading-Tunnel"""; \
  Flags: runhidden 64bit; RunOnceId: "TmfStopTunnelTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""TMF-Trading-Tunnel"" /F"; \
  Flags: runhidden 64bit; RunOnceId: "TmfDeleteTunnelTask"
Filename: "{sys}\regsvr32.exe"; \
  Parameters: "/u /s ""{app}\CapitalAPI_2.13.58_PythonExample\CapitalAPI_2.13.58_PythonExample\PythonExampleV2\Quote\Quote\SKCOM.dll"""; \
  Flags: runhidden 64bit; RunOnceId: "TmfUnregisterSkcom"

[Icons]
Name: "{group}\開啟 {#AppName} 儀表板"; Filename: "http://127.0.0.1:{#AppPort}"
Name: "{commondesktop}\{#AppName}"; Filename: "http://127.0.0.1:{#AppPort}"
; quick tunnel 網址每次開機都會換，這裡給一個捷徑直接開啟 run_tunnel.ps1 寫出的網址檔，
; 使用者不用學怎麼看排程任務或 log。
Name: "{group}\查看目前對外連線網址"; Filename: "{win}\notepad.exe"; Parameters: """{app}\tunnel-url.txt"""
Name: "{commondesktop}\{#AppName} 對外連線網址"; Filename: "{win}\notepad.exe"; Parameters: """{app}\tunnel-url.txt"""

[Code]
var
  EnvPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  EnvPage := CreateInputOptionPage(wpSelectDir,
    '交易環境', '先用測試環境驗證，沒問題再切正式環境',
    '群益帳密、永豐金鑰現在集中存在 Supabase，安裝時不用再手動輸入，' + #13#10 +
    '後端開機會自動去抓。' + #13#10 +
    '⚠️ 但帳號本人必須已經在這台電腦完成「CA 憑證申請」與「SKCOMVerifyDJ ' + #13#10 +
    '連線測試」（見隨附的《CA 憑證申請圖文指南》），否則裝好也連不上。' + #13#10 +
    '強烈建議先選「測試環境」，確認報價/下單流程都正常後，' + #13#10 +
    '再回來修改 .env 把 CAPITAL_ENVIRONMENT 改成 0（正式環境），' + #13#10 +
    '避免安裝當下不小心對正式帳號下單。',
    False, False);
  EnvPage.Add('測試環境（建議先選這個）');
  EnvPage.Add('正式環境（我已完成連線測試，確定要用正式帳號）');
  EnvPage.SelectedValueIndex := 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  EnvValue: string;
  EnvContent: string;
  EnvPath: string;
begin
  if CurStep = ssPostInstall then
  begin
    if EnvPage.SelectedValueIndex = 1 then
      EnvValue := '0'
    else
      EnvValue := '2';

    { .env 全程只能用 ASCII：SaveStringToFile 是用系統 ANSI codepage 寫檔，
      混入中文字元會跟 python-dotenv 預設的 UTF-8 解碼衝突，直接讓 exe 開機就崩潰。 }
    EnvContent :=
      '# generated by installer wizard' + #13#10 +
      'CAPITAL_ENVIRONMENT=' + EnvValue + #13#10 +
      'AUTO_CONNECT=1' + #13#10 +
      '# product code is chosen on the dashboard web page, not here' + #13#10 +
      '' + #13#10 +
      '# trading starts disabled; set to 0 manually after verifying connection/quotes' + #13#10 +
      'DISABLE_TRADING=1' + #13#10 +
      '' + #13#10 +
      'STRATEGY_INITIAL_CAPITAL=100000' + #13#10 +
      'STRATEGY_MAX_LOSS_NTD=10000' + #13#10 +
      'STRATEGY_MAX_LOSS_PCT=0.10' + #13#10 +
      'STRATEGY_QTY=1' + #13#10 +
      '' + #13#10 +
      '# 2026-08-24：登入用 Supabase 驗證（見 backend/supabase_auth.py），這裡是' + #13#10 +
      '# 專案網址，不是密鑰，公開寫死沒問題；漏了這行後端會對每個登入 token 回' + #13#10 +
      '# 401（缺 SUPABASE_URL 無法驗證）。' + #13#10 +
      'SUPABASE_URL=https://tgpsxzuyoqqjmwsqjiej.supabase.co' + #13#10 +
      '' + #13#10 +
      '# 2026-08-26：群益帳密/永豐金鑰集中存在 Supabase app_secrets 表' + #13#10 +
      '# （見 backend/secrets_store.py），這把是真正的密鑰，繞過 RLS，' + #13#10 +
      '# 不能外流。開機時後端會用它去抓其餘密鑰蓋掉下面這些空值。' + #13#10 +
      'SUPABASE_SECRET_KEY={#SupabaseSecretKey}' + #13#10;

    EnvPath := ExpandConstant('{app}\.env');
    SaveStringToFile(EnvPath, EnvContent, False);
  end;
end;
