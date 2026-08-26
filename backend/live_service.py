"""
live_service.py — 實盤 Dashboard 服務（參考賽博纏論 main.py 架構）

啟動時自動連線群益 API，背景輪詢報價/帳務，供 REST + WebSocket 推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .broker.capital_futures import Environment
from .capital_parse import (
    parse_future_rights_raw,
    parse_open_interest_raw,
    parse_order_report_raw,
)
from .config import app_base_dir
from .kline_engine import DEFAULT_KLINE_LIMIT, get_store
from .secrets_store import load_remote_secrets
from .trading_service import (
    PRODUCT_CODE_BY_CONTRACT,
    TradingService,
    compute_current_tmf_code,
    resolve_quote_product_code,
    trading_disabled,
)

logger = logging.getLogger(__name__)

BASE_DIR = app_base_dir(__file__)
ENV_PATH = BASE_DIR / ".env"


def _load_project_env() -> None:
    load_dotenv(ENV_PATH)
    # 2026-08-26：本機 .env 先當底線/備援值，讀完之後才嘗試用 Supabase 的
    # 遠端密鑰覆蓋（見 backend/secrets_store.py）——這樣多台機器只要都設定
    # 同一個 SUPABASE_SECRET_KEY，密鑰改一個地方全部同步；沒設定服務端
    # 金鑰、或連線失敗時，維持用本機 .env 既有值，不影響開機。
    try:
        load_remote_secrets()
    except Exception as exc:  # noqa: BLE001 - 遠端密鑰失敗不該擋開機
        logger.warning("套用 Supabase 密鑰時發生例外，忽略並使用本機 .env: %s", exc)


_load_project_env()


def _default_live_product() -> str:
    # 2026-08-21 修正：不要用寫死的字串當預設，會在每個月合約結算之後悄悄失效
    # （見 compute_current_tmf_code 說明）。LIVE_PRODUCT_CODE 保留給手動覆蓋用。
    override = os.getenv("LIVE_PRODUCT_CODE", "").strip().upper()
    return override or compute_current_tmf_code()


# 目前監看的商品（可被前端切換）
current_product: str = _default_live_product()
price_cache: dict[str, float] = {}
ws_clients: dict[str, set] = {}  # product_code -> WebSocket set

_connect_error: str = ""
_last_poll_at: float | None = None


def _default_environment() -> int:
    _load_project_env()
    return int(os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION))


def auto_connect_on_startup() -> None:
    """部署啟動時自動連線（讀 .env，不需手動按連線）。"""
    global _connect_error, current_product
    _load_project_env()
    if os.getenv("AUTO_CONNECT", "1").strip().lower() in {"0", "false", "no"}:
        logger.info("AUTO_CONNECT 已關閉，跳過自動連線")
        return
    if platform.system() != "Windows":
        _connect_error = "非 Windows 環境，無法載入 SKCOM"
        logger.warning(_connect_error)
        return

    product = _default_live_product()
    current_product = product
    try:
        TradingService.get().connect(
            environment=_default_environment(),
            product_code=product,
            account=os.getenv("CAPITAL_ACCOUNT") or None,
        )
        _connect_error = ""
        logger.info("實盤 Dashboard 自動連線成功 product=%s", product)
    except Exception as exc:  # noqa: BLE001
        _connect_error = str(exc) or repr(exc)
        logger.exception("自動連線失敗: %s", exc)


def _svc() -> TradingService:
    return TradingService.get()


def _broker_state() -> dict[str, Any]:
    try:
        return _svc().status()
    except Exception:
        return {"connected": False}


def connection_meta() -> dict[str, Any]:
    st = _broker_state()
    return {
        "connected": bool(st.get("connected")),
        "error": _connect_error,
        "product_code": current_product,
        "environment": st.get("environment"),
        "user_id": st.get("user_id"),
        "active_account": st.get("active_account"),
        "last_poll_at": _last_poll_at,
        "kline_api_ok": st.get("kline_api_ok", True),
        "kline_api_block_reason": st.get("kline_api_block_reason", ""),
        "agreements": st.get("agreements") or [],
        "trading_disabled": trading_disabled(),
        "quote": st.get("quote") or {},
        "kline_loaded_products": st.get("kline_loaded_products") or [],
        "kline_bar_count": len(
            get_store(resolve_quote_product_code(st.get("subscribed_product") or current_product)).get_klines(
                limit=0
            )
        ),
        "quote_connected": st.get("quote_connected", 0),
        "quote_ready": st.get("quote_ready", False),
        "order_product_code": st.get("order_product_code", ""),
    }


def get_account() -> dict[str, Any]:
    st = _broker_state()
    rights = st.get("rights") or {}
    if not rights and st.get("rights_raw"):
        rights = parse_future_rights_raw(st.get("rights_raw") or "")
    quote = st.get("quote") or {}
    return {
        "connected": st.get("connected", False),
        "user_id": st.get("user_id", ""),
        "active_account": st.get("active_account", ""),
        "environment": st.get("environment"),
        "rights_raw": st.get("rights_raw") or "",
        "rights": rights,
        "equity": rights.get("equity"),
        "floating_pl": rights.get("floating_pl"),
        "available_balance": rights.get("available_balance"),
        "maintenance_margin": rights.get("maintenance_margin"),
        "initial_margin": rights.get("initial_margin"),
        "risk_indicator": rights.get("risk_indicator"),
        "maintenance_rate": rights.get("maintenance_rate"),
        "intraday_unrealized": rights.get("intraday_unrealized"),
        "futures_close_pl": rights.get("futures_close_pl"),
        "account_balance": rights.get("account_balance"),
        "last_price": quote.get("last_price"),
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
        "quote_updated_at": quote.get("updated_at"),
    }


def get_positions() -> list[dict[str, Any]]:
    st = _broker_state()
    positions = st.get("positions")
    if positions:
        return positions
    return parse_open_interest_raw(st.get("positions_raw", ""))


def get_orders() -> list[dict[str, Any]]:
    st = _broker_state()
    return parse_order_report_raw(st.get("orders_raw") or "")


def get_fills() -> list[dict[str, Any]]:
    st = _broker_state()
    raw = st.get("fulfills_raw") or ""
    return [{"raw": line.strip()} for line in str(raw).splitlines() if line.strip()]


def get_ticker(product: str | None = None) -> dict[str, Any]:
    product = product or current_product
    st = _broker_state()
    q = st.get("quote") or {}
    subscribed = st.get("subscribed_product") or current_product
    # Only use live quote data if it matches the requested product
    if subscribed == product:
        last = q.get("last_price") or price_cache.get(product)
        bid = q.get("bid")
        ask = q.get("ask")
        volume = q.get("volume")
        updated_at = q.get("updated_at")
    else:
        last = price_cache.get(product)
        bid = None
        ask = None
        volume = None
        updated_at = None
    if last is not None:
        price_cache[product] = float(last)
    return {
        "product_code": product,
        "last_price": last,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "updated_at": updated_at,
    }


def get_klines(product: str | None = None, limit: int = DEFAULT_KLINE_LIMIT) -> list[dict[str, Any]]:
    code = resolve_quote_product_code(product or current_product)
    return get_store(code).get_klines(limit=limit)


def set_product(product_code: str) -> dict[str, Any]:
    global current_product
    current_product = product_code
    st = _broker_state()
    if st.get("connected"):
        _svc().subscribe(product_code)
    return connection_meta()


async def poll_once() -> dict[str, Any] | None:
    """單次輪詢：刷新 broker 快照並回傳 tick 訊息。"""
    global _last_poll_at
    st = _broker_state()
    if not st.get("connected"):
        return None

    def _sync_refresh() -> dict[str, Any]:
        _svc().refresh()
        return _svc().status()

    try:
        st = await asyncio.to_thread(_sync_refresh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("poll refresh failed: %s", exc)
        return None

    _last_poll_at = time.time()
    q = st.get("quote") or {}
    product = st.get("subscribed_product") or current_product
    last = q.get("last_price")
    if last is not None:
        price_cache[product] = float(last)

    quote_product = resolve_quote_product_code(product)
    loaded = st.get("kline_loaded_products") or []
    if st.get("quote_ready") and quote_product not in loaded:
        bar_limit = int(os.getenv("CAPITAL_KLINE_BAR_LIMIT", "500"))
        try:
            ok = await asyncio.to_thread(
                _svc().try_load_kline_history, quote_product, bar_limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("deferred kline load: %s", exc)
            ok = False
        # 2026-08-26：VM 上回報「Shioaji 補缺好像沒觸發」，但完全查不出是「群益
        # 回補這一步就沒成功」還是「群益回補成功了、Shioaji 那步才沒動作」——
        # 兩種在稽核 log 上看起來一模一樣（都是完全沒有紀錄）。這裡補一筆閘門
        # 本身的紀錄，才能分清楚卡在哪一層。
        try:
            from .shioaji_backfill import _append_audit as _append_shioaji_audit

            _append_shioaji_audit(
                f"gate\tproduct={quote_product}\tkline_load_ok={ok}"
            )
        except Exception:  # noqa: BLE001 - 稽核 log 不該影響主流程
            pass
        if ok:
            # 2026-08-25：群益回補對「進行中交易時段」已經收盤的 K 棒不給資料
            # （見 backend/shioaji_backfill.py 檔頭說明），這裡在群益回補成功
            # 後順手檢查殘留缺口，有永豐金鑰才會真的連線，純粹錦上添花，
            # 失敗/沒設定都不影響主流程。只在剛回補完那一次跑，不會每輪重跑。
            try:
                from .shioaji_backfill import backfill_via_shioaji

                added = await asyncio.to_thread(backfill_via_shioaji, quote_product)
                if added:
                    logger.info("Shioaji 補缺完成：%s 新增 %d 根", quote_product, added)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Shioaji 補缺失敗（不影響主流程）: %s", exc)

    if st.get("connected") and not st.get("quote_connected"):
        try:
            def _sync_reconnect_quote() -> None:
                _svc()._broker.enter_monitor()
                _svc().subscribe(product)
            await asyncio.to_thread(_sync_reconnect_quote)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto quote reconnect failed: %s", exc)

    klines = get_klines(quote_product, limit=1)
    kline = klines[-1] if klines else None

    return {
        "type": "tick",
        "product_code": product,
        "price": last,
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "kline": kline,
        "connected": True,
    }


async def poll_loop() -> None:
    """背景輪詢（參考賽博纏論 poll_loop）。"""
    while True:
        watched = {p for p, clients in list(ws_clients.items()) if clients}
        watched.add(current_product)
        tick = await poll_once()
        if tick and tick.get("price") is not None:
            tick_product = tick.get("product_code") or current_product
            msg = json.dumps(tick, ensure_ascii=False)
            # Only send tick to clients subscribed to the matching product
            target_clients = ws_clients.get(tick_product, set())
            dead = set()
            for ws in list(target_clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.add(ws)
            target_clients -= dead
        await asyncio.sleep(5.0)


def place_order(payload: dict[str, Any]) -> dict[str, Any]:
    return _svc().place_order(payload)


def place_stop_order(payload: dict[str, Any]) -> dict[str, Any]:
    return _svc().place_stop_order(payload)


def place_mit_order(payload: dict[str, Any]) -> dict[str, Any]:
    return _svc().place_mit_order(payload)


def cancel_stop_order(payload: dict[str, Any]) -> dict[str, Any]:
    return _svc().cancel_stop_order(payload)


def query_stop_loss_report(account: str | None = None) -> dict[str, Any]:
    return _svc().query_stop_loss_report(account)


def close_position(payload: dict[str, Any]) -> dict[str, Any]:
    return _svc().close_position(
        product_code=str(payload.get("product_code", current_product)),
        direction_key=str(payload.get("direction_key", "")),
        qty=int(payload.get("qty", 1)),
        account=payload.get("account"),
    )


def flatten_all_positions(account: str | None = None) -> dict[str, Any]:
    return _svc().flatten_all(account=account)


def cancel_order(seq_no: str, account: str | None = None) -> dict[str, Any]:
    return _svc().cancel_order(seq_no, account=account)


def trading_params_for_sidebar() -> dict[str, Any]:
    from .trading_service import trading_params_schema

    from .strategy_service import strategy_config_defaults

    schema = trading_params_schema()
    schema["live_strategy"] = strategy_config_defaults()
    return schema