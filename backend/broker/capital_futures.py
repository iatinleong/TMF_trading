"""
backend/broker/capital_futures.py

群益證券（Capital Futures）SKCOM API 期貨下單整合模組。

將 CapitalAPI_2.13.58_PythonExample 內的 COM 元件（SKCOM.dll）包裝成一個
可在無 GUI 環境下使用的 Python 類別 `CapitalFuturesBroker`，提供登入、
台指期下單（大台 TX / 小台 MTX / 微台 TMF）、刪單、改價、帳務查詢、
即時報價訂閱等功能，方便與現有回測/策略系統整合為實盤（含測試環境）交易。

⚠️ 僅支援 Windows，且必須先完成以下環境設置（僅需一次）：
  1. 使用本專案內建的 SKCOM.dll（優先 PythonExampleV2 Quote 目錄，與官方 GUI 相同）：
     CapitalAPI_2.13.58_PythonExample\\...\\PythonExampleV2\\Quote\\Quote\\SKCOM.dll
     備援：CapitalAPI_2.13.58_PythonExample\\...\\元件\\x64\\SKCOM.dll
  2. 若 DLL 是從壓縮檔解壓縮而來，Windows 可能會標記為「網路來源」而阻擋載入，
     需先以 PowerShell 執行 `Get-ChildItem <元件\\x64目錄> | Unblock-File` 解除封鎖。
  3. 以系統管理員身分執行 regsvr32 SKCOM.dll（或該目錄下的 install.bat）完成 COM 元件註冊。
  4. 若系統缺少 mfc100.dll / msvcr100.dll，需安裝
     Microsoft Visual C++ 2010 SP1 Redistributable (x64)（官方下載連結需自行搜尋安裝）。
  5. pip install comtypes python-dotenv

環境變數（建議寫在專案根目錄 .env，勿提交版控）：
  CAPITAL_USER_ID       群益期貨帳號登入 ID
  CAPITAL_PASSWORD      群益期貨密碼
  CAPITAL_ENVIRONMENT   0=正式環境 1=正式環境SGX 2=測試環境 3=測試環境SGX（預設 0）
  CAPITAL_DLL_PATH      SKCOM.dll 完整路徑（預設自動偵測本專案內建路徑，通常不需設定）
  CAPITAL_LOG_PATH      SKCOM log 目錄（預設 <專案>/data/capital_logs）

已知的 comtypes 相容性問題（Python 3.13 + 繁體中文 Windows）：
  comtypes 產生 COM wrapper 原始碼時會寫入 `# -*- coding: mbcs -*-`，但實際寫檔採用
  locale 預設編碼，兩者不一致會導致 `SyntaxError: 'mbcs' codec can't decode bytes...`。
  若遇到此錯誤，需修改本機安裝的
  comtypes/client/_generate.py（寫檔加上 encoding="utf-8"）與
  comtypes/tools/codegenerator/codegenerator.py（coding cookie 改為 utf-8）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常數：與群益 API FUTUREORDER 欄位對應的列舉值（依官方 PythonExampleV2 範例）
# ---------------------------------------------------------------------------
class TradeType:
    ROD = 0  # 掛單至收盤前有效
    IOC = 1  # 立即成交否則取消
    FOK = 2  # 全部成交否則取消


class BuySell:
    BUY = 0
    SELL = 1


class NewClose:
    NEW = 0    # 新倉
    CLOSE = 1  # 平倉
    AUTO = 2   # 自動


class Reserved:
    INTRADAY = 0  # 盤中 (T盤及T+1盤)
    RESERVED = 1  # T盤預約


class SmartOrderTradeType:
    """
    期貨智慧單（STP/MST等，SendFutureSTPOrderV1 等函式）專用的委託方式代碼。
    !!注意跟上面 TradeType 的數值定義不一樣!! 官方文件《8.下單-國內期選智慧單》
    範例程式碼裡智慧單用的是 ROD=0/IOC=3/FOK=4，跟一般委託（SendFutureOrderCLR
    用的 TradeType，ROD=0/IOC=1/FOK=2）是兩套不同的編碼，同一個 FUTUREORDER
    struct 依呼叫的函式不同，這個欄位的數值意義也不同。
    """

    ROD = 0
    IOC = 3
    FOK = 4


class SmartOrderPriceType:
    """智慧單 nOrderPriceType：2=限價（需另填 bstrPrice）、3=範圍市價。"""

    LIMIT = 2
    RANGE_MARKET = 3


class TriggerDirection:
    """FUTUREORDER.nTriggerDirection（MIT 觸價單用）：現價相對觸發價的方向。"""

    GTE = 1  # 大於等於：現價漲到/超過觸發價才觸發（多單停利用）
    LTE = 2  # 小於等於：現價跌到/低於觸發價才觸發（空單停利用）


class SmartOrderKind:
    """CANCELSTRATEGYORDER.nTradeKind：智慧單種類代碼（官方文件刪單範例列舉）。"""

    OCO = 3
    STP = 5
    MIT = 8
    MST = 9
    AB = 10


class SmartOrderMarket:
    """CANCELSTRATEGYORDER.nMarket：市場別。"""

    DOMESTIC_SECURITIES = 1
    DOMESTIC_FUTURES = 2
    FOREIGN_SECURITIES = 3
    FOREIGN_FUTURES = 4


class Environment:
    PRODUCTION = 0      # 正式環境
    PRODUCTION_SGX = 1  # 正式環境 SGX
    TEST = 2            # 測試環境
    TEST_SGX = 3         # 測試環境 SGX


# 本專案內建的 SKCOM.dll 相對路徑（優先 V2 Quote 目錄，與官方 GUI 一致）
_DLL_CANDIDATE_RELATIVE_PATHS = (
    "CapitalAPI_2.13.58_PythonExample"
    "\\CapitalAPI_2.13.58_PythonExample\\PythonExampleV2\\Quote\\Quote\\SKCOM.dll",
    "CapitalAPI_2.13.58_PythonExample"
    "\\CapitalAPI_2.13.58_PythonExample\\元件\\x64\\SKCOM.dll",
)


def _decode_capital_text(value: Any) -> str:
    """群益 COM BSTR：繁中 Windows 多數已是 Unicode，勿再 latin1→cp950 重解。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        raw = value
    else:
        text = str(value)
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
            return text.strip()
        raw = text.encode("latin1", errors="ignore")
    for encoding in ("cp950", "big5", "utf-8"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return str(value).strip()


def _format_capital_kline_date(dt: datetime) -> str:
    """群益 SKQuoteLib_RequestKLineAMByDate 日期格式：YYYYMMDD。"""
    return dt.strftime("%Y%m%d")


def _days_for_kline_bars(bar_limit: int, *, bar_minutes: int = 60) -> int:
    """依所需 K 棒數估算查詢日數（60 分 K 約每日 19 根，含週末緩衝）。"""
    if bar_limit <= 0:
        return 45
    bars_per_day = max(1, (23 * 60) // bar_minutes)  # 台指期全盤約 23 小時
    trading_days = max(1, (bar_limit + bars_per_day - 1) // bars_per_day)
    calendar_days = int(trading_days * 7 / 5) + 14
    return max(45, min(calendar_days, 365))


def _capital_tick_timestamp(n_date: int, n_timehms: int) -> pd.Timestamp:
    date_s = str(int(n_date)).zfill(8)
    time_s = str(int(n_timehms)).zfill(6)
    return pd.Timestamp(
        f"{date_s[:4]}-{date_s[4:6]}-{date_s[6:8]} "
        f"{time_s[:2]}:{time_s[2:4]}:{time_s[4:6]}"
    )


def _find_project_root() -> Path:
    """由 SKCOM.dll 位置反推專案根目錄。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        for rel in _DLL_CANDIDATE_RELATIVE_PATHS:
            if (parent / rel).exists():
                return parent
    return here.parents[2]


def _find_default_dll_path() -> Path:
    """尋找內建 SKCOM.dll；優先 PythonExampleV2 Quote 目錄（與官方 GUI 相同）。"""
    root = _find_project_root()
    for rel in _DLL_CANDIDATE_RELATIVE_PATHS:
        candidate = root / rel
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "找不到 SKCOM.dll，請設定環境變數 CAPITAL_DLL_PATH 指向正確路徑，"
        "或確認 CapitalAPI_2.13.58_PythonExample 資料夾未被搬移。"
    )


def _ensure_sta_apartment() -> None:
    """COM 事件須在 STA 執行緒派送（與 tkinter mainloop 相同）。"""
    import pythoncom

    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    except pythoncom.com_error:
        # 執行緒已初始化 COM apartment 時略過
        pass


def _find_default_log_path() -> Path:
    """SKCOM Center/Order/Reply log 預設輸出目錄。"""
    env_path = os.getenv("CAPITAL_LOG_PATH")
    if env_path:
        return Path(env_path)
    return _find_project_root() / "data" / "capital_logs"


def _order_audit_log_path() -> Path:
    """
    我們自己的下單稽核 log（持久化、只會 append，不會被 SKCOM 元件重置）。
    2026-07-27 發現 SKCOM 自己的 Order.log 每次 SKOrderLib_Initialize 都會被
    重置一部分，且我們原本完全沒有把每次下單呼叫的原始 code/message 存下來
    （只寫進記憶體 state.last_action，程式一重啟就沒了）——導致一次「broker
    端明明成功、我們的程式碼卻判定失敗」的事故完全查不出根因。這個檔案就是
    為了下次再發生類似狀況時，能拿到當時 SendFutureOrderCLR 真正回傳的
    code/raw 內容。
    """
    return _find_default_log_path().parent / "order_audit.log"


def _append_order_audit(
    *,
    account: str,
    product_code: str,
    side: int,
    qty: int,
    price: str,
    trade_type: int,
    new_close: int,
    code: int,
    message: str,
    raw: str,
) -> None:
    try:
        path = _order_audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = (
            f"{ts}\taccount={account}\tproduct={product_code}\tside={side}\t"
            f"qty={qty}\tprice={price}\ttrade_type={trade_type}\tnew_close={new_close}\t"
            f"code={code}\tsuccess={code == 0}\tmessage={message}\traw={raw}\n"
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - 稽核 log 寫入失敗不該影響下單主流程
        logger.warning("寫入下單稽核 log 失敗", exc_info=True)


def _position_audit_log_path() -> Path:
    """
    我們自己的持倉稽核 log（持久化、只會 append），跟 _order_audit_log_path()
    同一個道理。2026-08-25 實測抓到的狀況：使用者回報「開單成功後，持倉畫面
    卻一直是空的」，但當時能查的只有 SKCOM 自己的 Order.log——而那份 log
    本身會被重置，加上事後又用同一組帳號重新連線了幾次做別的診斷，混進了
    後來連線的內容，已經沒辦法拿它當乾淨的證據回頭查那個時間點到底發生了
    什麼事。這個檔案就是為了下次再發生類似狀況時，能有一份不受 SKCOM 自己
    log 重置、也不會被我方後續診斷連線污染的持倉快照歷史紀錄可查。
    """
    return _find_default_log_path().parent / "position_audit.log"


def _append_position_audit(*, account: str, raw: str, positions: list[dict[str, Any]]) -> None:
    try:
        path = _position_audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        summary = ";".join(
            f"{p.get('product')}/{p.get('direction_key')}x{p.get('qty')}" for p in positions
        )
        line = f"{ts}\taccount={account}\tcount={len(positions)}\tpositions={summary}\traw={raw}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - 稽核 log 寫入失敗不該影響持倉更新主流程
        logger.warning("寫入持倉稽核 log 失敗", exc_info=True)


@dataclass
class OrderResult:
    """下單 / 刪單 / 改價等函式的統一回傳格式。"""

    success: bool
    code: int
    message: str
    raw: str

    def __str__(self) -> str:  # pragma: no cover - 純顯示用途
        status = "OK" if self.success else "FAIL"
        return f"[{status}] code={self.code} {self.message} | {self.raw}"


@dataclass
class LiveQuote:
    product_code: str = ""
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int | None = None
    updated_at: str = ""


@dataclass
class BrokerLiveState:
    """由 COM 事件與查詢 API 累積的即時狀態（供 dashboard 輪詢）。"""

    connected: bool = False
    user_id: str = ""
    environment: int = Environment.PRODUCTION
    accounts: list[str] = field(default_factory=list)
    active_account: str = ""
    subscribed_product: str = ""
    order_product_code: str = ""
    quote: LiveQuote = field(default_factory=LiveQuote)
    positions: list[dict[str, Any]] = field(default_factory=list)
    positions_raw: str = ""
    stop_loss_raw: str = ""
    orders_raw: str = ""
    fulfills_raw: str = ""
    # 2026-08-29 事故調查：發現官方文件其實有一個專門回報「這次未平倉查詢到底
    # 成功還是失敗」的事件 OnOpenInterestGWStatus，我們一直只監聽有資料才會
    # 觸發的 OnOpenInterest，從沒監聽這個狀態事件——導致「查詢其實失敗」跟
    # 「查詢成功但真的沒資料」這兩種完全不同的情況，在我們的畫面/log 上看起來
    # 一模一樣，沒辦法分辨。這個欄位記下最後一次查詢狀態，供診斷用。
    open_interest_query_status: str = ""
    rights_raw: str = ""
    rights: dict[str, Any] = field(default_factory=dict)
    agreements: list[str] = field(default_factory=list)
    kline_api_ok: bool = True
    kline_api_block_reason: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    last_error: str = ""

    def add_message(self, category: str, text: str, *, max_items: int = 200) -> None:
        self.messages.append(
            {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "category": category, "text": text}
        )
        if len(self.messages) > max_items:
            self.messages = self.messages[-max_items:]


class CapitalFuturesBroker:
    """
    群益證券期貨下單 API 包裝類別。

    使用範例：
        broker = CapitalFuturesBroker(environment=Environment.TEST)
        broker.login()  # 從 .env 讀取 CAPITAL_USER_ID / CAPITAL_PASSWORD
        accounts = broker.initialize_order()
        result = broker.send_future_order(
            account=accounts[0],
            product_code="MXFR1",       # 小台近月，實際代碼請以群益商品代碼為準
            side=BuySell.BUY,
            qty=1,
            price="M",                   # 市價；搭配 IOC/FOK，見 send_future_order 說明
            trade_type=TradeType.FOK,
            new_close=NewClose.AUTO,
        )
        print(result)
    """

    def __init__(
        self,
        dll_path: Optional[str] = None,
        environment: int = Environment.PRODUCTION,
    ) -> None:
        self._lock = threading.Lock()
        self.accounts: List[str] = []
        self.user_id: Optional[str] = None
        self.environment = environment
        self._logged_in = False
        self._order_initialized = False
        self._quote_monitoring = False
        self._stocks_ready = False
        self._last_snapshot_query_at: float = 0.0
        # 2026-08-30 實盤事故：OnOpenInterest 對「一次查詢」的回應，實際上是
        # 分成好幾次獨立事件呼叫送過來的（一筆部位資料一次呼叫，接著幾毫秒
        # 後再一次呼叫送「##,,,,...」查詢結束終止符），不是文件字面上看起來
        # 的「每次推送都是完整快照」。這個緩衝區用來累積同一次查詢裡收到的
        # 部位資料列，只有在收到終止符時才正式提交進 self.live.positions，
        # 見 _parse_open_interest。
        self._open_interest_buffer: list[dict[str, Any]] = []
        self.on_order_reply: Optional[Callable[[str, str], None]] = None
        self.live = BrokerLiveState(environment=environment)
        self._kline_loaded: set[str] = set()
        self._kline_failed: set[str] = set()
        self._kline_complete = False
        self._log_path_configured = False
        self.log_path = _find_default_log_path()
        # 即時報價 / 成交明細事件計數（診斷用）
        self._quote_notify_count = 0
        self._tick_notify_count = 0

        self.dll_path = Path(dll_path) if dll_path else self._resolve_dll_path()
        self._load_com_library()

    # ------------------------------------------------------------------
    # 初始化 / DLL 載入
    # ------------------------------------------------------------------
    def _resolve_dll_path(self) -> Path:
        env_path = os.getenv("CAPITAL_DLL_PATH")
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise FileNotFoundError(f"CAPITAL_DLL_PATH 指定的路徑不存在: {path}")
            return path
        return _find_default_dll_path()

    def _load_com_library(self) -> None:
        import comtypes.client

        _ensure_sta_apartment()
        dll_dir = str(self.dll_path.parent)
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)
        os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")

        # 2026-08-06 發現：官方範例 Quote.py 用純檔名「SKCOM.dll」呼叫 GetModule，
        # 靠 PATH 讓 Windows 的 LoadTypeLibEx 找到檔案；但 comtypes 內部
        # （client/_generate.py GetModule）如果偵測到不是絕對路徑，事後還會另外去
        # 問 Windows 的 TypeLib 登錄檔「這個 DLL 官方登記在哪裡」，如果登錄檔記的
        # 是舊路徑（例如專案資料夾搬過、或曾經從別的位置 regsvr32 過），就會撞到
        # comtypes 自己的 assert（路徑是絕對路徑但檔案不存在）而整個連線失敗，
        # 而且錯誤訊息是空的 AssertionError，完全看不出原因。改成直接傳入我們自己
        # 已經解析好、確定存在的絕對路徑，跳過這段容易踩雷的登錄檔查詢。
        comtypes.client.GetModule(str(self.dll_path))
        import comtypes.gen.SKCOMLib as sk  # noqa: N812 (COM 產生的模組名稱)

        self._sk = sk
        self.center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        self.reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
        self.order = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
        self.quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        # 與 PythonExampleV2 Quote.py 一致，建立海外/零股報價元件（國內期報價亦會用到）
        self.os_quote = comtypes.client.CreateObject(sk.SKOSQuoteLib, interface=sk.ISKOSQuoteLib)
        self.oo_quote = comtypes.client.CreateObject(sk.SKOOQuoteLib, interface=sk.ISKOOQuoteLib)

        self._register_events(comtypes.client)

    def _register_events(self, comtypes_client) -> None:
        """必須在登入前完成事件註冊，否則登入會收到
        SK_WARNING_REGISTER_REPLYLIB_ONREPLYMESSAGE_FIRST 警告。"""
        broker = self

        class CenterEvents:
            def OnShowAgreement(self, bstrData):  # noqa: N802
                text = _decode_capital_text(bstrData).strip()
                if text:
                    broker.live.agreements.append(text)
                    logger.info("[OnShowAgreement] %s", text)
                    broker.live.add_message("agreement", text)

        class ReplyEvents:
            def OnReplyMessage(self, bstrUserID, bstrMessages):  # noqa: N802 (COM 事件簽章)
                logger.info("[ReplyMessage] %s: %s", bstrUserID, bstrMessages)
                broker.live.add_message("reply", f"{bstrUserID}: {bstrMessages}")
                return -1  # 依官方範例，必須回傳 -1 確認碼

            def OnReplyClearMessage(self, bstrUserID):  # noqa: N802
                logger.info("[ReplyClearMessage] %s 正在清除前日回報", bstrUserID)
                broker.live.add_message("reply", f"{bstrUserID} 清除前日回報")

            def OnComplete(self, bstrUserID):  # noqa: N802
                logger.info("[Complete] %s 回報連線與資料正常", bstrUserID)
                broker.live.add_message("reply", f"{bstrUserID} 回報連線正常")

            def OnNewData(self, bstrUserID, bstrData):  # noqa: N802
                logger.info("[NewData] %s: %s", bstrUserID, bstrData)
                broker.live.add_message("order_report", bstrData)
                if broker.on_order_reply:
                    broker.on_order_reply(bstrUserID, bstrData)

        class OrderEvents:
            def OnAccount(self, bstrLogInID, bstrAccountData):  # noqa: N802
                try:
                    values = bstrAccountData.split(",")
                    # 群益格式：IB (4碼) + 帳號 (7碼)
                    if len(values) < 4:
                        logger.warning("[Account] 格式異常: %s", bstrAccountData)
                        return
                    full_account = values[1] + values[3]
                    if full_account not in broker.accounts:
                        broker.accounts.append(full_account)
                    broker.live.accounts = list(broker.accounts)
                    logger.info("[Account] %s", full_account)
                    broker.live.add_message("account", full_account)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OnAccount parse failed: %s", exc)

            def OnAsyncOrder(self, nThreadID, nCode, bstrMessage):  # noqa: N802
                text = f"thread={nThreadID} code={nCode} msg={bstrMessage}"
                logger.info("[AsyncOrder] %s", text)
                broker.live.add_message("async_order", text)

            def OnOpenInterest(self, bstrData):  # noqa: N802
                try:
                    logger.info("[OnOpenInterest] %s", bstrData)
                    broker.live.positions_raw = bstrData
                    broker.live.add_message("position", bstrData)
                    broker._parse_open_interest(bstrData)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OnOpenInterest parse failed: %s", exc)

            def OnOpenInterestGWStatus(self, nQueryStatus, bstrErrorMsg):  # noqa: N802,N803
                # 2026-08-29：官方文件記載的查詢狀態回報事件（0:成功 1:失敗+錯誤
                # 訊息），我們一直沒監聽——沒有這個，沒辦法分辨「查詢真的失敗」
                # 跟「查詢成功但剛好沒資料」，這次事故調查才發現這個缺口。
                text = f"status={nQueryStatus} msg={bstrErrorMsg}"
                logger.info("[OnOpenInterestGWStatus] %s", text)
                broker.live.open_interest_query_status = text
                broker.live.add_message("open_interest_status", text)

            def OnStopLossReport(self, bstrData):  # noqa: N802
                # 新版期貨智慧單(停損單/移動停損/OCO/觸價單)被動回報，透過呼叫
                # GetStopLossReport 觸發（見官方文件《8.下單-國內期選智慧單》）。
                try:
                    logger.info("[OnStopLossReport] %s", bstrData)
                    broker.live.stop_loss_raw = bstrData
                    broker.live.add_message("stop_loss_report", bstrData)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OnStopLossReport parse failed: %s", exc)

            def OnFutureRights(self, bstrData):  # noqa: N802
                try:
                    logger.info("[OnFutureRights] %s", bstrData)
                    broker.live.rights_raw = bstrData
                    broker._parse_future_rights(bstrData)
                    broker.live.add_message("rights", bstrData)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OnFutureRights parse failed: %s", exc)

            def OnProxyOrder(self, nStampID, nCode, bstrMessage):  # noqa: N802
                text = f"stamp={nStampID} code={nCode} msg={bstrMessage}"
                logger.info("[OnProxyOrder] %s", text)
                broker.live.add_message("proxy_order", text)

        class QuoteEvents:
            def OnNotifyTicksLONG(  # noqa: N802
                self,
                sMarketNo,
                nIndex,
                nPtr,
                nDate,
                nTimehms,
                nTimemillismicros,
                nBid,
                nAsk,
                nClose,
                nQty,
                nSimulate,
            ):
                # 成交明細：有成交才推送（與 RequestTicks 對應）
                broker._tick_notify_count += 1
                broker.live.quote.bid = float(nBid) / 100.0
                broker.live.quote.ask = float(nAsk) / 100.0
                broker.live.quote.last_price = float(nClose) / 100.0
                broker.live.quote.volume = int(nQty)
                broker.live.quote.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
                broker._on_tick_for_kline(float(nClose) / 100.0, int(nQty), nDate, nTimehms)

            def OnNotifyKLineData(self, bstrStockNo, bstrData):  # noqa: N802
                broker._ingest_kline_row(str(bstrStockNo), str(bstrData))
                broker.live.add_message("kline", f"{bstrStockNo}: {bstrData[:120]}")

            def OnKLineComplete(self, bstrEndString):  # noqa: N802
                broker._kline_complete = True
                text = _decode_capital_text(bstrEndString)
                logger.info("[OnKLineComplete] %s", text)
                broker.live.add_message("kline", f"歷史 K 線回補完成: {text}")

            def OnConnection(self, nKind, nCode):  # noqa: N802
                kind_msg = broker._msg(nKind)
                code_msg = broker._msg(nCode)
                msg = f"kind={nKind}({kind_msg}) code={nCode}({code_msg})"
                logger.info("[OnConnection] %s", msg)
                broker.live.add_message("quote_conn", msg)
                if nKind == 3003:
                    broker._stocks_ready = True
                    logger.info("OnConnection: 商品檔下載完成 (nKind=3003)")
                    broker.live.add_message("quote_conn", "商品檔下載完成 (3003)")
                elif nKind == 3002:
                    broker._stocks_ready = False
                    logger.warning("OnConnection: 報價斷線 (nKind=3002)")

            def OnNotifyQuoteLONG(self, sMarketNo, nIndex):  # noqa: N802
                # 即時報價：對應 RequestStocks（官方看盤主路徑）
                try:
                    stock = broker._sk.SKSTOCKLONG()
                    res = broker.quote.SKQuoteLib_GetStockByIndexLONG(sMarketNo, nIndex, stock)
                    n_code, populated_stock = broker._unpack_com_stock_result(res, stock)
                    if n_code == 0 and populated_stock:
                        broker._quote_notify_count += 1
                        broker._apply_stock_quote(populated_stock, source="OnNotifyQuoteLONG")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OnNotifyQuoteLONG parse failed: %s", exc)

        self._center_events = comtypes_client.GetEvents(self.center, CenterEvents())
        self._reply_events = comtypes_client.GetEvents(self.reply, ReplyEvents())
        self._order_events = comtypes_client.GetEvents(self.order, OrderEvents())
        self._quote_events = comtypes_client.GetEvents(self.quote, QuoteEvents())

    def _configure_log_path(self) -> None:
        """設定 SKCOM log 路徑；官方要求此函式須於 Login 前最先呼叫。"""
        if self._log_path_configured:
            return
        self.log_path.mkdir(parents=True, exist_ok=True)
        log_dir = str(self.log_path.resolve())
        n_code = int(self.center.SKCenterLib_SetLogPath(log_dir))
        if n_code != 0:
            msg = self._msg(n_code)
            logger.warning("SetLogPath 失敗 (%s): %s", log_dir, msg)
            self.live.add_message("log", f"SetLogPath 失敗: {msg}")
            return
        self._log_path_configured = True
        logger.info("SKCOM log 路徑: %s", log_dir)
        self.live.add_message("log", f"SKCOM log 路徑: {log_dir}")

    # ------------------------------------------------------------------
    # 登入 / 初始化下單
    # ------------------------------------------------------------------
    def login(
        self,
        user_id: Optional[str] = None,
        password: Optional[str] = None,
        *,
        request_agreement: bool = True,
    ) -> None:
        user_id = user_id or os.getenv("CAPITAL_USER_ID")
        password = password or os.getenv("CAPITAL_PASSWORD")
        if not user_id or not password:
            raise ValueError(
                "請提供 user_id/password 參數，或於 .env 設定 "
                "CAPITAL_USER_ID / CAPITAL_PASSWORD"
            )

        self._configure_log_path()

        n_code = self.center.SKCenterLib_SetAuthority(self.environment)
        if n_code != 0:
            logger.warning("SetAuthority 回傳非 0: %s", self._msg(n_code))

        n_code = self.center.SKCenterLib_Login(user_id, password)
        if n_code != 0:
            raise RuntimeError(f"登入失敗: {self._msg(n_code)}")

        self.user_id = user_id
        self._logged_in = True
        self.live.user_id = user_id
        self.live.connected = True
        self.live.last_error = ""
        logger.info("登入成功: %s", user_id)
        if request_agreement:
            self.request_agreement_status(wait_seconds=2.0)

    def request_agreement_status(self, wait_seconds: float = 2.0) -> int:
        """查詢同意書簽署狀態（結果由 OnShowAgreement 事件回傳）。"""
        if not self._logged_in or not self.user_id:
            raise RuntimeError("請先呼叫 login()")
        self.live.agreements.clear()
        n_code = int(self.center.SKCenterLib_RequestAgreement(self.user_id))
        if n_code != 0:
            msg = self._msg(n_code)
            logger.warning("RequestAgreement: %s", msg)
            self.live.add_message("agreement", f"查詢失敗: {msg}")
        else:
            self._pump_events(wait_seconds)
            self._evaluate_kline_api_agreement()
        return n_code

    def initialize_order(self, wait_seconds: float = 3.0) -> List[str]:
        """初始化下單元件並取得交易帳號列表（帳號透過 OnAccount 事件非同步回傳）。"""
        if not self._logged_in:
            raise RuntimeError("請先呼叫 login()")

        n_code = self.order.SKOrderLib_Initialize()
        if n_code != 0:
            logger.warning("SKOrderLib_Initialize 回傳: %s", self._msg(n_code))

        cert_code = self.order.ReadCertByID(self.user_id)
        if cert_code != 0:
            raise RuntimeError(f"讀取交易憑證失敗: {self._msg(cert_code)}")

        self.order.GetUserAccount()
        self._pump_events(wait_seconds)
        self._order_initialized = True
        self.live.accounts = list(self.accounts)
        if self.accounts and not self.live.active_account:
            futures_accounts = [a for a in self.accounts if a.startswith("F")]
            self.live.active_account = futures_accounts[0] if futures_accounts else self.accounts[0]
        return self.accounts

    # ------------------------------------------------------------------
    # 下單 / 刪單 / 改價
    # ------------------------------------------------------------------
    def send_future_order(
        self,
        account: str,
        product_code: str,
        side: int,
        qty: int,
        price: str = "M",
        trade_type: int = TradeType.FOK,
        day_trade: int = 0,
        new_close: int = NewClose.AUTO,
        reserved: int = Reserved.INTRADAY,
        is_async: bool = False,
    ) -> OrderResult:
        """
        送出台指期貨委託單（大台 TX / 小台 MTX / 微台 TMF）。

        product_code: 群益期貨商品代碼（含月份，例如小台近月 "MXFR1"），
                       實際代碼請以群益商品代碼表或報價元件查詢結果為準。
        price: 字串價格。官方文件（《7.下單-國內期選》FUTUREORDER 結構定義）：
               搭配 trade_type=IOC 或 FOK 時，填 "M" 代表市價、"P" 代表範圍市價；
               這裡沒有 nPriceFlag 旗標（那是另一個結構 FUTUREPROXYORDER 才有）。
               2026-07-24 曾誤傳 price="0" 想代表市價，被群益以 [530] 委託單類別
               (ORDER TYPE)輸入錯誤拒單 151 次——"0" 不是合法值，市價要傳 "M"。
        """
        self._require_ready()
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = account
        order.bstrStockNo = product_code
        order.sTradeType = trade_type
        order.sBuySell = side
        order.sDayTrade = day_trade
        order.sNewClose = new_close
        order.bstrPrice = str(price)
        order.nQty = int(qty)
        order.sReserved = reserved

        # 2026-08-25 實測抓到的重大 bug：跟 STP/MIT 一樣，SendFutureOrderCLR 也是
        # message 在前、code 在後（用 comtypes 對 SKCOM.dll 產生的型別庫逐一核對過
        # 官方 COMMETHOD 定義：SendFutureOrderCLR/CancelOrderBySeqNo/
        # CorrectPriceBySeqNo/SendFutureSTPOrderV1/SendFutureMITOrderV1/
        # CancelTFStrategyOrderV1 全部都是 (['out'] bstrMessage, ['out','retval']
        # retCode) 這個順序，不是只有智慧單特殊）。原本這裡用 code, message 順序
        # 解包，導致 code 變數實際上拿到的是 bstrMessage（真單委託成功時常常是一段
        # 含委託書號的文字，或恰好長得像一串數字），永遠不等於 0，於是每一筆
        # 真正成功的一般市價/限價委託，都會被誤判成失敗——這正是造成
        # 「委託系統回報失敗，但偵測到近期有相符的真實成交紀錄」這個孤兒單警告
        # 反覆觸發的根因，不是罕見的例外狀況，是每一筆一般委託都會踩到。
        message, code = self.order.SendFutureOrderCLR(self.user_id, is_async, order)
        _append_order_audit(
            account=account,
            product_code=product_code,
            side=side,
            qty=qty,
            price=str(price),
            trade_type=trade_type,
            new_close=new_close,
            code=code,
            message=self._msg(code),
            raw=message,
        )
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    def send_future_stp_order(
        self,
        account: str,
        stock_no: str,
        settlement_month: str,
        side: int,
        qty: int,
        trigger_price: str,
        price: str = "P",
        new_close: int = NewClose.NEW,
        trade_type: int = SmartOrderTradeType.ROD,
        day_trade: int = 0,
        order_price_type: int = SmartOrderPriceType.RANGE_MARKET,
        reserved: int = Reserved.INTRADAY,
        is_async: bool = False,
    ) -> OrderResult:
        """
        送出期貨停損（STP）智慧單——真正掛在券商/智慧單主機那邊的條件單，觸價後
        由券商主機自動送出委託，不需要本地程式持續運行監控（跟 strategy_service.py
        目前用的「軟停損」——本地輪詢比價、觸價才臨時送一張新市價單——是完全不同
        層級的保護）。詳見官方文件《8.下單-國內期選智慧單》SendFutureSTPOrderV1。

        帳戶必須已簽署「期貨智慧單風險預告書」，否則會被拒單（可從
        get_live_state()['agreements'] 確認）。

        stock_no: 期權商品代號，用「根代碼」（例如 "TMF"），月份另外用
                  settlement_month 指定，不要跟 send_future_order 一樣傳
                  "TM2609" 這種月份已內嵌的代碼（避免跟 settlement_month 重複/衝突，
                  兩者哪個才是正確格式官方範例沒有給實際值，需要實際送單驗證）。
        settlement_month: 委託商品年月，YYYYMM 共 6 碼（例如 "202609"）。
        trigger_price: 觸發價格字串（bstrTrigger）——現價觸及這個價位時，才會
                        由券商主機自動送出委託，這是真正的條件單觸發價，跟
                        price（觸發之後實際送出的委託價）是兩回事。
        price: 觸發之後送出的委託價。"P" = 範圍市價（配合 order_price_type=3，
               觸發後盡量成交但仍有一個範圍保護，不是無限制市價）；
               若 order_price_type=2（限價）才需要填實際限價數字。
        new_close: 通常保護留倉用 NewClose.CLOSE，這裡預設 NEW 是因為測試/一般
                    情境下呼叫端應該明確指定，不要依賴預設值。
        """
        self._require_ready()
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = account
        order.bstrStockNo = stock_no
        order.bstrSettlementMonth = settlement_month
        order.sNewClose = new_close
        order.sBuySell = side
        order.sTradeType = trade_type
        order.sDayTrade = day_trade
        order.bstrPrice = str(price)
        order.nQty = int(qty)
        order.bstrTrigger = str(trigger_price)
        order.sReserved = reserved
        order.nOrderPriceType = order_price_type
        # 長效單相關欄位：這裡固定不啟用長效單（效期內未觸發就在收盤失效，
        # 不需要額外指定結束日），避免沒填的長效單參數被解讀成非預期值。
        order.nLongActionFlag = 0
        order.bstrLongEndDate = ""
        order.nLAType = 1

        # 2026-08-21 實測發現：跟 SendFutureOrderCLR（code, message 順序）不同，
        # 官方文件範例是 `bstrMessage,nCode= m_pSKOrder.SendFutureSTPOrderV1(...)`
        # ——message 在前、code 在後。實測送出一筆真實可成交的智慧單並立刻刪單
        # 驗證過：用 code, message 順序時，成功送單（真的掛在券商那邊、有回傳
        # 條件單號）卻被誤判成失敗（因為把 bstrMessage 字串拿去跟 0 比較）。
        message, code = self.order.SendFutureSTPOrderV1(self.user_id, is_async, order)
        _append_order_audit(
            account=account,
            product_code=f"{stock_no}/{settlement_month}",
            side=side,
            qty=qty,
            price=f"trigger={trigger_price} price={price}",
            trade_type=trade_type,
            new_close=new_close,
            code=code,
            message=self._msg(code),
            raw=message,
        )
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    def send_future_mit_order(
        self,
        account: str,
        stock_no: str,
        settlement_month: str,
        side: int,
        qty: int,
        trigger_price: str,
        trigger_direction: int,
        price: str,
        deal_price: str = "",
        new_close: int = NewClose.CLOSE,
        trade_type: int = SmartOrderTradeType.ROD,
        day_trade: int = 0,
        order_price_type: int = SmartOrderPriceType.LIMIT,
        reserved: int = Reserved.INTRADAY,
        is_async: bool = False,
    ) -> OrderResult:
        """
        送出期貨 MIT 觸價智慧單——用來做真正掛在券商端的停利單（STP 只能做停損，
        觸發方向固定跟部位反向；MIT 用 nTriggerDirection 明確指定 GTE/LTE，兩個
        方向都能用，停利/停損都可以掛）。詳見官方文件《8.下單-國內期選智慧單》
        SendFutureMITOrderV1。

        跟 STP 一樣要求帳戶已簽署「期貨智慧單風險預告書」。

        trigger_direction: TriggerDirection.GTE（現價漲到/超過觸發價才觸發，多單
                            停利用）或 LTE（跌到/低於才觸發，空單停利用）。
        price: 委託價格（bstrPrice），限價模式（order_price_type=2）時的實際限價。
        deal_price: 官方文件標示「成交價」，實際用途官方文件沒解釋清楚（STP 沒有
                    這個欄位），目前只能先傳空字串，靠實際下單觀察錯誤訊息校準。
        """
        self._require_ready()
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = account
        order.bstrStockNo = stock_no
        order.bstrPrice = str(price)
        order.sTradeType = trade_type
        order.sBuySell = side
        order.sDayTrade = day_trade
        order.sNewClose = new_close
        order.nQty = int(qty)
        order.bstrTrigger = str(trigger_price)
        order.bstrSettlementMonth = settlement_month
        order.nOrderPriceType = order_price_type
        order.bstrDealPrice = str(deal_price)
        order.nTriggerDirection = trigger_direction
        order.sReserved = reserved

        message, code = self.order.SendFutureMITOrderV1(self.user_id, is_async, order)
        _append_order_audit(
            account=account,
            product_code=f"{stock_no}/{settlement_month}",
            side=side,
            qty=qty,
            price=f"trigger={trigger_price}({trigger_direction}) price={price} deal_price={deal_price}",
            trade_type=trade_type,
            new_close=new_close,
            code=code,
            message=self._msg(code),
            raw=message,
        )
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    def send_future_oco_order(
        self,
        account: str,
        stock_no: str,
        settlement_month: str,
        qty: int,
        side: str | int,
        trigger_price: str,
        price: str,
        side2: str | int,
        trigger_price2: str,
        price2: str,
        new_close: int = NewClose.CLOSE,
        trade_type: int = SmartOrderTradeType.ROD,
        day_trade: int = 0,
        order_price_type: int = SmartOrderPriceType.RANGE_MARKET,
        reserved: int = Reserved.INTRADAY,
        is_async: bool = False,
    ) -> OrderResult:
        """
        送出期貨 OCO（二擇一）智慧單——一次委託同時掛「兩支腿」（例如停損+停利），
        由券商主機當成一組配對好的條件單管理：其中一支腿觸發成交後，另一支腿
        自動失效，不會像分開送兩張獨立的 STP+MIT 那樣，各自向券商聲請同一口
        部位的平倉額度而互相打架。

        2026-08-27 實盤事故：`_place_stop_order_for_state`/`_place_mit_order_for_state`
        分開送出兩張獨立的平倉智慧單，第二張被拒單（[999] 勾選平倉而留倉部位
        不足）——因為兩張各自聲請同一口的平倉額度，券商只能滿足其中一張。
        群益官方範例（TFStrategyOrder.py::buttonSendFutureOCOOrderV1_Click）用
        SendFutureOCOOrderV1 一次委託帶兩支腿（bstrTrigger/bstrPrice 跟
        bstrTrigger2/bstrPrice2），才是券商設計給這個情境用的正規做法。

        stock_no/settlement_month: 跟 STP/MIT 一樣，用根代碼＋YYYYMM 分開指定。
        side/trigger_price/price: 第一支腿（例如停損）。
        side2/trigger_price2/price2: 第二支腿（例如停利）。
        price/price2: "P" = 範圍市價（配合 order_price_type=3，官方文件範例
                      這樣用）；order_price_type=2（限價）才需要填實際限價數字。
        """
        self._require_ready()
        order = self._sk.FUTUREORDER()
        order.bstrFullAccount = account
        order.bstrStockNo = stock_no
        order.bstrSettlementMonth = settlement_month
        order.sTradeType = trade_type
        order.sBuySell = side
        order.sBuySell2 = side2
        order.sDayTrade = day_trade
        order.sNewClose = new_close
        order.nQty = int(qty)
        order.bstrTrigger = str(trigger_price)
        order.bstrPrice = str(price)
        order.bstrTrigger2 = str(trigger_price2)
        order.bstrPrice2 = str(price2)
        order.sReserved = reserved
        order.nOrderPriceType = order_price_type
        # 長效單相關欄位：跟 STP 一樣固定不啟用長效單。
        order.nLongActionFlag = 0
        order.bstrLongEndDate = ""
        order.nLAType = 1
        order.nTimeFlag = 1

        # 跟 STP/MIT 一樣：官方範例是 `bstrMessage,nCode=
        # m_pSKOrder.SendFutureOCOOrderV1(...)`——message 在前、code 在後。
        message, code = self.order.SendFutureOCOOrderV1(self.user_id, is_async, order)
        _append_order_audit(
            account=account,
            product_code=f"{stock_no}/{settlement_month}",
            side=side,
            qty=qty,
            price=(
                f"trigger={trigger_price} price={price} / "
                f"trigger2={trigger_price2} price2={price2}"
            ),
            trade_type=trade_type,
            new_close=new_close,
            code=code,
            message=self._msg(code),
            raw=message,
        )
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    def cancel_stp_order(
        self,
        account: str,
        *,
        smart_key: str,
        seq_no: str = "",
        order_no: str = "",
        long_action_key: str = "",
        trade_kind: int = SmartOrderKind.STP,
        market: int = SmartOrderMarket.DOMESTIC_FUTURES,
        is_async: bool = False,
    ) -> OrderResult:
        """
        取消期貨智慧單（含 STP 停損單）。smart_key 是送單成功回報裡的「智慧單號」，
        若該智慧單已經觸發、產生了委託書號，官方文件特別註明 order_no 也要一併
        填入，否則可能影響解除保證金風控。
        """
        self._require_ready()
        cancel_order = self._sk.CANCELSTRATEGYORDER()
        cancel_order.bstrLogInID = self.user_id
        cancel_order.bstrFullAccount = account
        cancel_order.nMarket = market
        cancel_order.bstrSmartKey = smart_key
        cancel_order.nTradeKind = trade_kind
        cancel_order.bstrSeqNo = seq_no
        cancel_order.bstrOrderNo = order_no
        cancel_order.bstrLongActionKey = long_action_key

        message, code = self.order.CancelTFStrategyOrderV1(cancel_order, is_async)
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    def get_stop_loss_report(
        self, account: str, *, report_status: int = 0, kind: str = "STP", date: str = ""
    ) -> int:
        """
        查詢目前掛著的智慧單（含 STP 停損單）。結果非同步由 OnStopLossReport
        事件回傳，寫入 self.live.stop_loss_raw，不是這個函式的回傳值本身。

        2026-08-21 實測發現：kind 傳空字串會被拒（M999 找不到資料表），官方
        文件範例是從下拉選單選 "STP"/"MST"/"OCO"/"MIT"/"AB" 其中之一，不能空白；
        date 也採用文件範例格式 YYYYMMDD，預設抓當天。
        """
        self._require_ready()
        query_date = date or datetime.now().strftime("%Y%m%d")
        return int(
            self.order.GetStopLossReport(
                self.user_id, account, int(report_status), kind, query_date
            )
        )

    def cancel_order_by_seqno(
        self, account: str, seq_no: str, is_async: bool = False
    ) -> OrderResult:
        """依委託書號（SeqNo）刪單。"""
        self._require_ready()
        # 2026-08-25：跟 send_future_order 同一個 bug，型別庫確認 CancelOrderBySeqNo
        # 也是 message 在前、code 在後。
        message, code = self.order.CancelOrderBySeqNo(
            self.user_id, is_async, account, seq_no
        )
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    def correct_price_by_seqno(
        self,
        account: str,
        seq_no: str,
        price: str,
        trade_type: int = TradeType.ROD,
        is_async: bool = False,
    ) -> OrderResult:
        """依委託書號（SeqNo）改價。"""
        self._require_ready()
        # 2026-08-25：跟 send_future_order 同一個 bug，型別庫確認 CorrectPriceBySeqNo
        # 也是 message 在前、code 在後。
        message, code = self.order.CorrectPriceBySeqNo(
            self.user_id, is_async, account, seq_no, str(price), trade_type
        )
        return OrderResult(success=(code == 0), code=code, message=self._msg(code), raw=message)

    # ------------------------------------------------------------------
    # 報價（選用；下單前可先訂閱即時 Tick 供策略判斷進場價）
    # ------------------------------------------------------------------
    def enter_monitor(self) -> None:
        # 台指期等期貨商品須用 EnterMonitorLONG（對應 OnNotifyTicksLONG）
        self._stocks_ready = False
        n_code = int(self.quote.SKQuoteLib_EnterMonitorLONG())
        if n_code != 0:
            msg = self._msg(n_code)
            logger.warning("EnterMonitorLONG 回傳: %s", msg)
            self.live.add_message("quote_conn", f"EnterMonitorLONG failed: {msg}")
        else:
            self._quote_monitoring = True
            self.live.add_message("quote_conn", "EnterMonitorLONG ok, waiting quote server")
            logger.info("EnterMonitorLONG 成功，等待報價主機連線")

    def _quote_connection_status(self) -> int:
        try:
            return int(self.quote.SKQuoteLib_IsConnected())
        except Exception as exc:  # noqa: BLE001
            logger.warning("SKQuoteLib_IsConnected 失敗: %s", exc)
            return 0

    def is_quote_ready(self) -> bool:
        return self._stocks_ready

    def _wait_quote_connected(self, timeout: float = 120.0) -> bool:
        """等待報價主機連線與商品基本資料下載完成（OnConnection nKind=3003）。"""
        deadline = time.time() + timeout
        last_status = -1
        next_status_log_at = 0.0
        while time.time() < deadline:
            if self._stocks_ready:
                logger.info("報價連線及商品資料下載完成 (_stocks_ready=True)")
                return True
            now = time.time()
            if now >= next_status_log_at:
                status = self._quote_connection_status()
                if status != last_status:
                    logger.info("等待報價 3003：IsConnected=%s", status)
                    last_status = status
                next_status_log_at = now + 5.0
            # 官方 GUI 靠 mainloop 派送 COM；避免在等待期間頻繁呼叫 IsConnected
            self._pump_events(0.05)
        logger.warning("等待報價連線或商品下載逾時 (timeout=%ss)", timeout)
        return False

    def request_stocks(self, product_code: str, page: int = 1) -> int:
        """
        訂閱即時報價（官方主路徑）。
        對應 OnNotifyQuoteLONG → GetStockByIndexLONG。
        page：分頁編號，官方範例初學者可用 1。
        """
        self.live.subscribed_product = product_code
        self.live.quote.product_code = product_code
        res = self.quote.SKQuoteLib_RequestStocks(int(page), product_code)
        if isinstance(res, (tuple, list)):
            return int(res[1])
        return int(res)

    def request_ticks(self, product_code: str, page: int = 1) -> int:
        """
        訂閱成交明細與五檔（有成交才推 OnNotifyTicksLONG）。
        注意：這不是「持續報價心跳」；看盤 last 應優先靠 request_stocks。
        """
        self.live.subscribed_product = product_code
        self.live.quote.product_code = product_code
        res = self.quote.SKQuoteLib_RequestTicks(int(page), product_code)
        if isinstance(res, (tuple, list)):
            return int(res[1])
        return int(res)

    def _apply_stock_quote(self, stock: Any, *, source: str = "") -> None:
        """從 SKSTOCKLONG 寫入 live.quote（價格群益以 *100 整數傳遞）。"""
        try:
            code = str(getattr(stock, "bstrStockNo", "") or "")
            if code:
                self.live.quote.product_code = code
            n_close = int(getattr(stock, "nClose", 0) or 0)
            n_bid = int(getattr(stock, "nBid", 0) or 0)
            n_ask = int(getattr(stock, "nAsk", 0) or 0)
            n_qty = int(getattr(stock, "nTQty", 0) or 0)
            if n_close != 0:
                self.live.quote.last_price = float(n_close) / 100.0
                # 2026-08-25 實測抓到的 bug：nTQty 是「今日累計總量」（官方
                # readme_doc 範例把它標成「總量」，不是單次輪詢的新增量）。
                # 這裡如果把它當成這次輪詢新增的成交量傳給 _on_tick_for_kline，
                # 每次輪詢（大約每 10~30 秒一次，見 request_stocks 的用途說明）
                # 都會把同一個、且持續變大的累計總量重複疊加進當前這根K棒的
                # volume，短短一小時內就能疊到幾百萬，完全脫離真實成交量。
                # 真正的逐筆成交量已經由 OnNotifyTicksLONG（見上方）正確累加，
                # 這裡只需要更新價格；volume 傳 0（on_tick 對 volume<=0 的呼叫
                # 不會動 volume 欄位，只更新 high/low/close）。
                self._on_tick_for_kline(float(n_close) / 100.0, 0, 0, 0)
            if n_bid != 0:
                self.live.quote.bid = float(n_bid) / 100.0
            if n_ask != 0:
                self.live.quote.ask = float(n_ask) / 100.0
            if n_qty != 0:
                self.live.quote.volume = n_qty
            self.live.quote.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            if source:
                self.live.add_message(
                    "quote",
                    f"{source} {code or self.live.quote.product_code} "
                    f"last={self.live.quote.last_price} bid={self.live.quote.bid} "
                    f"ask={self.live.quote.ask}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_apply_stock_quote failed: %s", exc)

    def request_kline_history(
        self,
        product_code: str,
        *,
        days: int | None = None,
        bar_limit: int = 500,
        bar_minutes: int = 60,
    ) -> int:
        """向報價伺服器請求 60 分 K 歷史（結果由 OnNotifyKLineData / OnKLineComplete 回傳）。"""
        import ctypes

        if days is None:
            days = _days_for_kline_bars(bar_limit, bar_minutes=bar_minutes)
        end = datetime.now()
        start = end - timedelta(days=days)
        self._kline_complete = False
        n_code = int(
            self.quote.SKQuoteLib_RequestKLineAMByDate(
                product_code,
                ctypes.c_short(0),  # 分線
                ctypes.c_short(1),  # 新版輸出
                ctypes.c_short(0),  # 全盤
                _format_capital_kline_date(start),
                _format_capital_kline_date(end),
                ctypes.c_short(int(bar_minutes)),
            )
        )
        msg = self._msg(n_code)
        if n_code != 0:
            logger.warning("RequestKLineAMByDate %s: %s", product_code, msg)
            if "AGREEMENT" in msg.upper():
                self.live.kline_api_ok = False
                self.live.kline_api_block_reason = msg
        return n_code

    def request_kline_recent(self, product_code: str, *, bar_minutes: int = 60) -> int:
        """備援：不帶日期區間的 RequestKLineAM（官方文件：僅提供歷史資料）。"""
        import ctypes

        self._kline_complete = False
        n_code = int(
            self.quote.SKQuoteLib_RequestKLineAM(
                product_code,
                ctypes.c_short(0),
                ctypes.c_short(1),
                ctypes.c_short(0),
            )
        )
        if n_code != 0:
            logger.warning("RequestKLineAM %s: %s", product_code, self._msg(n_code))
        return n_code

    def _wait_kline_complete(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self._kline_complete:
            self._pump_events(0.05)

    def _request_kline_history_when_ready(
        self,
        product_code: str,
        *,
        bar_limit: int = 500,
        max_attempts: int = 2,
    ) -> bool:
        if not self._stocks_ready:
            logger.debug("略過歷史 K 線 %s：報價尚未 3003 就緒", product_code)
            return False
        if product_code in self._kline_failed:
            return False
        if not self.live.kline_api_ok:
            reason = self.live.kline_api_block_reason or "證券期貨 API 行情同意書未簽署"
            logger.warning(
                "跳過歷史 K 線請求 %s：%s（圖表將僅使用 API/tick 合成）",
                product_code,
                reason,
            )
            self.live.add_message("kline", f"歷史 K 線略過: {reason}")
            self._kline_failed.add(product_code)
            return False

        for attempt in range(1, max_attempts + 1):
            self._pump_events(0.2)
            n_code = self.request_kline_history(product_code, bar_limit=bar_limit)
            if n_code == 0:
                self._wait_kline_complete(timeout=3.0)
                self._pump_events(0.3)
                self._kline_loaded.add(product_code)
                logger.info("歷史 K 線請求成功 %s（bar_limit=%s）", product_code, bar_limit)
                return True
            msg = self._msg(n_code)
            if "QUOTE_CONNECT" in msg.upper() and attempt < max_attempts:
                logger.info(
                    "歷史 K 線請求 %s 第 %d 次過早，等待後重試: %s",
                    product_code,
                    attempt,
                    msg,
                )
                self._pump_events(0.5)
                continue
            if attempt == 1 and n_code != 0:
                logger.info("RequestKLineAMByDate 失敗，改試 RequestKLineAM: %s", msg)
                fallback = self.request_kline_recent(product_code)
                if fallback == 0:
                    self._wait_kline_complete(timeout=3.0)
                    self._pump_events(0.3)
                    self._kline_loaded.add(product_code)
                    logger.info("歷史 K 線備援請求成功 %s（RequestKLineAM）", product_code)
                    return True
            logger.warning("歷史 K 線請求失敗 %s: %s", product_code, msg)
            self._kline_failed.add(product_code)
            return False
        return False

    def subscribe_quote(self, product_code: str, *, wait_timeout: float = 120.0) -> bool:
        """
        官方 GUI / 文件順序：
          EnterMonitorLONG → 等 3003 → RequestStockList
          → RequestStocks（即時報價）→ RequestTicks（成交明細）→ 可選 GetStockByNoLONG 快照
        """
        if not self._quote_monitoring:
            self.enter_monitor()
        if not self._wait_quote_connected(timeout=wait_timeout):
            logger.warning(
                "報價商品檔尚未下載完成 (3003)，略過 RequestStockList/RequestStocks/RequestTicks"
            )
            self.live.add_message("quote_conn", "等待 3003 逾時，尚未訂閱即時報價")
            # 重置狀態，讓下次呼叫 subscribe_quote 會重新 EnterMonitorLONG，
            # 而不是卡在同一個從未收到 3003 的 monitor session 裡原地打轉。
            self._quote_monitoring = False
            return False

        self.live.add_message("quote_conn", "商品檔下載完成，開始訂閱即時報價")
        list_code = int(self.quote.SKQuoteLib_RequestStockList(2))
        if list_code != 0:
            logger.warning("RequestStockList(期貨) 回傳: %s", self._msg(list_code))
        self._pump_events(1.0)

        # 1) 即時報價（官方主路徑）
        stocks_code = self.request_stocks(product_code, page=1)
        if stocks_code != 0:
            logger.warning(
                "RequestStocks %s 回傳: %s", product_code, self._msg(stocks_code)
            )
            self.live.add_message(
                "quote_conn", f"RequestStocks failed: {self._msg(stocks_code)}"
            )
        else:
            self.live.add_message("quote_conn", f"RequestStocks ok: {product_code}")
        self._pump_events(1.5)

        # 2) 成交明細（有成交才推；策略/合成 K 用）
        tick_code = self.request_ticks(product_code, page=1)
        if tick_code != 0:
            logger.warning("RequestTicks %s 回傳: %s", product_code, self._msg(tick_code))
            self.live.add_message(
                "quote_conn", f"RequestTicks failed: {self._msg(tick_code)}"
            )
        else:
            self.live.add_message("quote_conn", f"RequestTicks ok: {product_code}")

        # 3) 單次快照備援（事件尚未到時仍可能有價）
        self._snapshot_quote(product_code)
        self._pump_events(2.0)

        ok = stocks_code == 0 or tick_code == 0 or self.live.quote.last_price is not None
        if not ok:
            logger.warning(
                "subscribe_quote 失敗：RequestStocks=%s RequestTicks=%s last=%s",
                stocks_code,
                tick_code,
                self.live.quote.last_price,
            )
        return ok

    @staticmethod
    def _unpack_com_stock_result(res: Any, default_stock: Any) -> tuple[int, Any]:
        """動態解包 comtypes 回傳的 (code, stock) 或 (stock, code) 元組。"""
        if isinstance(res, tuple) and len(res) >= 2:
            if isinstance(res[0], int):
                return int(res[0]), res[1]
            if isinstance(res[1], int):
                return int(res[1]), res[0]
            return 0, res[0]
        if isinstance(res, int):
            return int(res), default_stock
        return 0, default_stock

    def _snapshot_quote(self, product_code: str) -> None:
        """以 GetStockByNoLONG 取單次快照（事件尚未進來時的備援）。"""
        try:
            stock = self._sk.SKSTOCKLONG()
            res = self.quote.SKQuoteLib_GetStockByNoLONG(product_code, stock)
            n_code, populated_stock = self._unpack_com_stock_result(res, stock)
            if int(n_code) != 0:
                logger.warning(
                    "GetStockByNoLONG %s: %s", product_code, self._msg(int(n_code))
                )
                return
            self._apply_stock_quote(populated_stock, source="GetStockByNoLONG")
        except Exception as exc:  # noqa: BLE001
            logger.warning("snapshot quote failed %s: %s", product_code, exc)

    def wait_for_quote_price(self, timeout: float = 30.0) -> bool:
        """等到 last_price 有值（RequestStocks / Ticks / 快照任一來源）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.live.quote.last_price is not None:
                return True
            self._pump_events(0.2)
        return self.live.quote.last_price is not None


    def try_load_kline_history(self, product_code: str, *, bar_limit: int = 500) -> bool:
        try:
            from ..trading_service import resolve_quote_product_code
        except ImportError:
            from trading_service import resolve_quote_product_code  # type: ignore

        quote_code = resolve_quote_product_code(product_code)
        if quote_code in self._kline_loaded:
            return True
        if quote_code in self._kline_failed:
            return False
        if not self._stocks_ready:
            return False
        return self._request_kline_history_when_ready(quote_code, bar_limit=bar_limit)

    def get_order_report(self, account: str, report_type: int = 2) -> str:
        """查詢委託回報。report_type: 1全部 2有效 3可消 4已消 5已成 6失敗…"""
        self._require_ready()
        result = self.order.GetOrderReport(self.user_id, account, int(report_type))
        self.live.orders_raw = str(result)
        return self.live.orders_raw

    def get_fulfill_report(self, account: str, report_type: int = 1) -> str:
        """查詢成交回報。report_type: 1全部 2合併同書號…"""
        self._require_ready()
        result = self.order.GetFulfillReport(self.user_id, account, int(report_type))
        self.live.fulfills_raw = str(result)
        return self.live.fulfills_raw

    def get_open_interest(self, account: str, n_format: int = 1) -> int:
        """
        查詢期貨未平倉（結果由 OnOpenInterest 事件回傳）。
        官方文件《7.下單-國內期選.docx》GetOpenInterestGW 參數為 nFormat（回傳格式），
        目前文件與官方範例都只用 1（官方範例：GetOpenInterestGW(userID, account, 1)）。
        先前這裡誤傳 0（原註解自稱「0=當日成交即時持倉／1=未平倉留倉」是沒有查證過的
        錯誤假設，官方文件根本沒有這個 0/1 語意），導致查回來的持倉資料是錯的格式，
        持倉表格因此永遠顯示「無持倉」。
        """
        self._require_ready()
        return int(self.order.GetOpenInterestGW(self.user_id, account, int(n_format)))

    def get_future_rights(self, account: str, coin_type: int = 0) -> int:
        """查詢國內期貨權益數（結果由 OnFutureRights 事件回傳）。"""
        self._require_ready()
        return int(self.order.GetFutureRights(self.user_id, account, int(coin_type)))

    def refresh_live_snapshot(self, account: str | None = None, *, force: bool = False) -> None:
        """
        刷新委託/成交/持倉/權益快照。
        2026-08-29 實測驗證（3 輪、每輪 12 次、每秒查一次 GetOpenInterestGW，
        結果完全一致）：查太快會拿到同步錯誤碼 1019，成功查詢之間的間隔穩定
        是 6 秒，不是舊註解寫的「M999：至少 5 秒」——那句話是這個專案第一次
        `git init` 之前就存在的舊假設，沒有查到出處、也沒人驗證過，而且原本
        用 5.0 當節流門檻，比實測的 6 秒還短，代表舊邏輯偶爾還是會踩到
        1019（提早 1 秒重查）。改用 6.5（實測值 + 0.5 秒安全邊際）。如果之後
        要調緊，記得先跑一次一樣的實測，不要再憑舊註解猜。
        """
        account = account or self.live.active_account
        if not account:
            return
        now = time.time()
        if not force and now - self._last_snapshot_query_at < 6.5:
            self._pump_events(0.3)
            return
        self._last_snapshot_query_at = now
        self.get_order_report(account, report_type=1)
        self.get_fulfill_report(account, report_type=1)
        self.get_open_interest(account, n_format=1)
        self.get_future_rights(account, coin_type=0)
        self._pump_events(2.0)

    def get_live_state(self) -> dict[str, Any]:
        return {
            "connected": self.live.connected,
            "user_id": self.live.user_id,
            "environment": self.live.environment,
            "accounts": list(self.live.accounts),
            "active_account": self.live.active_account,
            "subscribed_product": self.live.subscribed_product,
            "quote": {
                "product_code": self.live.quote.product_code,
                "last_price": self.live.quote.last_price,
                "bid": self.live.quote.bid,
                "ask": self.live.quote.ask,
                "volume": self.live.quote.volume,
                "updated_at": self.live.quote.updated_at,
            },
            "positions": list(self.live.positions),
            "positions_raw": self.live.positions_raw,
            "open_interest_query_status": self.live.open_interest_query_status,
            "orders_raw": self.live.orders_raw,
            "fulfills_raw": self.live.fulfills_raw,
            "rights_raw": self.live.rights_raw,
            "rights": dict(self.live.rights),
            "agreements": list(self.live.agreements),
            "kline_api_ok": self.live.kline_api_ok,
            "kline_api_block_reason": self.live.kline_api_block_reason,
            "kline_loaded_products": sorted(self._kline_loaded),
            "quote_ready": self._stocks_ready,
            "order_product_code": self.live.order_product_code,
            "quote_connected": self._quote_connection_status(),
            "messages": list(self.live.messages[-50:]),
            "last_error": self.live.last_error,
        }

    def pump_events(self, seconds: float) -> None:
        """在需要等待 COM 事件回傳（帳號、委託回報、即時報價）時呼叫，會阻塞目前執行緒。"""
        self._pump_events(seconds)

    # ------------------------------------------------------------------
    # 內部工具
    # ------------------------------------------------------------------
    def _pump_events(self, seconds: float) -> None:
        import pythoncom

        deadline = time.time() + max(seconds, 0.0)
        while time.time() < deadline:
            # 模擬 tkinter mainloop：持續派送 Windows/COM 訊息
            pythoncom.PumpWaitingMessages()
            time.sleep(0.01)

    def _msg(self, code: int) -> str:
        try:
            return self.center.SKCenterLib_GetReturnCodeMessage(code)
        except Exception:  # noqa: BLE001 - 查詢訊息失敗不應中斷主要流程
            return str(code)

    def _parse_open_interest(self, raw: str) -> None:
        try:
            from ..capital_parse import parse_open_interest_raw
        except ImportError:
            from capital_parse import parse_open_interest_raw  # type: ignore

        # 2026-08-30 實盤事故：這裡原本假設「OnOpenInterest 每次推送的都是
        # 完整快照」，每次呼叫都無條件覆寫 self.live.positions——但用真實
        # position_audit.log 逐筆核對過，群益對「一次查詢」的回應其實是分成
        # 好幾次獨立事件呼叫送過來的：一筆真正的部位資料先送一次，接著幾
        # 毫秒後再送一次「##,,,,...」查詢結束終止符。舊邏輯每次都覆寫，
        # 代表終止符那次呼叫（解析不出任何部位）會把剛剛才寫進去、真正
        # 存在的部位資料整個蓋掉，導致 self.live.positions 有極高比例的時間
        # 都是空的——不管是持倉表格畫面、還是 reconcile_after_manual_close
        # 讀到的，都可能是這個「假空倉」，不是真的空倉。
        #
        # 改成緩衝＋提交：每次呼叫先把這次解析到的部位列（可能是 0 筆或多筆）
        # 累加進緩衝區，只有在收到終止符（"##" 開頭）或空字串時，才把緩衝區
        # 的內容正式提交進 self.live.positions、並清空緩衝區給下一次查詢用。
        # 稽核 log 維持每次呼叫都記一筆（記這次呼叫本身解析到的內容，不是
        # 提交後的值），保留完整的原始封包歷史，方便事後逐筆核對。
        text = raw.strip()
        rows = parse_open_interest_raw(raw)
        self._open_interest_buffer.extend(rows)
        is_terminator = not text or text.startswith("##")
        if is_terminator:
            self.live.positions = list(self._open_interest_buffer)
            self._open_interest_buffer.clear()
        _append_position_audit(account=self.user_id or "", raw=raw, positions=rows)

    def _parse_future_rights(self, raw: str) -> None:
        try:
            from ..capital_parse import parse_future_rights_raw
        except ImportError:
            from capital_parse import parse_future_rights_raw  # type: ignore

        parsed = parse_future_rights_raw(raw)
        if not parsed:
            return
        # OnFutureRights 對每個帳號各自觸發一次事件，其中非目標帳號（或無資料的帳號）
        # 常常是全空白欄位（權益數等皆為 None）。不篩選帳號的話，空白事件會直接覆蓋掉
        # 剛更新好的真實數字，導致儀表板權益數/浮動損益等欄位一直閃爍回「—」。
        account_no = str(parsed.get("account_no") or "").strip()
        target = str(self.live.active_account or "").strip()
        if target and account_no and account_no != target:
            return
        if parsed.get("equity") is None and self.live.rights.get("equity") is not None:
            return
        self.live.rights = parsed

    def _ingest_kline_row(self, product_code: str, raw: str) -> None:
        try:
            from ..kline_engine import get_store
        except ImportError:
            from kline_engine import get_store  # type: ignore

        get_store(product_code).ingest_kline_row(raw)

    def _evaluate_kline_api_agreement(self) -> None:
        """依 OnShowAgreement 內容判斷歷史 K 線 API 是否可用。"""
        keywords = ("API", "行情", "證券", "期貨")
        unsigned_markers = ("未簽", "尚未簽", "未簽署", "尚未簽署", "未同意")
        signed_markers = ("已簽", "已簽署", "已同意")

        api_rows = [
            row for row in self.live.agreements
            if any(k in row for k in keywords)
        ]
        if not api_rows:
            return

        for row in api_rows:
            if any(m in row for m in unsigned_markers):
                self.live.kline_api_ok = False
                self.live.kline_api_block_reason = row
                logger.warning("歷史 K 線 API 同意書未簽: %s", row)
                return
            if any(m in row for m in signed_markers) and "API" in row.upper():
                self.live.kline_api_ok = True
                self.live.kline_api_block_reason = ""
                logger.info("歷史 K 線 API 同意書已簽: %s", row)
                return

    def _on_tick_for_kline(
        self, price: float, volume: int, n_date: int, n_timehms: int
    ) -> None:
        if price <= 0:
            return
        product = self.live.quote.product_code or self.live.subscribed_product
        if not product:
            return
        try:
            from ..kline_engine import get_store
            from ..timeutil import TAIPEI_TZ
        except ImportError:
            from kline_engine import get_store  # type: ignore
            from timeutil import TAIPEI_TZ  # type: ignore

        # n_date<=0（如 request_stocks() 輪詢路徑）沒有券商給的交易所時間可用，
        # 不能退回 pd.Timestamp.now()：那是讀取作業系統本地時區的 naive 時間戳，
        # 若部署機器（例如全新 GCP VM，預設常常是 UTC）系統時區沒設成台灣，
        # 就會混進時間標錯的 K 棒。改用 datetime.now(TAIPEI_TZ)，不管作業系統
        # 時區設定為何都能得到正確的台灣當地時間。
        ts = (
            _capital_tick_timestamp(n_date, n_timehms)
            if n_date > 0
            else pd.Timestamp(datetime.now(TAIPEI_TZ))
        )
        get_store(product).on_tick(price, volume, ts)

    def _require_ready(self) -> None:
        if not self._logged_in:
            raise RuntimeError("尚未登入，請先呼叫 login()")
        if not self._order_initialized:
            raise RuntimeError("尚未初始化下單元件，請先呼叫 initialize_order()")
