"""backend/strategy_config_store.py — 帳號 ↔ 策略客製化綁定（Supabase
user_strategy_configs 表）。一般使用者的策略欄位預設是空的；綁定是工程操作
（見 scripts/bind_strategy.py），不是 Dashboard 網頁上的功能——「客製化」
指的是幫這個帳號另外寫進出場邏輯的程式碼，不是填一份網頁表單（見
docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。登入之後，
Dashboard 只會讀這張表、顯示「這個帳號綁定了哪些策略」，不會寫入。

跟 secrets_store.py 用同一套輕量 REST 呼叫模式（不用完整 supabase-py SDK），
一律用 SUPABASE_SECRET_KEY 繞過 RLS——這張表沒有建任何 RLS policy，安全邊界
靠「一般使用者的 Supabase JWT 只換得到唯讀查詢自己那幾列」（backend/api.py
的 request.state.user_id）+「寫入只能透過拿得到 SUPABASE_SECRET_KEY 的人在
機器上執行 scripts/bind_strategy.py」把關，不是 Supabase 自己擋。這裡只負責
讀寫，不驗證呼叫者是誰。
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


def admin_upsert_strategy_config(
    user_id: str,
    strategy_id: str,
    *,
    product_code: str | None = None,
    qty: int | None = None,
    enabled: bool | None = None,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
    max_loss_ntd: float | None = None,
    max_loss_pct: float | None = None,
    oco_enabled: bool | None = None,
    soft_stop_enabled: bool | None = None,
    risk_insurance_enabled: bool | None = None,
    reverse_signal_exit_enabled: bool | None = None,
    timeout: float = 10.0,
) -> dict:
    """把某個策略客製化綁定給某個帳號（新增），或更新已經綁定過的參數；靠
    (user_id, strategy_id) 的 unique 限制做 upsert。這是唯一能讓某個帳號的
    策略欄位「從空變成有東西」的路徑——刻意不透過 Dashboard 網頁操作，是給
    scripts/bind_strategy.py 這種部署時的工程操作用的（見該腳本說明）。
    失敗直接 raise，呼叫端要讓操作者知道存不進去，不能安靜吞掉。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法儲存策略設定")

    payload: dict[str, object] = {
        "user_id": user_id,
        "strategy_id": strategy_id,
    }
    # enabled 也是 NOT NULL DEFAULT false，跟 product_code 同一種問題：
    # scripts/bind_strategy.py 的 --enabled/--disabled 是共用 dest="enabled"、
    # 預設值 None 的一對旗標，兩者都不帶時 args.enabled 是 None，明確送 null
    # 一樣違反 NOT NULL（2026-09-29 修 product_code 時漏看了這個同類欄位）。
    if enabled is not None:
        payload["enabled"] = bool(enabled)
    # product_code 是 NOT NULL DEFAULT 'TM2608'：不能明確送 null，Postgres
    # 的欄位預設值只在「完全不給這個欄位」時才生效，明確給 null 一律違反
    # NOT NULL（2026-09-29 實測抓到：綁定新策略沒指定 --product-code 時
    # 這裡送出 null，被 PostgREST 回 400）。
    if product_code is not None:
        payload["product_code"] = product_code
    if qty is not None:
        payload["qty"] = qty
    if stop_loss_points is not None:
        payload["stop_loss_points"] = float(stop_loss_points)
    if take_profit_points is not None:
        payload["take_profit_points"] = float(take_profit_points)
    if max_loss_ntd is not None:
        payload["max_loss_ntd"] = float(max_loss_ntd)
    if max_loss_pct is not None:
        payload["max_loss_pct"] = float(max_loss_pct)
    if oco_enabled is not None:
        payload["oco_enabled"] = bool(oco_enabled)
    if soft_stop_enabled is not None:
        payload["soft_stop_enabled"] = bool(soft_stop_enabled)
    if risk_insurance_enabled is not None:
        payload["risk_insurance_enabled"] = bool(risk_insurance_enabled)
    if reverse_signal_exit_enabled is not None:
        payload["reverse_signal_exit_enabled"] = bool(reverse_signal_exit_enabled)

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
