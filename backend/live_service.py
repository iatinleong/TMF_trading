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
from .broker_credentials_store import get_broker_credentials
from .capital_parse import (
    parse_future_rights_raw,
    parse_open_interest_raw,
    parse_order_report_raw,
)
from .config import app_base_dir
from .kline_engine import DEFAULT_KLINE_LIMIT, get_kline_store, get_store
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


def _apply_worker_account_credentials() -> None:
    """ACCOUNT_USER_ID 有設的話，用那個帳號自己的群益帳密覆蓋
    CAPITAL_USER_ID/CAPITAL_PASSWORD；沒設、或那個帳號還沒被設定過帳密，
    就維持原本的值不變（安靜跳過，不中止開機——跟其他遠端密鑰的失敗處理
    方式一致）。"""
    account_user_id = os.getenv("ACCOUNT_USER_ID", "").strip()
    if not account_user_id:
        return
    creds = get_broker_credentials(account_user_id)
    if not creds:
        logger.warning("ACCOUNT_USER_ID=%s 還沒有設定群益帳密，維持原本的值", account_user_id)
        return
    os.environ["CAPITAL_USER_ID"] = creds["capital_user_id"]
    os.environ["CAPITAL_PASSWORD"] = creds["capital_password"]


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

    # 2026-08-31 關鍵修正：若處於 Worker 模式，載入完遠端全域密鑰後，
    # 必須保證用該 Worker 自己的帳密覆蓋 CAPITAL_USER_ID / PASSWORD，
    # 避免後續 _load_project_env() 呼叫將專屬帳密覆蓋回共用主帳號。
    _apply_worker_account_credentials()


def _is_worker_mode() -> bool:
    """WORKER_MODE=1 代表這個行程是某個帳號自己的 worker，不是共用的主行程
    ——差別見 docs/superpowers/plans/2026-08-26-per-account-worker-process.md。"""
    return os.getenv("WORKER_MODE", "").strip().lower() in {"1", "true", "yes"}


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

# 2026-08-26 實測抓到的問題：Shioaji 補缺原本包在「群益自己的 quote_ready
# 變 true」分支底下，但群益的 OnConnection(3003) 事件實測過會間歇性完全不
# 觸發（quote_ready 因此永遠卡在 False），Shioaji 這個本來要拿來自動補救的
# 安全網就永遠沒機會執行——即使 Shioaji 本身是完全獨立的第三方連線，根本
# 不需要群益報價「就緒」才能查。改成跟 quote_ready 脫鉤、定期檢查一次（見
# _should_attempt_periodic_shioaji_backfill），不用等一個可能永遠不會發生
# 的事件。
SHIOAJI_BACKFILL_INTERVAL_SECONDS = 300.0
_last_shioaji_backfill_attempt: float = 0.0


def _should_attempt_periodic_shioaji_backfill(
    now: float, last_attempt: float, *, interval: float = SHIOAJI_BACKFILL_INTERVAL_SECONDS
) -> bool:
    return (now - last_attempt) >= interval


def _default_environment() -> int:
    _load_project_env()
    return int(os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION))


def auto_connect_on_startup() -> None:
    """部署啟動時自動連線（讀 .env，不需手動按連線）。"""
    global _connect_error, current_product
    _load_project_env()
    environment = _default_environment()
    product = _default_live_product()
    current_product = product
    _apply_worker_account_credentials()
    if os.getenv("AUTO_CONNECT", "1").strip().lower() in {"0", "false", "no"}:
        logger.info("AUTO_CONNECT 已關閉，跳過自動連線")
        return
    if platform.system() != "Windows":
        _connect_error = "非 Windows 環境，無法載入 SKCOM"
        logger.warning(_connect_error)
        return

    try:
        TradingService.get().connect(
            environment=environment,
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


def get_klines(
    product: str | None = None, limit: int = DEFAULT_KLINE_LIMIT, interval_minutes: int = 60
) -> list[dict[str, Any]]:
    code = resolve_quote_product_code(product or current_product)
    return get_kline_store(code, interval_minutes=interval_minutes).get_klines(limit=limit)


def set_product(product_code: str) -> dict[str, Any]:
    global current_product
    current_product = product_code
    st = _broker_state()
    if st.get("connected"):
        _svc().subscribe(product_code)
    return connection_meta()


async def poll_once() -> dict[str, Any] | None:
    """單次輪詢：刷新 broker 快照並回傳 tick 訊息。"""
    global _last_poll_at, _last_shioaji_backfill_attempt
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

    # 2026-09-17 實盤事故：TM2609 結算到期（每月第三個星期三）之後，市場上
    # 已經沒有這個合約的報價，但 SKCOM 訂閱、儀表板都還停在這個舊代碼上，
    # 導致圖表/K線一片空白。原本的換月判斷寫在 strategy_service._tick_one()
    # 裡，有兩個問題：(1) 只有在「有策略被武裝」時才會執行到，只要使用者
    # 把策略全部停用，就永遠不會被觸發；(2) 就算執行到了，也只是改
    # StrategyState 自己的 product_code 欄位，從來沒有真的呼叫
    # TradingService.subscribe() 去讓 SKCOM 連線層跟著換到新合約——兩個問題
    # 疊加起來，代表換月這件事實際上從來沒有真正自動生效過。這裡獨立判斷、
    # 不依賴任何策略是否啟動：只要目前訂閱的商品是過期的「TM+年月」代碼，
    # 立刻重新訂閱新的近月合約，讓連線層本身（不只是個別策略）保持正確。
    try:
        from .strategy_service import resolve_strategy_product_code

        resolved_product = resolve_strategy_product_code(product)
    except Exception as exc:  # noqa: BLE001 - 換月判斷失敗不該影響其餘輪詢
        logger.debug("換月判斷失敗（不影響主流程): %s", exc)
        resolved_product = product
    if resolved_product.upper() != product.upper():
        logger.info("偵測到近月合約已換月：%s -> %s，自動重新訂閱", product, resolved_product)
        try:
            await asyncio.to_thread(set_product, resolved_product)
            product = resolved_product
        except Exception as exc:  # noqa: BLE001
            logger.warning("自動換月重新訂閱失敗: %s", exc)

    quote_product = resolve_quote_product_code(product)
    loaded = st.get("kline_loaded_products") or []
    # 2026-08-26：K 線只由共用的主行程負責讀寫（見
    # docs/superpowers/plans/2026-08-26-per-account-worker-process.md）——
    # worker 行程（WORKER_MODE=1）不做群益歷史 K 線回補，避免多個帳號的
    # worker 同時搶著寫同一個商品代碼的 K 線快取檔案。
    if not _is_worker_mode() and st.get("quote_ready") and quote_product not in loaded:
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

    # 2026-08-26：Shioaji 補缺刻意跟上面的 quote_ready 分支脫鉤、獨立定期跑
    # （見 _should_attempt_periodic_shioaji_backfill 說明）——群益的
    # OnConnection(3003) 事件實測過會間歇性完全不觸發，quote_ready 卡住的話
    # 上面那個分支整輪都不會進來，Shioaji 這個安全網也就永遠沒機會執行。
    # Shioaji 是獨立的第三方連線，不需要等群益報價「就緒」。
    now = time.time()
    if not _is_worker_mode() and _should_attempt_periodic_shioaji_backfill(now, _last_shioaji_backfill_attempt):
        _last_shioaji_backfill_attempt = now
        try:
            from .shioaji_backfill import backfill_via_shioaji

            added = await asyncio.to_thread(backfill_via_shioaji, quote_product)
            if added:
                logger.info("Shioaji 定期補缺：%s 新增 %d 根", quote_product, added)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Shioaji 定期補缺失敗（不影響主流程）: %s", exc)

    if st.get("connected") and not st.get("quote_connected"):
        try:
            await asyncio.to_thread(_svc().subscribe, product)
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto quote reconnect failed: %s", exc)

    klines = get_klines(quote_product, limit=1)
    kline = klines[-1] if klines else None
    klines_15m = get_klines(quote_product, limit=1, interval_minutes=15)
    kline_15m = klines_15m[-1] if klines_15m else None

    return {
        "type": "tick",
        "product_code": product,
        "price": last,
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "kline": kline,
        "kline_15m": kline_15m,
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