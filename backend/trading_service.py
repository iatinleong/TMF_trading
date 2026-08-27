"""
trading_service.py — 實盤交易會話管理（群益 SKCOM API）

提供 dashboard 用的連線、參數 schema、下單、狀態查詢。
僅支援 Windows + 已註冊 SKCOM.dll。
"""

from __future__ import annotations

import logging
import os
import platform
import queue
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, TypeVar

from pathlib import Path

from dotenv import load_dotenv

from .broker.capital_futures import (
    BuySell,
    CapitalFuturesBroker,
    Environment,
    NewClose,
    SmartOrderKind,
    SmartOrderPriceType,
    SmartOrderTradeType,
    TradeType,
    TriggerDirection,
)
from .config import DEFAULT_STRATEGY, ContractSpec, app_base_dir

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class _ComJob:
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None

_BASE_DIR = app_base_dir(__file__)
_ENV_PATH = _BASE_DIR / ".env"


def _load_project_env() -> None:
    load_dotenv(_ENV_PATH, override=True)


def trading_disabled() -> bool:
    _load_project_env()
    return os.getenv("DISABLE_TRADING", "1").strip().lower() in {"1", "true", "yes"}


def set_trading_disabled(disabled: bool) -> bool:
    """從儀表板切換是否允許真實下單；同時更新目前 process 與 .env 檔（重開機也會保留）。"""
    value = "1" if disabled else "0"
    os.environ["DISABLE_TRADING"] = value

    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("DISABLE_TRADING="):
            lines[i] = f"DISABLE_TRADING={value}"
            found = True
            break
    if not found:
        lines.append(f"DISABLE_TRADING={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return trading_disabled()


def _require_trading_enabled() -> None:
    if trading_disabled():
        raise RuntimeError(
            "目前已關閉下單（DISABLE_TRADING=1）。請先確認 K 線與報價正確後，"
            "於儀表板開啟「允許真實下單」再啟用。"
        )


# 商品代碼對照（群益 SKCOM）
# - 大台/小台：報價常用連續碼 TX00/MTX00；下單用近月 TXFR1/MXFR1
# - 微台：實測 TMF00/TMFR1 無效；須用「TM + YYMM」具體月份碼（例 2026/08 = TM2608）
#   報價與下單同一碼。近月代碼預設由 compute_current_tmf_code() 自動算，
#   真的算錯了才用 .env 的 CAPITAL_TMF_CODE / LIVE_PRODUCT_CODE 手動覆蓋救急。
DEFAULT_TMF_CODE = "TM2608"
# 舊文件/UI 常見但實測不可用的微台別名 → 轉成 DEFAULT_TMF_CODE
_LEGACY_TMF_ALIASES = frozenset({"TMF00", "TMFR1", "TMF"})


def third_wednesday(year: int, month: int) -> date:
    """台指期貨（TX/MTX/TMF）每月結算日：該月第三個星期三（交易所公開規則）。"""
    first_of_month = date(year, month, 1)
    days_until_wednesday = (2 - first_of_month.weekday()) % 7  # Monday=0 ... Wednesday=2
    first_wednesday = first_of_month + timedelta(days=days_until_wednesday)
    return first_wednesday + timedelta(days=14)


def compute_current_tmf_code(today: date | None = None) -> str:
    """
    2026-08-21 實盤稽核發現：.env 裡的 LIVE_PRODUCT_CODE 是寫死的月份碼，8月合約
    08-19 結算到期後沒人手動更新，導致報價一直是 null（近月合約已經死了）。這個
    函式改成每次連線都自動算「現在近月是哪個月」：今天在這個月結算日（含）之前，
    近月就是這個月；結算日之後，自動跳下個月。避免每個月都要有人手動記得改。
    """
    today = today or date.today()
    settlement = third_wednesday(today.year, today.month)
    if today <= settlement:
        target_year, target_month = today.year, today.month
    else:
        target_year, target_month = today.year, today.month + 1
        if target_month > 12:
            target_month = 1
            target_year += 1
    return f"TM{target_year % 100:02d}{target_month:02d}"


def _tmf_month_code() -> str:
    """微台當月合約碼；優先 CAPITAL_TMF_CODE/LIVE_PRODUCT_CODE 手動覆蓋，否則自動算近月。"""
    _load_project_env()
    override = (os.getenv("CAPITAL_TMF_CODE") or os.getenv("LIVE_PRODUCT_CODE") or "").strip().upper()
    return override or compute_current_tmf_code()


def _quote_code_map() -> dict[str, str]:
    return {
        "TX": "TX00",
        "MTX": "MTX00",
        "TMF": _tmf_month_code(),
    }


def _order_code_map() -> dict[str, str]:
    return {
        "TX": "TXFR1",
        "MTX": "MXFR1",
        "TMF": _tmf_month_code(),
    }


# 相容舊 import / 文件：模組載入時的快照（執行時請用 resolve_* 或 _*_code_map）
QUOTE_CODE_BY_CONTRACT = {
    "TX": "TX00",
    "MTX": "MTX00",
    "TMF": DEFAULT_TMF_CODE,
}
ORDER_CODE_BY_CONTRACT = {
    "TX": "TXFR1",
    "MTX": "MXFR1",
    "TMF": DEFAULT_TMF_CODE,
}
PRODUCT_CODE_BY_CONTRACT = ORDER_CODE_BY_CONTRACT


def resolve_quote_product_code(product_code: str | None, *, contract: str = "TMF") -> str:
    quote_map = _quote_code_map()
    order_map = _order_code_map()
    code = (product_code or "").strip().upper()
    if not code:
        return quote_map.get(contract.upper(), _tmf_month_code())
    if code in _LEGACY_TMF_ALIASES:
        return quote_map["TMF"]
    if code in quote_map:
        return quote_map[code]
    for key, order_code in order_map.items():
        if code == order_code.upper():
            return quote_map[key]
    # 大台/小台連續碼；微台 TMyymm 原樣通過
    return code


def resolve_order_product_code(product_code: str | None, *, contract: str = "TMF") -> str:
    quote_map = _quote_code_map()
    order_map = _order_code_map()
    code = (product_code or "").strip().upper()
    if not code:
        return order_map.get(contract.upper(), _tmf_month_code())
    if code in _LEGACY_TMF_ALIASES:
        return order_map["TMF"]
    if code in order_map:
        return order_map[code]
    for key, quote_code in quote_map.items():
        if code == quote_code.upper():
            return order_map[key]
    return code

ENV_LABEL = {
    Environment.PRODUCTION: "正式環境",
    Environment.PRODUCTION_SGX: "正式環境SGX",
    Environment.TEST: "測試環境",
    Environment.TEST_SGX: "測試環境SGX",
}


def trading_params_schema() -> dict[str, Any]:
    """回傳 dashboard 可填寫的全部參數定義。"""
    s = DEFAULT_STRATEGY
    return {
        "connection": {
            "environment": {
                "type": "select",
                "options": [
                    {"value": Environment.PRODUCTION, "label": "正式環境 (0)"},
                    {"value": Environment.TEST, "label": "測試環境 (2)"},
                ],
                "default": int(os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION)),
                "note": "測試環境需專用測試帳號；正式帳號請用正式環境。",
            },
            "user_id": {"type": "string", "default": os.getenv("CAPITAL_USER_ID", ""), "label": "登入 ID"},
            "password": {"type": "password", "default": "", "label": "密碼（建議用 .env）"},
        },
        "instrument": {
            "contract": {
                "type": "select",
                "options": ["TX", "MTX", "TMF"],
                "default": "TMF",
            },
            "product_code": {
                "type": "string",
                "default": DEFAULT_TMF_CODE,
                "note": (
                    "微台用 TMyymm（例 TM2608，報價/下單同碼）；"
                    "大台/小台報價 TX00/MTX00、下單 TXFR1/MXFR1。"
                    "TMF00/TMFR1 為舊別名，會自動對應當月微台碼。"
                ),
            },
            "account": {"type": "string", "default": "", "note": "留空則自動選期貨帳號"},
        },
        "strategy": {
            "strategy": {"type": "select", "options": ["breakout", "pullback"], "default": "pullback"},
            "exit_mode": {
                "type": "select",
                "options": [
                    {"value": "with_sltp", "label": "有點數 SL/TP"},
                    {"value": "signal_only", "label": "無點數（純訊號反手）"},
                ],
                "default": "signal_only",
            },
            "ma_fast": {"type": "int", "default": s.ma_fast},
            "ma_mid": {"type": "int", "default": s.ma_mid},
            "ma_slow": {"type": "int", "default": s.ma_slow},
            "breakout_stop_loss_points": {"type": "float", "default": s.breakout_stop_loss_points},
            "breakout_take_profit_points": {"type": "float", "default": s.breakout_take_profit_points},
            "pullback_stop_loss_points": {"type": "float", "default": s.pullback_stop_loss_points},
            "pullback_take_profit_points": {"type": "float", "default": s.pullback_take_profit_points},
        },
        "order": {
            "side": {"type": "select", "options": ["buy", "sell"], "default": "buy"},
            "qty": {"type": "int", "default": 1, "min": 1},
            "price": {
                "type": "string",
                "default": "M",
                "note": "搭配 IOC/FOK：M=市價、P=範圍市價；限價請填實際價格字串（不要填 0，會被拒單）",
            },
            "trade_type": {
                "type": "select",
                "options": [
                    {"value": TradeType.ROD, "label": "ROD"},
                    {"value": TradeType.IOC, "label": "IOC"},
                    {"value": TradeType.FOK, "label": "FOK"},
                ],
                "default": TradeType.FOK,
            },
            "new_close": {
                "type": "select",
                "options": [
                    {"value": NewClose.NEW, "label": "新倉"},
                    {"value": NewClose.CLOSE, "label": "平倉"},
                    {"value": NewClose.AUTO, "label": "自動"},
                ],
                "default": NewClose.AUTO,
            },
            "day_trade": {"type": "select", "options": [0, 1], "default": 0, "labels": ["否", "是"]},
        },
        "cost": {
            "slippage_points": {"type": "float", "default": 0.0},
            "commission_per_side": {"type": "float", "default": None, "note": "留空用契約預設"},
            "tax_rate": {"type": "float", "default": 0.00002},
        },
    }


def trading_capability_report() -> dict[str, Any]:
    """盤點目前環境能否順利掛單。"""
    _load_project_env()
    is_windows = platform.system() == "Windows"
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("作業系統為 Windows", is_windows, platform.system())
    add("CAPITAL_USER_ID 已設定", bool(os.getenv("CAPITAL_USER_ID")), "從 .env 讀取")
    add("CAPITAL_PASSWORD 已設定", bool(os.getenv("CAPITAL_PASSWORD")), "從 .env 讀取，不在 API 回傳")

    dll_ok = False
    dll_detail = "未檢查"
    if is_windows:
        try:
            from .broker.capital_futures import _find_default_dll_path

            dll_ok = _find_default_dll_path().exists()
            dll_detail = str(_find_default_dll_path())
        except Exception as exc:  # noqa: BLE001
            dll_detail = str(exc)
    add("SKCOM.dll 可找到", dll_ok, dll_detail)

    env = int(os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION))
    add(
        "環境設定",
        True,
        f"{ENV_LABEL.get(env, str(env))} — 正式帳號請用 0，測試帳號請用 2",
    )

    return {
        "can_trade": is_windows and dll_ok and bool(os.getenv("CAPITAL_USER_ID")),
        "platform": platform.system(),
        "checks": checks,
        "supported_actions": [
            "login",
            "subscribe_quote",
            "place_order",
            "cancel_order",
            "get_orders",
            "get_positions",
            "get_fills",
        ],
        "notes": [
            "已實作：登入、報價訂閱、下單、刪單、委託/成交/持倉查詢。",
            "自動策略：回測/回彈 + 無點數純訊號反手；虧損 1 萬或 -10% 自動停機。",
            "測試環境需專用測試帳號；96571013 這類正式戶請用正式環境。",
        ],
    }


class TradingService:
    _instance: TradingService | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._broker: CapitalFuturesBroker | None = None
        self._com_queue: queue.Queue[_ComJob | None] = queue.Queue()
        self._com_stop = threading.Event()
        self._com_ready = threading.Event()
        self._com_thread = threading.Thread(
            target=self._com_worker, name="capital-com", daemon=True
        )
        self._com_thread.start()
        if not self._com_ready.wait(timeout=10.0):
            logger.warning("COM 專用執行緒啟動逾時")

    @classmethod
    def get(cls) -> TradingService:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = TradingService()
            return cls._instance

    def _require_windows(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("群益 SKCOM API 僅支援 Windows。")

    def _com_worker(self) -> None:
        import pythoncom

        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        self._com_ready.set()
        while not self._com_stop.is_set():
            try:
                job = self._com_queue.get(timeout=0.15)
            except queue.Empty:
                with self._lock:
                    if self._broker and self._broker.live.connected:
                        try:
                            self._broker.pump_events(0.2)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("pump_events failed: %s", exc)
                continue
            if job is None:
                break
            try:
                job.value = job.fn()
            except BaseException as exc:  # noqa: BLE001
                job.error = exc
            finally:
                job.done.set()

    def _run_com(self, fn: Callable[[], T]) -> T:
        job = _ComJob(fn=fn)
        self._com_queue.put(job)
        job.done.wait()
        if job.error:
            raise job.error
        return job.value

    def connect(
        self,
        *,
        environment: int | None = None,
        user_id: str | None = None,
        password: str | None = None,
        product_code: str | None = None,
        account: str | None = None,
    ) -> dict[str, Any]:
        self._require_windows()
        return self._run_com(
            lambda: self._connect_impl(
                environment=environment,
                user_id=user_id,
                password=password,
                product_code=product_code,
                account=account,
            )
        )

    def _connect_impl(
        self,
        *,
        environment: int | None,
        user_id: str | None,
        password: str | None,
        product_code: str | None,
        account: str | None,
    ) -> dict[str, Any]:
        _load_project_env()
        env = int(
            environment
            if environment is not None
            else os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION)
        )
        with self._lock:
            if self._broker and self._broker.live.connected:
                self._disconnect_impl()

            broker = CapitalFuturesBroker(environment=env)
            # 對齊 PythonExampleV2 Quote.py：登入後先連報價，下單初始化延後
            broker.login(user_id=user_id, password=password, request_agreement=False)
            quote_product = resolve_quote_product_code(product_code)
            order_product = resolve_order_product_code(product_code)
            broker.live.order_product_code = order_product
            # 報價 3003 間歇性卡住是已知問題（跟這台機器/帳號無關，重跑
            # EnterMonitorLONG 通常就會恢復），所以在同一次連線裡多重試幾次，
            # 不要一次等滿 120 秒就放棄——這樣使用者不用手動重啟整個服務。
            quote_ok = False
            for attempt in range(1, 4):
                quote_ok = broker.subscribe_quote(quote_product, wait_timeout=40.0)
                if quote_ok:
                    break
                logger.warning("報價連線第 %s/3 次嘗試逾時，重試 EnterMonitorLONG", attempt)
            if not quote_ok:
                logger.warning("報價連線 3 次嘗試皆逾時，將繼續初始化下單元件")

            accounts = broker.initialize_order(wait_seconds=5.0)
            if not accounts:
                raise RuntimeError("登入成功但未取得交易帳號。")

            if account:
                broker.live.active_account = account
            broker.request_agreement_status(wait_seconds=2.0)
            kline_limit = int(os.getenv("CAPITAL_KLINE_BAR_LIMIT", "500"))
            broker.try_load_kline_history(quote_product, bar_limit=kline_limit)
            broker.pump_events(5.0)
            if broker.live.active_account:
                broker.refresh_live_snapshot(broker.live.active_account, force=True)
            self._broker = broker
        return self._status_impl()

    def disconnect(self) -> dict[str, Any]:
        return self._run_com(self._disconnect_impl)

    def _disconnect_impl(self) -> dict[str, Any]:
        with self._lock:
            if self._broker:
                self._broker.live.connected = False
            self._broker = None
        return {"connected": False}

    def status(self) -> dict[str, Any]:
        return self._run_com(self._status_impl)

    def _status_impl(self) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                return {"connected": False, "message": "尚未連線"}
            return self._broker.get_live_state()

    def refresh(self) -> dict[str, Any]:
        return self._run_com(self._refresh_impl)

    def _refresh_impl(self) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            self._broker.refresh_live_snapshot()
            return self._broker.get_live_state()

    def subscribe(self, product_code: str) -> dict[str, Any]:
        return self._run_com(lambda: self._subscribe_impl(product_code))

    def _subscribe_impl(self, product_code: str) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            quote_product = resolve_quote_product_code(product_code)
            self._broker.subscribe_quote(quote_product, wait_timeout=120.0)
            self._broker.pump_events(3.0)
            return self._broker.get_live_state()

    def _try_load_kline_impl(self, product_code: str, bar_limit: int = 500) -> bool:
        with self._lock:
            if not self._broker:
                return False
            return self._broker.try_load_kline_history(product_code, bar_limit=bar_limit)

    def try_load_kline_history(self, product_code: str, bar_limit: int = 500) -> bool:
        return self._run_com(
            lambda: self._try_load_kline_impl(product_code, bar_limit=bar_limit)
        )

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_trading_enabled()
        return self._run_com(lambda: self._place_order_impl(payload))

    def _place_order_impl(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            raw_account = payload.get("account") or broker.live.active_account
            if not raw_account:
                raise ValueError("請指定交易帳號")
            account = str(raw_account)

            side_key = str(payload.get("side", "buy")).lower()
            side = BuySell.BUY if side_key == "buy" else BuySell.SELL
            result = broker.send_future_order(
                account=account,
                product_code=str(
                    payload.get(
                        "product_code",
                        broker.live.order_product_code
                        or resolve_order_product_code(broker.live.subscribed_product),
                    )
                ),
                side=side,
                qty=int(payload.get("qty") or 1),
                price=str(payload.get("price") or "M"),  # 不帶價格時預設市價（"M"，不是 "0"）
                trade_type=int(payload.get("trade_type") or TradeType.FOK),
                new_close=int(payload.get("new_close", NewClose.AUTO)),
                day_trade=int(payload.get("day_trade", 0)),
            )
            broker.pump_events(2.0)
            broker.refresh_live_snapshot(account)
            return {
                "order_result": {
                    "success": result.success,
                    "code": result.code,
                    "message": result.message,
                    "raw": result.raw,
                },
                "state": broker.get_live_state(),
            }

    def close_position(
        self,
        *,
        product_code: str,
        direction_key: str,
        qty: int,
        account: str | None = None,
    ) -> dict[str, Any]:
        """平倉單筆持倉（direction_key: long / short）。"""
        _require_trading_enabled()
        if direction_key not in {"long", "short"}:
            raise ValueError("direction_key 須為 long 或 short")
        if qty <= 0:
            raise ValueError("口數須大於 0")
        side = "sell" if direction_key == "long" else "buy"
        return self.place_order(
            {
                "account": account,
                "product_code": product_code,
                "side": side,
                "qty": int(qty),
                "price": "M",  # 市價（見 capital_futures.send_future_order 說明），"0" 會被拒單
                "trade_type": TradeType.FOK,
                "new_close": NewClose.CLOSE,
            }
        )

    def flatten_all(self, account: str | None = None) -> dict[str, Any]:
        """一鍵平倉：關閉所有持倉。"""
        _require_trading_enabled()
        st = self.status()
        if not st.get("connected"):
            raise RuntimeError("尚未連線")
        positions = st.get("positions") or []
        results: list[dict[str, Any]] = []
        for pos in positions:
            direction_key = pos.get("direction_key")
            if direction_key not in {"long", "short"}:
                continue
            try:
                qty = int(float(str(pos.get("qty", "0")).replace(",", "")))
            except ValueError:
                continue
            if qty <= 0:
                continue
            product = str(
                pos.get(
                    "product",
                    st.get("order_product_code")
                    or resolve_order_product_code(st.get("subscribed_product")),
                )
            )
            try:
                results.append(
                    {
                        "product": product,
                        "result": self.close_position(
                            product_code=product,
                            direction_key=str(direction_key),
                            qty=qty,
                            account=account,
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append({"product": product, "error": str(exc)})
        return {"closed": len(results), "results": results}

    def place_stop_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        送出真正的券商端 STP 停損智慧單（見 broker/capital_futures.py 的
        send_future_stp_order 說明）。跟 place_order 一樣要求交易總開關為開啟，
        因為這是會真的掛在券商那邊的真實委託。
        """
        _require_trading_enabled()
        return self._run_com(lambda: self._place_stop_order_impl(payload))

    def _place_stop_order_impl(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            raw_account = payload.get("account") or broker.live.active_account
            if not raw_account:
                raise ValueError("請指定交易帳號")
            account = str(raw_account)

            side_key = str(payload.get("side", "sell")).lower()
            side = BuySell.BUY if side_key == "buy" else BuySell.SELL
            new_close_key = str(payload.get("new_close", "close")).lower()
            new_close = NewClose.CLOSE if new_close_key == "close" else NewClose.NEW

            result = broker.send_future_stp_order(
                account=account,
                stock_no=str(payload.get("stock_no") or "TMF"),
                settlement_month=str(payload.get("settlement_month") or ""),
                side=side,
                qty=int(payload.get("qty") or 1),
                trigger_price=str(payload.get("trigger_price") or ""),
                price=str(payload.get("price") or "P"),
                new_close=new_close,
                trade_type=int(payload.get("trade_type", SmartOrderTradeType.ROD)),
                order_price_type=int(payload.get("order_price_type", SmartOrderPriceType.RANGE_MARKET)),
            )
            broker.pump_events(2.0)
            broker.refresh_live_snapshot(account)
            return {
                "order_result": {
                    "success": result.success,
                    "code": result.code,
                    "message": result.message,
                    "raw": result.raw,
                },
                "state": broker.get_live_state(),
            }

    def place_mit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """送出真正的券商端 MIT 觸價智慧單（用來做真實停利單，見 send_future_mit_order）。"""
        _require_trading_enabled()
        return self._run_com(lambda: self._place_mit_order_impl(payload))

    def _place_mit_order_impl(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            raw_account = payload.get("account") or broker.live.active_account
            if not raw_account:
                raise ValueError("請指定交易帳號")
            account = str(raw_account)

            side_key = str(payload.get("side", "sell")).lower()
            side = BuySell.BUY if side_key == "buy" else BuySell.SELL
            new_close_key = str(payload.get("new_close", "close")).lower()
            new_close = NewClose.CLOSE if new_close_key == "close" else NewClose.NEW
            direction_key = str(payload.get("trigger_direction", "gte")).lower()
            trigger_direction = (
                TriggerDirection.GTE if direction_key == "gte" else TriggerDirection.LTE
            )

            price = str(payload.get("price") or "")
            # 2026-08-21 實測發現：MIT 智慧單一定要給 bstrDealPrice，不給會被拒單
            # （SK_ERROR_STRATEGY_ORDER_MUST_GIVE_DEAL_PRICE）。官方文件沒解釋這個
            # 欄位的確切用途，實測用跟 price 一樣的值送單成功過，沒給預設值就
            # 用 price 頂著，呼叫端不用特地知道這個雷。
            deal_price = str(payload.get("deal_price") or price)
            result = broker.send_future_mit_order(
                account=account,
                stock_no=str(payload.get("stock_no") or "TMF"),
                settlement_month=str(payload.get("settlement_month") or ""),
                side=side,
                qty=int(payload.get("qty") or 1),
                trigger_price=str(payload.get("trigger_price") or ""),
                trigger_direction=trigger_direction,
                price=price,
                deal_price=deal_price,
                new_close=new_close,
                trade_type=int(payload.get("trade_type", SmartOrderTradeType.ROD)),
                order_price_type=int(payload.get("order_price_type", SmartOrderPriceType.LIMIT)),
            )
            broker.pump_events(2.0)
            broker.refresh_live_snapshot(account)
            return {
                "order_result": {
                    "success": result.success,
                    "code": result.code,
                    "message": result.message,
                    "raw": result.raw,
                },
                "state": broker.get_live_state(),
            }

    def place_oco_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        送出真正的券商端 OCO（二擇一）智慧單——一次委託同時掛停損＋停利兩支腿
        （見 broker/capital_futures.py 的 send_future_oco_order 說明）。
        2026-08-27 起取代分開送 STP+MIT 兩張獨立平倉智慧單的做法——分開送會
        被券商拒絕第二張（[999] 勾選平倉而留倉部位不足，兩張各自向券商聲請
        同一口的平倉額度）。
        """
        _require_trading_enabled()
        return self._run_com(lambda: self._place_oco_order_impl(payload))

    def _place_oco_order_impl(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            raw_account = payload.get("account") or broker.live.active_account
            if not raw_account:
                raise ValueError("請指定交易帳號")
            account = str(raw_account)

            side_key = str(payload.get("side", "sell")).lower()
            side = BuySell.BUY if side_key == "buy" else BuySell.SELL
            side2_key = str(payload.get("side2", "sell")).lower()
            side2 = BuySell.BUY if side2_key == "buy" else BuySell.SELL
            new_close_key = str(payload.get("new_close", "close")).lower()
            new_close = NewClose.CLOSE if new_close_key == "close" else NewClose.NEW

            result = broker.send_future_oco_order(
                account=account,
                stock_no=str(payload.get("stock_no") or "TMF"),
                settlement_month=str(payload.get("settlement_month") or ""),
                qty=int(payload.get("qty") or 1),
                side=side,
                trigger_price=str(payload.get("trigger_price") or ""),
                price=str(payload.get("price") or "P"),
                side2=side2,
                trigger_price2=str(payload.get("trigger_price2") or ""),
                price2=str(payload.get("price2") or "P"),
                new_close=new_close,
                trade_type=int(payload.get("trade_type", SmartOrderTradeType.ROD)),
                order_price_type=int(payload.get("order_price_type", SmartOrderPriceType.RANGE_MARKET)),
            )
            broker.pump_events(2.0)
            broker.refresh_live_snapshot(account)
            return {
                "order_result": {
                    "success": result.success,
                    "code": result.code,
                    "message": result.message,
                    "raw": result.raw,
                },
                "state": broker.get_live_state(),
            }

    def cancel_stop_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_com(lambda: self._cancel_stop_order_impl(payload))

    def _cancel_stop_order_impl(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            raw_account = payload.get("account") or broker.live.active_account
            if not raw_account:
                raise ValueError("請指定交易帳號")
            account = str(raw_account)

            result = broker.cancel_stp_order(
                account,
                smart_key=str(payload.get("smart_key") or ""),
                seq_no=str(payload.get("seq_no") or ""),
                order_no=str(payload.get("order_no") or ""),
                trade_kind=int(payload.get("trade_kind", SmartOrderKind.STP)),
            )
            broker.pump_events(2.0)
            broker.refresh_live_snapshot(account, force=True)
            return {
                "cancel_result": {
                    "success": result.success,
                    "code": result.code,
                    "message": result.message,
                    "raw": result.raw,
                },
                "state": broker.get_live_state(),
            }

    def query_stop_loss_report(self, account: str | None = None) -> dict[str, Any]:
        return self._run_com(lambda: self._query_stop_loss_report_impl(account))

    def _query_stop_loss_report_impl(self, account: str | None) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            acc = account or broker.live.active_account
            if not acc:
                raise ValueError("請指定交易帳號")
            broker.get_stop_loss_report(acc)
            broker.pump_events(2.0)
            return {"stop_loss_raw": broker.live.stop_loss_raw}

    def cancel_order(self, seq_no: str, account: str | None = None) -> dict[str, Any]:
        return self._run_com(lambda: self._cancel_order_impl(seq_no, account))

    def _cancel_order_impl(self, seq_no: str, account: str | None) -> dict[str, Any]:
        with self._lock:
            if not self._broker:
                raise RuntimeError("尚未連線")
            broker = self._broker
            acc = account or broker.live.active_account
            if not acc:
                raise ValueError("請指定交易帳號")
            result = broker.cancel_order_by_seqno(acc, seq_no)
            broker.pump_events(2.0)
            broker.refresh_live_snapshot(acc)
            return {
                "cancel_result": {
                    "success": result.success,
                    "code": result.code,
                    "message": result.message,
                    "raw": result.raw,
                },
                "state": broker.get_live_state(),
            }