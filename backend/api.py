from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .backtest_engine import run_backtest, run_direction_limited_backtest
from .config import (
    ContractSpec,
    CostConfig,
    DEFAULT_COST,
    DEFAULT_STRATEGY,
    app_base_dir,
    cost_config_for_contract,
)
from .indicators import add_moving_averages, resample_to_60min
from .live_service import (
    auto_connect_on_startup,
    cancel_order as live_cancel_order,
    close_position as live_close_position,
    connection_meta,
    flatten_all_positions,
    get_account,
    get_fills,
    get_klines,
    get_orders,
    get_positions,
    get_ticker,
    place_order,
    place_stop_order,
    place_mit_order,
    cancel_stop_order,
    query_stop_loss_report,
    poll_loop,
    set_product,
    trading_params_for_sidebar,
    ws_clients,
)
from .load_taifex_tick import load_tmfr1_range
from .strategy_service import (
    STRATEGY_DEFS,
    klines_with_signals,
    reconcile_after_manual_close,
    start_strategy,
    stop_strategy,
    strategy_list,
    strategy_status,
    strategy_tick,
    trade_log,
)
from .signals import generate_breakout_signals, generate_pullback_signals
from .supabase_auth import InvalidSupabaseToken, verify_supabase_jwt
from .timeutil import to_unix_seconds
from .trading_service import (
    TradingService,
    set_trading_disabled,
    trading_capability_report,
    trading_disabled,
    trading_params_schema,
)

logger = logging.getLogger(__name__)

BASE_DIR = app_base_dir(__file__)
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TMF": ContractSpec(name="TMF", point_value=10.0, tick_size=1.0),
}
VALID_STRATEGIES = {"breakout", "pullback"}


async def strategy_loop() -> None:
    """每 60 秒檢查回測/回彈訊號（60 分 K）。"""
    while True:
        try:
            await asyncio.to_thread(strategy_tick)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategy_loop: %s", exc)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """部署啟動時於背景自動連線群益 API，不阻塞 uvicorn 啟動與網頁服務。"""
    asyncio.create_task(asyncio.to_thread(auto_connect_on_startup))
    poll_task = asyncio.create_task(poll_loop())
    strat_task = asyncio.create_task(strategy_loop())
    yield
    poll_task.cancel()
    strat_task.cancel()
    try:
        TradingService.get().disconnect()
    except Exception:  # noqa: BLE001
        pass


# 2026-08-21：原本的 ACCESS_KEY 共用金鑰換成 Supabase 登入（見
# backend/supabase_auth.py）——不開放註冊，帳號由使用者在 Supabase 後台手動
# 建立，前端只做登入，體驗比網址帶一串金鑰好，也不會有金鑰被截圖/瀏覽紀錄外流
# 的風險。API／WebSocket 一律要求有效的 Supabase JWT；靜態前端檔案（含登入頁
# 本身）不受此限——不然登入頁自己都載入不了。
_PUBLIC_PATHS = {"/api/health"}

app = FastAPI(title="台指期量化 Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_supabase_auth(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or not path.startswith("/api/"):
        # 非 /api/ 路徑（含 /ws/ 由各自的 WebSocket handler 自行驗證、以及所有
        # 靜態前端檔案）不在這個 middleware 的管轄範圍內。
        return await call_next(request)

    token = request.query_params.get("token")
    auth_header = request.headers.get("authorization", "")
    if not token and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    try:
        claims = verify_supabase_jwt(token or "")
    except InvalidSupabaseToken:
        return JSONResponse({"detail": "請先登入"}, status_code=401)

    # 2026-08-26：多帳號規劃第一步——把登入者身分掛到 request.state，讓後面
    # 個人化/權限判斷的端點（/api/my-strategy-configs、/api/admin/* 等）知道
    # 「這是誰、是不是管理員」，不用每個 handler 自己重新解一次 JWT（見
    # docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。
    request.state.user_id = claims.get("sub", "")
    request.state.user_email = claims.get("email", "")
    return await call_next(request)


class BacktestRequest(BaseModel):
    dataset: str
    strategy: str
    contract: str = "TX"
    slippage_points: float = 0.0
    commission_per_side: float | None = None
    tax_rate: float | None = None


class LiveStrategiesBacktestRequest(BaseModel):
    start_date: str
    end_date: str
    contract: str = "TMF"
    slippage_points: float = 0.0
    commission_per_side: float | None = None
    tax_rate: float | None = None
    stop_loss_points: float = 100.0
    take_profit_points: float = 300.0
    use_stop_take: bool = True
    initial_capital_ntd: float = 100_000.0
    max_loss_ntd: float = 10_000.0
    max_loss_pct: float = 0.10
    use_risk_stop: bool = True


class TradingConnectRequest(BaseModel):
    environment: int | None = None
    user_id: str | None = None
    password: str | None = None
    product_code: str | None = None
    account: str | None = None


class TradingOrderRequest(BaseModel):
    account: str | None = None
    product_code: str = "TM2608"
    side: str = "buy"
    qty: int = 1
    price: str = "0"
    trade_type: int = 2
    new_close: int = 2
    day_trade: int = 0


class TradingSubscribeRequest(BaseModel):
    product_code: str


class StrategyStartRequest(BaseModel):
    strategy_id: str
    product_code: str = "TM2608"
    qty: int | None = None


class StrategyStopRequest(BaseModel):
    strategy_id: str


class ClosePositionRequest(BaseModel):
    product_code: str
    direction_key: str
    qty: int
    account: str | None = None


class TradingSafetyRequest(BaseModel):
    disabled: bool
    confirm: str


class StopOrderRequest(BaseModel):
    stock_no: str = "TMF"
    settlement_month: str
    side: str  # "buy" | "sell"
    qty: int = 1
    trigger_price: str
    price: str = "P"
    order_price_type: int = 3  # 2=限價 3=範圍市價
    trade_type: int = 0  # 智慧單專用編碼：ROD=0 IOC=3 FOK=4（見 SmartOrderTradeType）
    new_close: str = "close"  # "close" | "new"
    account: str | None = None


class CancelStopOrderRequest(BaseModel):
    smart_key: str
    seq_no: str = ""
    order_no: str = ""
    trade_kind: int = 5  # 5=STP 8=MIT（見 SmartOrderKind）
    account: str | None = None


class MitOrderRequest(BaseModel):
    stock_no: str = "TMF"
    settlement_month: str
    side: str  # "buy" | "sell"
    qty: int = 1
    trigger_price: str
    trigger_direction: str = "gte"  # "gte" | "lte"
    price: str = ""
    deal_price: str = ""
    order_price_type: int = 2  # 2=限價 3=範圍市價
    trade_type: int = 0
    new_close: str = "close"
    account: str | None = None


def _json_number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None

    number = float(value)
    if not math.isfinite(number):
        return None
    return number


# 2026-08-24：這個轉換邏輯抽成 backend/timeutil.py 共用（kline_engine.py、
# capital_parse.py 都要用同一套，之前各自實作各自漏改，才踩到同一個 bug 兩次）。
_to_unix_seconds = to_unix_seconds


def _load_csv_with_datetime(csv_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(csv_path)
    if "datetime" not in frame.columns:
        raise ValueError("CSV must contain a 'datetime' column.")

    frame["datetime"] = pd.to_datetime(frame["datetime"])
    frame = frame.sort_values("datetime").reset_index(drop=True)
    return frame


def _median_bar_gap_minutes(frame: pd.DataFrame) -> float:
    if len(frame) < 2:
        return 60.0

    gaps = frame["datetime"].diff().dropna()
    if gaps.empty:
        return 60.0

    return float(gaps.dt.total_seconds().median() / 60.0)


def _prepare_market_data(csv_path: Path) -> pd.DataFrame:
    raw = _load_csv_with_datetime(csv_path)
    if raw.empty:
        return raw

    if _median_bar_gap_minutes(raw) < 60.0:
        bars = resample_to_60min(raw)
    else:
        bars = raw.set_index("datetime")

    if bars.empty:
        return bars

    bars.index = pd.to_datetime(bars.index)
    bars = bars.sort_index()
    return add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )


def _resolve_dataset_path(dataset_id: str) -> Path:
    csv_path = DATA_DIR / f"{dataset_id}.csv"
    if not csv_path.is_file():
        raise HTTPException(status_code=400, detail=f"Unknown dataset: {dataset_id}")
    return csv_path


def _resolve_strategy(strategy: str) -> str:
    if strategy not in VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail="strategy must be 'breakout' or 'pullback'.")
    return strategy


def _resolve_contract(contract_name: str) -> ContractSpec:
    contract = CONTRACT_SPECS.get(contract_name)
    if contract is None:
        raise HTTPException(status_code=400, detail="contract must be 'TX', 'MTX', or 'TMF'.")
    return contract


def _build_cost_config(request: BacktestRequest) -> CostConfig:
    # 預設手續費採群益期貨公告費率區間的「最大值」(保守估計)，依契約別 (TX/MTX/TMF) 不同。
    contract_default = cost_config_for_contract(request.contract)
    return CostConfig(
        tax_rate=DEFAULT_COST.tax_rate if request.tax_rate is None else float(request.tax_rate),
        commission_per_side=(
            contract_default.commission_per_side
            if request.commission_per_side is None
            else float(request.commission_per_side)
        ),
        slippage_points=float(request.slippage_points),
    )


def _generate_signaled_frame(frame: pd.DataFrame, strategy: str) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(frame)
    return generate_pullback_signals(frame)


def _serialize_klines(frame: pd.DataFrame) -> list[dict[str, object]]:
    return [
        {
            "time": _to_unix_seconds(index),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row["volume"]),
            "ma_fast": _json_number(row.get("ma_fast")),
            "ma_mid": _json_number(row.get("ma_mid")),
            "ma_slow": _json_number(row.get("ma_slow")),
        }
        for index, row in frame.iterrows()
    ]


def _serialize_signals(frame: pd.DataFrame) -> list[dict[str, object]]:
    signal_rows = frame.loc[frame["signal"].notna(), ["signal", "signal_color"]]
    return [
        {
            "time": _to_unix_seconds(index),
            "signal": str(row["signal"]),
            "signal_color": str(row["signal_color"]),
        }
        for index, row in signal_rows.iterrows()
    ]


def _serialize_trades(trades: list) -> list[dict[str, object]]:
    return [
        {
            "entry_time": _to_unix_seconds(trade.entry_time),
            "entry_price": float(trade.entry_price),
            "direction": trade.direction,
            "exit_time": _to_unix_seconds(trade.exit_time),
            "exit_price": float(trade.exit_price),
            "exit_reason": trade.exit_reason,
            "stop_loss_price": _json_number(trade.stop_loss_price),
            "take_profit_price": _json_number(trade.take_profit_price),
            "gross_pnl_points": float(trade.gross_pnl_points),
            "gross_pnl_ntd": float(trade.gross_pnl_ntd),
            "total_cost_ntd": float(trade.total_cost_ntd),
            "net_pnl_ntd": float(trade.net_pnl_ntd),
        }
        for trade in trades
    ]


def _build_equity_curve(first_bar_time: pd.Timestamp, trades: list) -> list[dict[str, object]]:
    equity_curve: list[dict[str, object]] = [
        {"time": _to_unix_seconds(first_bar_time), "equity": 0.0}
    ]
    cumulative = 0.0
    for trade in trades:
        cumulative += float(trade.net_pnl_ntd)
        equity_curve.append(
            {
                "time": _to_unix_seconds(trade.exit_time),
                "equity": float(cumulative),
            }
        )
    return equity_curve


def _serialize_summary(summary: dict) -> dict[str, float | int | None]:
    return {
        "total_trades": int(summary.get("total_trades", 0)),
        "win_rate": _json_number(summary.get("win_rate")),
        "total_net_pnl_ntd": _json_number(summary.get("total_net_pnl_ntd")),
        "max_drawdown_ntd": _json_number(summary.get("max_drawdown_ntd")),
        "profit_factor": _json_number(summary.get("profit_factor")),
        "avg_win_ntd": _json_number(summary.get("avg_win_ntd")),
        "avg_loss_ntd": _json_number(summary.get("avg_loss_ntd")),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _is_admin(request: Request) -> bool:
    """現階段只用一個環境變數做管理員清單（逗號分隔的信箱），不建角色資料表
    ——只有你一個人是管理員，沒必要為此多一張表。之後真的有多個管理員/更細
    的權限需求，再另外規劃。"""
    email = getattr(request.state, "user_email", "").strip().lower()
    if not email:
        return False
    admin_emails = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
    return email in admin_emails


@app.get("/api/me")
def api_me(request: Request) -> dict[str, object]:
    return {
        "user_id": getattr(request.state, "user_id", ""),
        "email": getattr(request.state, "user_email", ""),
        "is_admin": _is_admin(request),
    }


# ── 實盤 Dashboard（對齊賽博纏論 API 命名）────────────────────────────────

@app.get("/api/connection")
def live_connection() -> dict[str, object]:
    return connection_meta()


@app.get("/api/account")
def live_account() -> dict[str, object]:
    return get_account()


@app.get("/api/positions")
def live_positions() -> list[dict[str, object]]:
    return get_positions()


@app.get("/api/orders")
def live_orders() -> list[dict[str, object]]:
    return get_orders()


@app.get("/api/fills")
def live_fills() -> list[dict[str, object]]:
    return get_fills()


@app.get("/api/ticker")
def live_ticker(product: str = "TM2608") -> dict[str, object]:
    return get_ticker(product)


@app.get("/api/klines")
def live_klines(product: str = "TM2608", limit: int = 500) -> list[dict[str, object]]:
    from .kline_engine import DEFAULT_KLINE_LIMIT

    effective = limit if limit > 0 else DEFAULT_KLINE_LIMIT
    return get_klines(product, limit=min(effective, 2000))


@app.get("/api/klines/signals")
def live_klines_signals(product: str = "TM2608", limit: int = 500) -> list[dict[str, object]]:
    """K 棒 + MA(5/20/60) + 突破/回測策略訊號，給圖表疊圖用。"""
    return klines_with_signals(product, limit=min(limit, 2000))


@app.get("/api/strategy/trades")
def live_strategy_trades(strategy_id: str | None = None) -> list[dict[str, object]]:
    """各策略實際下單進出場紀錄（不是原始指標交叉訊號），給圖表畫真正成交箭頭用。"""
    return trade_log(strategy_id)


@app.post("/api/product")
def live_set_product(product: str) -> dict[str, object]:
    return set_product(product)


@app.get("/api/live/params")
def live_params() -> dict[str, object]:
    return trading_params_for_sidebar()


@app.get("/api/strategy/list")
def live_strategy_list() -> list[dict[str, object]]:
    return strategy_list()


@app.get("/api/strategy/status")
def live_strategy_status() -> dict[str, object]:
    return strategy_status()


@app.post("/api/strategy/start")
def live_strategy_start(request: StrategyStartRequest) -> dict[str, object]:
    try:
        return start_strategy(request.strategy_id, request.product_code, qty=request.qty)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/strategy/stop")
def live_strategy_stop(request: StrategyStopRequest) -> dict[str, object]:
    return stop_strategy(request.strategy_id)


@app.post("/api/positions/flatten")
def live_flatten_positions() -> dict[str, object]:
    try:
        result = flatten_all_positions()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 一鍵平倉是真的送出真實委託單，但策略自己的持倉記帳不會自動知道，
    # 平倉後一定要跑一次核對，兜不起來就自動停止相關策略、請人工核對
    # （見 strategy_service.reconcile_after_manual_close 註解）。
    result["reconciled_strategies"] = reconcile_after_manual_close()
    return result


@app.post("/api/positions/close")
def api_close_position(request: ClosePositionRequest) -> dict[str, object]:
    try:
        result = live_close_position(request.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["reconciled_strategies"] = reconcile_after_manual_close(request.product_code)
    return result


@app.delete("/api/order/{seq_no}")
def api_cancel_order(seq_no: str, account: str | None = None) -> dict[str, object]:
    try:
        return live_cancel_order(seq_no, account=account)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/order/stop")
def api_place_stop_order(request: StopOrderRequest) -> dict[str, object]:
    """
    送出真正的券商端 STP 停損智慧單（不是 strategy_service 目前用的軟停損）。
    帳戶需已簽署「期貨智慧單風險預告書」，見 backend/broker/capital_futures.py
    的 send_future_stp_order 說明。
    """
    try:
        return place_stop_order(request.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/order/stop/cancel")
def api_cancel_stop_order(request: CancelStopOrderRequest) -> dict[str, object]:
    try:
        return cancel_stop_order(request.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/order/mit")
def api_place_mit_order(request: MitOrderRequest) -> dict[str, object]:
    """
    送出真正的券商端 MIT 觸價智慧單（用來做真實停利單）。跟 STP 共用
    /api/order/stop/cancel 撤單（傳 trade_kind=8）。見 send_future_mit_order 說明。
    """
    try:
        return place_mit_order(request.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/order/stop/report")
def api_query_stop_loss_report(account: str | None = None) -> dict[str, object]:
    try:
        return query_stop_loss_report(account)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/ws/{product_code}")
async def live_ws(websocket: WebSocket, product_code: str):
    # HTTP middleware（require_supabase_auth）不會套用到 WebSocket，這裡要自己
    # 驗證一次，不然報價 WS 會完全繞過登入保護。瀏覽器原生 WebSocket API 不能
    # 自訂 header，token 只能用 query string 帶。
    token = websocket.query_params.get("token") or ""
    try:
        verify_supabase_jwt(token)
    except InvalidSupabaseToken:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    ws_clients.setdefault(product_code, set()).add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients = ws_clients.get(product_code)
        if clients is not None:
            clients.discard(websocket)
            if not clients:
                ws_clients.pop(product_code, None)


@app.get("/api/datasets")
def list_datasets() -> list[dict[str, object]]:
    datasets: list[dict[str, object]] = []
    if not DATA_DIR.is_dir():
        return datasets

    for csv_path in sorted(DATA_DIR.glob("*.csv")):
        # data/ 目錄裡也會放分析報告類 CSV（例如 strategy_failure_trade_details.csv，
        # 欄位是 entry_time/exit_time 而非 datetime），不是可拿來回測的K線資料集，
        # 跳過即可，不該讓一個不相干的檔案讓整個資料集清單 500。
        try:
            frame = _load_csv_with_datetime(csv_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("略過非資料集 CSV %s: %s", csv_path.name, exc)
            continue
        datasets.append(
            {
                "id": csv_path.stem,
                "filename": csv_path.name,
                "rows": int(len(frame)),
                "start": pd.Timestamp(frame["datetime"].iloc[0]).isoformat() if not frame.empty else None,
                "end": pd.Timestamp(frame["datetime"].iloc[-1]).isoformat() if not frame.empty else None,
            }
        )

    return datasets


@app.get("/api/trading/params")
def trading_params() -> dict[str, object]:
    return trading_params_schema()


@app.get("/api/trading/capability")
def trading_capability() -> dict[str, object]:
    return trading_capability_report()


@app.post("/api/trading/connect")
def trading_connect(request: TradingConnectRequest) -> dict[str, object]:
    try:
        return TradingService.get().connect(
            environment=request.environment,
            user_id=request.user_id,
            password=request.password,
            product_code=request.product_code,
            account=request.account,
        )
    except Exception as exc:  # noqa: BLE001 - 回傳給 dashboard 顯示
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/trading/disconnect")
def trading_disconnect() -> dict[str, object]:
    return TradingService.get().disconnect()


@app.get("/api/trading/status")
def trading_status() -> dict[str, object]:
    return TradingService.get().status()


@app.get("/api/trading/safety")
def trading_safety() -> dict[str, object]:
    return {"trading_disabled": trading_disabled()}


@app.post("/api/trading/safety")
def trading_safety_set(request: TradingSafetyRequest) -> dict[str, object]:
    # 從「禁止下單」切到「允許下單」時要求輸入確認字串，避免誤觸就打開真實下單。
    if not request.disabled and request.confirm != "ENABLE":
        raise HTTPException(status_code=400, detail="confirm 必須是 'ENABLE' 才能開啟真實下單")
    disabled = set_trading_disabled(request.disabled)
    return {"trading_disabled": disabled}


@app.post("/api/trading/refresh")
def trading_refresh() -> dict[str, object]:
    try:
        return TradingService.get().refresh()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/trading/subscribe")
def trading_subscribe(request: TradingSubscribeRequest) -> dict[str, object]:
    try:
        return TradingService.get().subscribe(request.product_code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/trading/order")
def trading_place_order(request: TradingOrderRequest) -> dict[str, object]:
    try:
        return TradingService.get().place_order(request.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/order")
def live_place_order(request: TradingOrderRequest) -> dict[str, object]:
    try:
        return place_order(request.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/backtest")
def backtest(request: BacktestRequest) -> dict[str, object]:
    csv_path = _resolve_dataset_path(request.dataset)
    strategy = _resolve_strategy(request.strategy)
    contract = _resolve_contract(request.contract)
    cost = _build_cost_config(request)

    try:
        enriched = _prepare_market_data(csv_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if enriched.empty:
        raise HTTPException(status_code=400, detail="No data available after processing.")

    signaled = _generate_signaled_frame(enriched, strategy)
    if signaled.empty:
        raise HTTPException(status_code=400, detail="No data available after processing.")

    trades, summary = run_backtest(
        signaled,
        strategy=strategy,
        contract=contract,
        cost=cost,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=float(request.slippage_points),
    )

    return {
        "klines": _serialize_klines(signaled),
        "signals": _serialize_signals(signaled),
        "trades": _serialize_trades(trades),
        "equity_curve": _build_equity_curve(pd.Timestamp(signaled.index[0]), trades),
        "summary": _serialize_summary(summary),
    }


@app.post("/api/backtest/live_strategies")
def backtest_live_strategies(request: LiveStrategiesBacktestRequest) -> dict[str, object]:
    """
    用真實 TMFR1 逐筆資料回測「跟實盤一致」的 4 個獨立方向策略（見
    backend/strategy_service.py 的 STRATEGY_DEFS）：breakout_long/short、
    pullback_long/short，方向限定不反手、預設無固定 SL/TP 只靠訊號出場，
    可選比照實盤持倉浮動損益停損。資料來源固定為本機
    data/raw_tick/TMFR1/*.parquet（見 load_tmfr1_range）。
    """
    try:
        ticks = load_tmfr1_range(request.start_date, request.end_date)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bars = resample_to_60min(ticks)
    if bars.empty:
        raise HTTPException(status_code=400, detail="該區間內沒有可用的K棒資料。")

    enriched = add_moving_averages(
        bars, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched.index = pd.to_datetime(enriched.index)
    enriched = enriched.sort_index()

    contract = _resolve_contract(request.contract)
    cost = _build_cost_config(request)

    signaled_by_strategy = {
        "breakout": generate_breakout_signals(enriched),
        "pullback": generate_pullback_signals(enriched),
    }

    strategies_out: dict[str, object] = {}
    for strategy_id, defs in STRATEGY_DEFS.items():
        signaled = signaled_by_strategy[defs["strategy"]]
        trades, summary = run_direction_limited_backtest(
            signaled,
            strategy=defs["strategy"],  # type: ignore[arg-type]
            direction_limit=defs["direction_limit"],  # type: ignore[arg-type]
            contract=contract,
            cost=cost,
            slippage_points=float(request.slippage_points),
            stop_loss_points=float(request.stop_loss_points),
            take_profit_points=float(request.take_profit_points),
            use_stop_take=request.use_stop_take,
            initial_capital_ntd=float(request.initial_capital_ntd),
            max_loss_ntd=float(request.max_loss_ntd),
            max_loss_pct=float(request.max_loss_pct),
            use_risk_stop=request.use_risk_stop,
        )
        strategies_out[strategy_id] = {
            "label": defs["label"],
            "trades": _serialize_trades(trades),
            "summary": _serialize_summary(summary),
        }

    return {
        "klines": _serialize_klines(enriched),
        "breakout_signals": _serialize_signals(signaled_by_strategy["breakout"]),
        "pullback_signals": _serialize_signals(signaled_by_strategy["pullback"]),
        "strategies": strategies_out,
        "data_span": [str(enriched.index[0]), str(enriched.index[-1])],
    }


if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
