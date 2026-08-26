"""backend/secrets_store.py — 集中式密鑰管理：開機時從 Supabase 抓真正的交易
密鑰（群益帳密、永豐 API 金鑰），取代分散在每台機器 `.env` 裡手動維護。

2026-08-26：多台機器（本機開發電腦、VM）各自維護一份 `.env`，密鑰要改就要
每台手動改一次，容易漏改/不同步。這裡改成：開機時嘗試從 Supabase 一張表讀
一次，讀到就覆蓋 os.environ；讀不到（沒設定服務端金鑰、表沒建、連線失敗、
沒資料）就安靜退回本機 `.env` 既有值，不影響開機流程——這只是「盡量從遠端
同步」，不是關鍵路徑，失敗不該讓後端連不上。

**特意不跟 Supabase 登入（`backend/supabase_auth.py`）綁在一起**：後端要在
程式啟動當下就連上群益/(未來)永豐，不等任何人打開瀏覽器登入——這是為了讓
STP/MIT 停損停利、策略持續運作這些「不看人在不在」的保護機制成立。如果密鑰
要等使用者登入儀表板才「代入」，沒人開瀏覽器的時候後端就連不上，等於自動化
保護整個失效。這裡用的是 Supabase 的 **service_role 金鑰**（讀 `SUPABASE_
SERVICE_ROLE_KEY`，繞過 Row Level Security，只有後端知道），跟「誰登入了
儀表板」完全無關，是機器對機器的存取，不是使用者身分驗證。

前提：Supabase 專案要有一張 `app_secrets` 表（見 docs/ 或跟這個模組一起交付
的 SQL），且該表**不能**給 anon/authenticated 角色任何讀取權限（RLS 開啟、
不建公開讀取 policy），只靠 service_role 金鑰繞過 RLS 讀取——這把金鑰本身
一樣要放在本機 `.env`，等於把「群益密碼 + 永豐兩把金鑰」集中換成一把「開鎖
用」的金鑰，不是變成零密鑰。
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

# 這幾個是目前會集中管理的密鑰，對應 app_secrets 表的欄位名稱。
SECRET_ENV_KEYS = (
    "CAPITAL_USER_ID",
    "CAPITAL_PASSWORD",
    "SJ_API_KEY",
    "SJ_SEC_KEY",
    "SJ_PRODUCTION",
)


def load_remote_secrets(*, timeout: float = 10.0) -> int:
    """
    嘗試從 Supabase app_secrets 表讀一次密鑰並覆蓋 os.environ。

    只有設定了 SUPABASE_URL + SUPABASE_SECRET_KEY 才會真的連線；
    連線失敗/表不存在/沒資料都安靜跳過。回傳實際套用的密鑰數量（0 代表沒有
    套用任何東西，不一定是失敗，也可能是根本沒設定服務端金鑰）。
    """
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    service_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not service_key:
        return 0

    try:
        resp = requests.get(
            f"{supabase_url}/rest/v1/app_secrets",
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
            },
            params={"select": ",".join(SECRET_ENV_KEYS), "limit": "1"},
            timeout=timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001 - 遠端密鑰只是錦上添花，失敗要安靜退回本機 .env
        logger.warning("讀取 Supabase 密鑰失敗，退回本機 .env: %s", exc)
        return 0

    if not rows:
        logger.info("Supabase app_secrets 目前沒有資料，使用本機 .env")
        return 0

    row = rows[0]
    applied = 0
    for key in SECRET_ENV_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            os.environ[key] = str(value)
            applied += 1

    if applied:
        logger.info("已從 Supabase app_secrets 套用 %d 個密鑰", applied)
    return applied
