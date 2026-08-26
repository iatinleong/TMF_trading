# 为本专案建立 GitHub Actions 用的 GCP Service Account
# 用法（本机，已 gcloud auth login）：
#   .\scripts\gcp\setup-github-cicd.ps1
# 然后把输出的 JSON 存成 GitHub Secret：GCP_SA_KEY

param(
    [string]$Project = "massive-rock-501112-g0",
    [string]$Bucket = "massive-rock-501112-g0-tmf-deploy",
    [string]$SaName = "github-deploy"
)

$ErrorActionPreference = "Stop"
$SaEmail = "$SaName@$Project.iam.gserviceaccount.com"
$KeyFile = "$env:TEMP\gcp-github-deploy-key.json"

gcloud config set project $Project
gcloud iam service-accounts describe $SaEmail 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud iam service-accounts create $SaName --display-name="GitHub Actions deploy"
}

gcloud projects add-iam-policy-binding $Project `
    --member="serviceAccount:$SaEmail" `
    --role="roles/storage.objectAdmin" `
    --condition=None | Out-Null

gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
    --member="serviceAccount:$SaEmail" `
    --role="roles/storage.objectAdmin" | Out-Null

if (Test-Path $KeyFile) { Remove-Item $KeyFile -Force }
gcloud iam service-accounts keys create $KeyFile --iam-account=$SaEmail

Write-Host ""
Write-Host "=== GitHub Repository Secrets ==="
Write-Host "GCP_SA_KEY = 整份 JSON 内容（$KeyFile）"
Write-Host ""
Write-Host "完成后请删除本机密钥文件，Secret 只保存在 GitHub。"
Write-Host "VM 端一次性执行："
Write-Host "  C:\tmf-trading\scripts\gcp\setup-deploy-watcher.ps1"