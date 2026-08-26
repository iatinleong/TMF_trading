# 在 GCP 建立 Windows Server VM（群益 SKCOM 需 Windows + COM）
# 用法：先執行 gcloud auth login，再：
#   .\scripts\gcp\create-vm.ps1
# 可選環境變數：$env:GCP_PROJECT, $env:GCP_ZONE, $env:VM_NAME

param(
    [string]$Project = $(if ($env:GCP_PROJECT) { $env:GCP_PROJECT } else { "massive-rock-501112-g0" }),
    [string]$Zone = $(if ($env:GCP_ZONE) { $env:GCP_ZONE } else { "asia-east1-a" }),
    [string]$VmName = $(if ($env:VM_NAME) { $env:VM_NAME } else { "tmf-trading-vm" }),
    [string]$MachineType = "e2-standard-2",
    [int]$DiskGb = 80
)

$ErrorActionPreference = "Stop"

Write-Host "Project: $Project"
Write-Host "Zone:    $Zone"
Write-Host "VM:      $VmName ($MachineType, ${DiskGb}GB)"

gcloud config set project $Project

# 確認 Compute API 已啟用
gcloud services enable compute.googleapis.com --project $Project

# 建立 VM（Windows Server 2022）
$createArgs = @(
    "compute", "instances", "create", $VmName,
    "--zone=$Zone",
    "--machine-type=$MachineType",
    "--image-family=windows-2022",
    "--image-project=windows-cloud",
    "--boot-disk-size=${DiskGb}GB",
    "--boot-disk-type=pd-balanced",
    "--tags=tmf-trading",
    "--metadata=enable-windows-auth=Y"
)
& gcloud @createArgs

Write-Host ""
Write-Host "VM 建立完成。取得 RDP 密碼："
Write-Host "  gcloud compute reset-windows-password $VmName --zone=$Zone --user=admin"
Write-Host ""
Write-Host "建議接著執行防火牆（將 YOUR_IP 換成你的公網 IP）："
Write-Host "  gcloud compute firewall-rules create allow-tmf-rdp --allow=tcp:3389 --target-tags=tmf-trading --source-ranges=YOUR_IP/32"
Write-Host "  gcloud compute firewall-rules create allow-tmf-api  --allow=tcp:8765 --target-tags=tmf-trading --source-ranges=YOUR_IP/32"
Write-Host ""
Write-Host "上傳專案並初始化："
Write-Host "  .\scripts\gcp\deploy.ps1 -VmName $VmName -Zone $Zone"