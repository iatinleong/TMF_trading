"""backend/broker_credentials_store.py — 每個登入帳號自己的群益期貨帳密
（Supabase user_broker_credentials 表）。這張表存真實交易密碼，比
strategy_config_store.py 敏感得多；只有 backend/live_service.py 的 worker
啟動流程用 SUPABASE_SECRET_KEY 讀取，絕對不能透過任何一般 API 端點外流（見
docs/superpowers/plans/2026-08-26-per-account-worker-process.md）。
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

TABLE = "user_broker_credentials"


def _base_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip().rstrip("/")


def _configured() -> bool:
    return bool(_base_url() and os.getenv("SUPABASE_SECRET_KEY", "").strip())


def _headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def get_broker_credentials(user_id: str, *, timeout: float = 10.0) -> dict | None:
    """回傳這個帳號自己的群益帳密，沒設定過就回傳 None。安靜失敗（連線問題
    等）也回傳 None，讓呼叫端（worker 啟動流程）自己決定要不要中止——這裡
    不猜呼叫端想怎麼處理。"""
    if not _configured():
        return None
    try:
        resp = requests.get(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers(),
            params={"user_id": f"eq.{user_id}", "select": "capital_user_id,capital_password"},
            timeout=timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取帳號 %s 的群益帳密失敗: %s", user_id, exc)
        return None
    if not rows:
        return None
    row = rows[0]
    return {"capital_user_id": row["capital_user_id"], "capital_password": row["capital_password"]}


def admin_set_broker_credentials(
    user_id: str, *, capital_user_id: str, capital_password: str, timeout: float = 10.0
) -> dict:
    """新增或覆蓋某個帳號的群益帳密。刻意沒有對應的一般 API 端點——只透過
    scripts/set_broker_credentials.py 這種本機工程操作呼叫，不透過網頁。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法儲存帳密")

    payload = {
        "user_id": user_id,
        "capital_user_id": capital_user_id,
        "capital_password": capital_password,
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    resp = requests.post(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers=headers,
        params={"on_conflict": "user_id"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else payload
