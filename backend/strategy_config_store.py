"""backend/strategy_config_store.py — 帳號 ↔ 策略客製化綁定（Supabase
user_strategy_configs 表）。一般使用者的策略欄位預設是空的，只有管理員能新增
一列（把某個策略綁給某個帳號）；使用者自己只能切換「已經綁定給他」那幾列的
enabled（見 docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。

跟 secrets_store.py 用同一套輕量 REST 呼叫模式（不用完整 supabase-py SDK），
一律用 SUPABASE_SECRET_KEY 繞過 RLS——這張表沒有建任何 RLS policy，安全邊界
完全靠後端 API（backend/api.py 的 request.state.user_id/user_email + admin
判斷）把關，不是 Supabase 自己擋。這裡只負責讀寫，不驗證呼叫者是誰、有沒有
管理權限。
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

TABLE = "user_strategy_configs"


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


def list_user_strategy_configs(user_id: str, *, timeout: float = 10.0) -> list[dict]:
    """回傳這個使用者已經被客製化綁定的策略列；沒有任何綁定（新帳號的常態）
    就是空 list，不是錯誤。未設定 Supabase 或連線失敗都安靜回傳空 list。"""
    if not _configured():
        return []
    try:
        resp = requests.get(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers(),
            params={"user_id": f"eq.{user_id}", "select": "*"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取使用者策略設定失敗: %s", exc)
        return []


def set_strategy_enabled(
    user_id: str, strategy_id: str, enabled: bool, *, timeout: float = 10.0
) -> dict | None:
    """一般使用者的自助動作：只切換「已經存在」那一列的 enabled，不會建立新列
    ——新增綁定是管理員的事（見 admin_upsert_strategy_config）。回傳 None 代表
    這個使用者根本沒有這個 strategy_id 的設定，呼叫端要當 404 處理。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法更新策略設定")
    resp = requests.patch(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers={**_headers(), "Prefer": "return=representation"},
        params={"user_id": f"eq.{user_id}", "strategy_id": f"eq.{strategy_id}"},
        json={"enabled": enabled},
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def admin_upsert_strategy_config(
    user_id: str,
    strategy_id: str,
    *,
    product_code: str,
    qty: int | None,
    enabled: bool,
    timeout: float = 10.0,
) -> dict:
    """管理員專用：把某個策略客製化綁定給某個帳號（新增），或更新已經綁定過
    的參數；靠 (user_id, strategy_id) 的 unique 限制做 upsert。呼叫端
    （backend/api.py）要先驗證呼叫者是管理員，這裡不做這層檢查。失敗直接
    raise，讓 API 層回 400——管理員主動送出的設定，存不進去要讓他知道。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法儲存策略設定")

    payload = {
        "user_id": user_id,
        "strategy_id": strategy_id,
        "product_code": product_code,
        "qty": qty,
        "enabled": enabled,
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    resp = requests.post(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers=headers,
        params={"on_conflict": "user_id,strategy_id"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else payload
