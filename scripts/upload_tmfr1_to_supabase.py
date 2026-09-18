"""一次性（或資料更新後）上傳 TMFR1 回測逐筆原始資料到 Supabase Storage。

背景：data/raw_tick/TMFR1（252MB/243 個 parquet 檔）每次都要手動搬到新 VM 很麻煩。
改成上傳一次到 Supabase Storage，之後 backend/load_taifex_tick.py::
ensure_tmfr1_data_available() 會在啟動/回測前自動偵測缺檔並補回，不用再手動搬。

沿用專案既有的 SUPABASE_URL + SUPABASE_SECRET_KEY（見 backend/secrets_store.py），
不用另外管理一組雲端憑證；也不需要 gcloud CLI，純 requests 呼叫 Supabase Storage
REST API，繞開本機 gcloud SDK 憑證庫可能出現的 SSL 驗證問題。

使用方式：
    python -m scripts.upload_tmfr1_to_supabase
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

BUCKET = os.environ.get("TMF_BACKTEST_DATA_BUCKET", "tmf-backtest-data")
PREFIX = "TMFR1"
LOCAL_DIR = PROJECT_ROOT / "data" / "raw_tick" / "TMFR1"


def _config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        raise SystemExit("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，請確認 .env")
    return url, key


def _ensure_bucket(supabase_url: str, headers: dict) -> None:
    resp = requests.get(f"{supabase_url}/storage/v1/bucket/{BUCKET}", headers=headers, timeout=10)
    if resp.status_code == 200:
        return
    resp = requests.post(
        f"{supabase_url}/storage/v1/bucket",
        headers=headers,
        json={"id": BUCKET, "name": BUCKET, "public": False},
        timeout=10,
    )
    if resp.status_code not in (200, 201) and "duplicate" not in resp.text.lower():
        raise SystemExit(f"建立 bucket 失敗: {resp.status_code} {resp.text}")
    print(f"已建立 bucket: {BUCKET}")


def _list_remote_files(supabase_url: str, headers: dict) -> set[str]:
    resp = requests.post(
        f"{supabase_url}/storage/v1/object/list/{BUCKET}",
        headers=headers,
        json={"prefix": f"{PREFIX}/", "limit": 1000},
        timeout=10,
    )
    resp.raise_for_status()
    return {e["name"] for e in resp.json() or [] if str(e.get("name", "")).endswith(".parquet")}


def main() -> None:
    if not LOCAL_DIR.is_dir():
        raise SystemExit(f"找不到本機資料夾: {LOCAL_DIR}")

    local_files = sorted(p.name for p in LOCAL_DIR.glob("TMFR1_*.parquet"))
    if not local_files:
        raise SystemExit(f"{LOCAL_DIR} 底下沒有任何 TMFR1_*.parquet 檔案")

    supabase_url, service_key = _config()
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}

    _ensure_bucket(supabase_url, headers)
    remote_files = _list_remote_files(supabase_url, headers)

    to_upload = [name for name in local_files if name not in remote_files]
    print(f"本機 {len(local_files)} 個檔案，遠端已有 {len(remote_files)} 個，需上傳 {len(to_upload)} 個。")

    upload_headers = {**headers, "Content-Type": "application/octet-stream", "x-upsert": "true"}
    ok = 0
    for i, name in enumerate(to_upload, start=1):
        data = (LOCAL_DIR / name).read_bytes()
        url = f"{supabase_url}/storage/v1/object/{BUCKET}/{PREFIX}/{name}"
        try:
            resp = requests.post(url, headers=upload_headers, data=data, timeout=60)
            resp.raise_for_status()
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(to_upload)}] 上傳 {name} 失敗: {exc}", file=sys.stderr)
            continue
        if i % 20 == 0 or i == len(to_upload):
            print(f"[{i}/{len(to_upload)}] 已上傳 {ok} 個")

    print(f"完成：{ok}/{len(to_upload)} 個檔案上傳成功。VM 端下次啟動/回測時會自動偵測並補齊。")


if __name__ == "__main__":
    main()
