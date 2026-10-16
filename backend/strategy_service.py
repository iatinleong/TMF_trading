"""
strategy_service.py — 實盤自動策略（4 個獨立方向策略，可同時勾選啟用）

對齊 scripts/run_four_strategies.py 的 4 個獨立方向策略：
- breakout_long  / breakout_short：突破系統，只做多 / 只做空
- pullback_long  / pullback_short：回測（pullback）系統，只做多 / 只做空
每個策略各自追蹤自己的虛擬倉位（進場後同向訊號忽略、反向訊號出現時平倉——單邊限定，
不會反手做另一邊；另外每筆留倉都有獨立的浮動損益停損，見 _tick_one 開頭），彼此獨立風控。
因為多個策略可能同時交易同一商品，broker 回報的實際淨倉位無法拿來判斷單一策略
自己的進出場狀態，所以用 StrategyState.held_qty/held_direction 各自記帳。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import pandas as pd

from .capital_parse import parse_order_report_raw
from .config import DEFAULT_STRATEGY, ContractSpec, app_base_dir
from .kline_engine import get_kline_store, get_store
from .indicators import add_moving_averages, resample_to_60min
from .signals import generate_breakout_signals, generate_death_cross_signals, generate_pullback_signals
from .timeutil import TAIPEI_TZ, to_unix_seconds
from .trading_service import TradingService, compute_current_tmf_code

logger = logging.getLogger(__name__)

BASE_DIR = app_base_dir(__file__)
DATA_DIR = BASE_DIR / "data"

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "TX": ContractSpec(name="TX", point_value=200.0),
    "MTX": ContractSpec(name="MTX", point_value=50.0),
    "TMF": ContractSpec(name="TMF", point_value=10.0),
}

STRATEGY_DEFS: dict[str, dict[str, str]] = {
    "breakout_long": {"strategy": "breakout", "direction_limit": "long", "label": "突破做多"},
    "breakout_short": {"strategy": "breakout", "direction_limit": "short", "label": "突破做空"},
    "pullback_long": {"strategy": "pullback", "direction_limit": "long", "label": "回測做多"},
    "pullback_short": {"strategy": "pullback", "direction_limit": "short", "label": "回測做空"},
    "death_cross_short": {"strategy": "death_cross", "direction_limit": "short", "label": "死叉做空"},
}


def _interval_minutes_for_strategy(strategy: str) -> int:
    return 15 if strategy == "death_cross" else 60


def _min_bars_required_for_strategy(strategy: str) -> int:
    return DEFAULT_STRATEGY.ma_mid + 2 if strategy == "death_cross" else DEFAULT_STRATEGY.ma_slow + 2


def _bar_session_key(ts: pd.Timestamp) -> tuple:
    """判斷一根 K 棒收盤時間屬於哪個「盤」：日盤（08:45~13:45）或夜盤
    （15:00~次日 05:00，跨日算同一盤）。只看時分，不受日期本身跨假日影響，
    用來讓下面的缺口偵測可以放行「換盤」造成的正常大跳躍（例如 13:45→16:00、
    或跨假日的 05:00→下個交易日 09:45），只抓「同一盤內漏了中間幾根」的異常缺口。"""
    t = pd.Timestamp(ts).time()
    if t <= dt_time(6, 0):
        # 00:00~05:00 這段，算進「前一天晚上開始」的那個夜盤
        return (pd.Timestamp(ts).normalize() - pd.Timedelta(days=1), "night")
    if t < dt_time(15, 0):
        return (pd.Timestamp(ts).normalize(), "day")
    return (pd.Timestamp(ts).normalize(), "night")


def _now() -> pd.Timestamp:
    """牆上時鐘現在時間（台灣本地時間）——拉成獨立函式只是為了讓測試可以 patch 掉，
    不用真的等現實時間流逝，見 _tick_one 裡的訊號時效性檢查。
    錨定 TAIPEI_TZ，避免 GCP Windows VM 若 OS 預設為 UTC 時與 naive 台灣時間差 8 小時（見 gotchas 第 5 節）。"""
    return pd.Timestamp.now(tz=TAIPEI_TZ).tz_localize(None)


def _session_first_bar_time(session: str, interval_minutes: int) -> dt_time:
    """某一盤第一根K棒的收盤時間：日盤 08:45 開盤、夜盤 15:00 開盤（跟
    kline_engine.bar_close_time_from_ts 的 session_start 定義一致），第一根收盤
    時間就是「開盤時間 + 一個週期」。刻意用 interval_minutes 動態算，不要寫死
    60分K的 16:00/09:45——這個專案的 death_cross 策略用的是 15 分K，第一根夜盤
    收盤應該是 15:15，寫死 60 分K的時間會把 15 分K 的換盤判斷全部搞錯。"""
    start_minutes = (8 * 60 + 45) if session == "day" else (15 * 60)
    total = (start_minutes + interval_minutes) % (24 * 60)
    return dt_time(total // 60, total % 60)


# 2026-10-06 實盤事故：客戶回報 TradingView 在 18:00 就該觸發的黃金交叉，實盤卻拖到
# 22:00:03 才送單。事後查證：VM 當天 17:48~19:13 被手動 suspend/stop/start，後端
# 20:20 才重新登入群益，使用者 21:53 才在儀表板手動武裝 breakout_long；而「真正」
# 連續無缺的 60 分 K 在 18:00 那根就已經黃金交叉（5MA=50061.00 > 20MA=50050.20，
# 跟 TradingView 數字完全吻合）。問題出在：後端剛連回群益、K 棒快取還沒完全補齊
# 這段期間，_load_bars 回傳的 bars 序列中間可能缺了幾根（16:00~20:00 這幾根要等
# 群益的歷史K棒查詢陸續回補，Shioaji 備援整晚查三天都是空資料，完全沒補上），
# generate_breakout_signals 的 .shift(1) 是跟「資料序列裡的上一列」比，不是跟「真正
# 的上一個小時」比——缺口還沒補齊時算出來的「交叉」，比較基準其實是缺口前的舊K棒，
# 等於是被缺口「借位」出來的假訊號。
#
# 2026-10-07 修正（第一版的漏洞）：第一版只拿「換盤」當理由就放行，沒檢查跳過去的
# 那一根是不是真的是新盤的第一根——這代表「日盤13:45直接跳到夜盤21:00」（中間漏了
# 16:00~20:00 五根）會被第一版誤判成正常換盤而放行，完全沒攔到。現在換盤時額外要求
# cur_ts 必須「恰好」等於新盤第一根的收盤時間，不是的話就是漏了好幾根，判定異常。
def _has_abnormal_intraday_gap(prev_ts: pd.Timestamp, cur_ts: pd.Timestamp, interval_minutes: int) -> bool:
    prev_key = _bar_session_key(prev_ts)
    cur_key = _bar_session_key(cur_ts)
    if prev_key == cur_key:
        gap_minutes = (pd.Timestamp(cur_ts) - pd.Timestamp(prev_ts)).total_seconds() / 60.0
        return gap_minutes > interval_minutes * 1.5
    # 換盤：cur 必須是新盤的第一根，否則代表進新盤後還漏了好幾根才接上。
    expected_first = _session_first_bar_time(cur_key[1], interval_minutes)
    return pd.Timestamp(cur_ts).time() != expected_first


# 2026-10-07 修正（第一版的漏洞）：第一版只檢查「訊號用到的那一對K棒」，但 MA5/MA20
# 是滾動視窗算出來的，缺口只要落在視窗裡任何一段，均線一樣會失真，不是只有「最後一對」
# 要連續才算數。這裡把檢查範圍擴大成「訊號這根往前數 window 根」（window 用
# _min_bars_required_for_strategy 對齊目前策略實際用到的 MA 根數），逐一檢查每一對
# 相鄰K棒，而不是只看最後一對。
def _has_abnormal_gap_in_recent_window(
    bars: pd.DataFrame, gap_idx: int, window: int, interval_minutes: int
) -> bool:
    start = max(1, gap_idx - window + 1)
    for i in range(start, gap_idx + 1):
        if _has_abnormal_intraday_gap(
            pd.Timestamp(bars.index[i - 1]), pd.Timestamp(bars.index[i]), interval_minutes
        ):
            return True
    return False


# 連續下單失敗達此次數就自動停止該策略（見 _tick_one）。
MAX_CONSECUTIVE_FAILURES = 3

# 2026-08-29 實盤事故：22:00:19 進場，broker 持倉快照要到 22:00:34 才第一次
# 真正更新（15 秒空窗期，已用 position_audit.log 實測數字確認），
# reconcile_after_manual_close 剛好在這個空窗期內跑，拿到還沒更新的舊快照
# 誤判「內部有、券商沒有」，22:00:26 把剛掛好的真實 OCO 保護單撤掉，部位
# 裸奔超過 18 小時。30 秒是這個實測空窗期的兩倍，留出充分餘裕。剛進場
# （entry_recorded_at 在這個時間內）的部位，reconcile 一律先跳過，不比對、
# 不清空、不撤單，見 reconcile_after_manual_close／reconcile_orphan_stop_orders。
RECONCILE_ENTRY_GRACE_SECONDS = 30.0

# 2026-09-11 實盤事故：群益 SKCOM 的報價事件（OnNotifyQuoteLONG）在跨盤
# （夜盤 05:00 收盤 → 日盤 08:45 開盤）之後沒有自動更新，st["quote"]["last_price"]
# 卡在前一盤收盤價（46,082）長達 13 小時沒變，但同一時間 K 棒（Shioaji 回補+即時
# tick 兩路來源）持續正常更新到真實市價（46,390）。官方文件（CapitalAPI SDK
# 「13.國內報價.docx」OnNotifyTicksLONG 備註）明講「T盤切換T+1盤，不保留前一盤
# 資料，開發者需自行清除前一盤資料」——SKCOM 本身不會幫你清這個。進場當下
# entry_px 優先信任這個報價變數，量到一個過期但 >0 的舊價，直接跳過後面所有
# 備援（broker 持倉快照／K棒收盤價），用這個舊價去算 OCO 停損停利觸發價，
# 導致停利觸發價還沒掛到券商那邊就已經被真實市價超過，OCO 一掛上去立刻秒觸發
# 平倉（entry_price=46,082 但真實成交在 46,390，停利觸發價只算到 46,332）。
# 這裡加一道防呆：報價跟最新K棒收盤價（可靠來源，見上方說明）偏離超過這個
# 點數，就不信任報價，強制走後面的備援鏈（等同報價缺失時的處理）。
QUOTE_STALENESS_POINTS = 50.0

# 2026-10-07 加固：券商 OCO 異常未觸發或失效時的「緊急硬穿價軟停損」緩衝點數。
# 若市價穿過停損點位超過此緩衝點數，研判券商智慧單未如期運作，強制啟動市價平倉備援。
EMERGENCY_STOP_BUFFER_POINTS = float(os.getenv("STRATEGY_EMERGENCY_STOP_BUFFER_POINTS", "10.0"))

# 2026-10-07：長效單（跨盤跨日不失效的 OCO）已透過 scripts/verify_long_action_oco.py
# 在群益正式環境實測驗證成功（送單成功進入洗價池、查得長效單序號、成功撤單完畢）。
# 正式預設開啟（True），亦可透過環境變數 USE_LONG_ACTION_PROTECTION 控制。
USE_LONG_ACTION_PROTECTION = os.getenv("USE_LONG_ACTION_PROTECTION", "1").strip().lower() in {"1", "true", "yes"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class StrategyState:
    strategy_id: str
    product_code: str
    strategy: str
    direction_limit: str
    label: str
    qty: int = 1
    initial_capital_ntd: float = 100_000.0
    max_loss_ntd: float = 10_000.0
    max_loss_pct: float = 0.10
    stop_loss_points: float = 100.0
    take_profit_points: float = 250.0
    realized_pnl_ntd: float = 0.0
    held_qty: int = 0
    held_direction: str | None = None
    entry_price: float = 0.0
    # 2026-08-29 實盤事故：22:00:23 掛好的真實 OCO 保護單，22:00:26（只隔 3
    # 秒）就被 reconcile_after_manual_close 誤判「內部有、券商沒有」撤掉，部位
    # 裸奔超過 14 小時——根因是 svc.status() 讀的是快取（靠 OnOpenInterest 事件
    # 被動更新），剛進場那一刻快取還沒跟上，reconcile 立刻拿過期快照去比對，
    # 一定會誤判。這個欄位記下「這筆倉位是什麼時候記進來的」，
    # reconcile_after_manual_close 靠它跳過剛進場、寬限期內的部位，見該函式
    # 說明。None 代表不知道進場時間（例如測試手動建構、或程式重啟後遺留的
    # 舊狀態），一律當作寬限期已過，照舊立即核對。
    entry_recorded_at: float | None = None
    # 2026-08-29 Gemini review 抓到的漏洞：reconcile_orphan_stop_orders 原本
    # 也是拿 entry_recorded_at 去算寬限期——但 reconcile_after_manual_close
    # 只有在寬限期「已經過了」才會清零 held_qty，代表走到 reconcile_orphan_
    # stop_orders 時，用同一個 entry_recorded_at 算出來的寬限期一定也已經
    # 過了，形同沒有寬限期，同一輪就會立刻撤單。這個欄位記下「held_qty 是
    # 什麼時候被 reconcile_after_manual_close 清零的」，讓 reconcile_orphan_
    # stop_orders 從這個時間點重新起算一段獨立的寬限期，而不是沿用已經
    # 過期的舊時鐘。
    held_qty_cleared_at: float | None = None
    # 券商端 OCO（二擇一）保護單追蹤（見 _place_oco_protection_for_state）。
    # 有值代表這筆留倉已經有真正掛在券商那邊的停損＋停利保護（單一委託同時
    # 帶兩支腿），_tick_one 的軟停損／軟停利檢查都會讓路給它，不會兩邊搶著
    # 平倉。None 代表沒有真正保護（下單失敗或非 TMF 商品），這種情況才會用
    # 軟停損停利頂著。
    #
    # 2026-08-27：原本分開追蹤 stop_order_*／tp_order_* 兩組欄位（對應分開送
    # STP+MIT 兩張獨立平倉智慧單），但那個做法會被券商拒絕第二張（[999]
    # 勾選平倉而留倉部位不足——兩張各自向券商聲請同一口的平倉額度）。改用
    # SendFutureOCOOrderV1 一次委託帶兩支腿之後，停損停利已經是同一張智慧單，
    # 只需要一組欄位追蹤、撤單也只需要撤一次。
    protection_order_smart_key: str | None = None
    protection_order_seq_no: str = ""
    protection_order_order_no: str = ""
    # 2026-10-07：長效單撤單要用「長效單序號」（跟智慧單號是不同欄位，見官方文件
    # 《8.下單-國內期選智慧單》），只有在下單時明確開啟長效單（long_action_flag=True）
    # 才會去查、才會有值；非長效單維持 None，撤單時照舊傳空字串。
    protection_order_long_action_key: str | None = None
    last_signal_key: str | None = None
    last_checked_at: float | None = None
    last_signal: dict[str, Any] | None = None
    stopped: bool = False
    stop_reason: str = ""
    last_action: str = ""
    consecutive_failures: int = 0
    # 2026-08-31：四道出場防護網（Layer 1 OCO／Layer 2 軟停損停利／Layer 3
    # 金額比例硬停損／Layer 4 反向訊號出場）各自獨立的開關，可依帳號/策略
    # 客製化關閉。刻意用 4 個獨立布林欄位而不是單一字串列舉——今天稍早用
    # exit_mode 字串列舉時，兩組不同用途的字串值互相撞名，導致某些帳號被
    # 靜默關掉不該關的防護層（見 docs/superpowers/specs/2026-08-31-
    # per-layer-protection-toggles-design.md）。四層完全對等，預設全部
    # True（開啟），任何既有呼叫不傳這 4 個參數時行為與過去完全一致。
    oco_enabled: bool = True
    soft_stop_enabled: bool = True
    risk_insurance_enabled: bool = True
    reverse_signal_exit_enabled: bool = True

    def loss_limit_hit(self, pnl_ntd: float | None = None) -> bool:
        pnl = self.realized_pnl_ntd if pnl_ntd is None else pnl_ntd
        if pnl <= -abs(self.max_loss_ntd):
            return True
        if self.initial_capital_ntd > 0:
            pct = pnl / self.initial_capital_ntd
            if pct <= -abs(self.max_loss_pct):
                return True
        return False


# key = strategy_id（見 STRATEGY_DEFS），可以同時有多個進入 armed 狀態
_armed: dict[str, StrategyState] = {}

# 2026-10-16：單機單帳號部署（docs/deploy_end_user_pc.md）用這個本機檔案記住「目前
# 有哪些策略是使用者自己開著的」，讓 backend process 重啟後（不管是這次修的
# VM重開機、還是任何其他原因重啟）能自動恢復成重啟前的樣子，不用使用者每次都手動
# 回儀表板重新按「啟動」。跟 _auto_arm_bound_strategies（backend/api.py）是兩套
# 不同機制：那個是給「多帳號共用一台機器、WORKER_MODE=1」的部署模式用，從 Supabase
# 讀設定，而且會連帶關掉這個行程自己的 K 線回補（假設有別的行程負責）——這台單帳號
# VM 沒有「別的行程」，不能沿用那套，所以另外用一個單純的本機檔案。
_ARMED_SNAPSHOT_PATH = DATA_DIR / "armed_strategies.json"
_ARMED_SNAPSHOT_FIELDS = (
    "qty", "stop_loss_points", "take_profit_points", "max_loss_ntd", "max_loss_pct",
    "oco_enabled", "soft_stop_enabled", "risk_insurance_enabled", "reverse_signal_exit_enabled",
)


def _persist_armed_snapshot() -> None:
    """把目前「還在開著、沒被使用者停止」的策略設定寫到本機檔案，供下次啟動時
    resume_armed_strategies_from_snapshot() 讀回來自動恢復。故意只存 start_strategy
    會用到的設定值，不存 held_qty/entry_price/protection_order_* 這些執行期狀態——
    那些本來就該靠 reconcile_after_manual_close/reconcile_orphan_stop_orders 跟券商
    端現狀重新核對，不能憑舊的本機檔案假裝記得，會跟實際留倉脫鉤。"""
    try:
        rows = []
        for state in _armed.values():
            if state.stopped:
                continue  # 使用者已經按停止、只是還在等留倉平掉，不算「開著」
            row = {"strategy_id": state.strategy_id, "product_code": state.product_code}
            row.update({f: getattr(state, f) for f in _ARMED_SNAPSHOT_FIELDS})
            rows.append(row)
        _ARMED_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ARMED_SNAPSHOT_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # 寫這個檔案純粹是「重啟後體驗更好」的加分項，絕對不能讓它的失敗影響
        # start_strategy/stop_strategy 本身（那兩個函式才是真的在管理真錢部位）。
        logger.warning("寫入 armed_strategies.json 快照失敗（不影響本次操作）: %s", exc)


def resume_armed_strategies_from_snapshot() -> int:
    """開機時呼叫：讀回上次重啟前「還在開著」的策略設定，用跟手動按啟動完全一樣的
    start_strategy() 重新 arm 起來。回傳成功恢復的策略數量。AUTO_RESUME_STRATEGIES
    環境變數預設開啟（"1"），可設成 "0" 關閉、改回每次都要手動到儀表板重新啟動。"""
    if os.getenv("AUTO_RESUME_STRATEGIES", "1").strip().lower() in {"0", "false", "no"}:
        return 0
    if not _ARMED_SNAPSHOT_PATH.is_file():
        return 0
    try:
        rows = json.loads(_ARMED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取 armed_strategies.json 快照失敗，略過自動恢復: %s", exc)
        return 0

    resumed_ids: list[str] = []
    for row in rows:
        strategy_id = row.get("strategy_id")
        if not strategy_id or strategy_id not in STRATEGY_DEFS:
            continue
        try:
            start_strategy(
                strategy_id,
                row.get("product_code"),
                **{f: row.get(f) for f in _ARMED_SNAPSHOT_FIELDS},
            )
            resumed_ids.append(strategy_id)
            logger.info("已從重啟前的快照自動恢復策略: %s", strategy_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自動恢復策略 %s 失敗: %s", strategy_id, exc)

    if resumed_ids:
        _pause_resumed_strategies_with_unclaimed_broker_positions(resumed_ids)
    return len(resumed_ids)


def _pause_resumed_strategies_with_unclaimed_broker_positions(resumed_strategy_ids: list[str]) -> None:
    """2026-10-16 加固（Gemini review 抓到的真實漏洞，讀過 reconcile_after_manual_
    close／reconcile_orphan_stop_orders 的程式碼驗證過）：resume_armed_strategies_
    from_snapshot() 重新 arm 起來的策略一律從 held_qty=0 開始（本機快照故意不存
    執行期狀態，見 _persist_armed_snapshot 的說明），但如果重啟前那一刻券商端其實
    還握著一筆真倉位（process 剛好在持倉中被重開機打斷），現有的兩個 reconcile
    函式都只處理「內部有、券商沒有」這個方向，完全沒有反過來比對「內部沒有、券商
    有」——放著不管，策略會誤判自己空手，可能對同一方向再送一次進場單（曝險
    加倍），軟停損/風控也完全不會替那筆舊倉位計算損益，保護單的智慧單號也早就
    遺失、無法自動撤銷。

    這裡保守處理：偵測到就不讓那個策略去送新倉單，先停在這裡、大聲記警告，等
    人工確認實際部位跟保護單之後再手動重新啟動——不嘗試猜測怎麼接管（同一商品
    理論上不會有兩個策略同時認領，但故意不賭這個假設一定成立）。"""
    try:
        st = TradingService.get().status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume 後檢查券商留倉失敗，略過這次孤兒倉位檢查: %s", exc)
        return

    broker_qty: dict[tuple[str, str], int] = {}
    for pos in st.get("positions") or []:
        direction_key = pos.get("direction_key")
        if direction_key not in {"long", "short"}:
            continue
        prod = _canonical_position_product(str(pos.get("product") or ""))
        try:
            qty = int(float(str(pos.get("qty", "0")).replace(",", "")))
        except ValueError:
            qty = 0
        key = (prod, direction_key)
        broker_qty[key] = broker_qty.get(key, 0) + qty

    paused_any = False
    for strategy_id in resumed_strategy_ids:
        state = _armed.get(strategy_id)
        if state is None or state.held_qty > 0:
            continue
        key = (_canonical_position_product(state.product_code), state.direction_limit)
        actual = broker_qty.get(key, 0)
        if actual <= 0:
            continue
        reason = (
            f"⚠️ 重啟後偵測到券商端有未被認領的留倉（{key[0]}/{key[1]} x{actual}），"
            "本機重啟前的狀態不明，已暫停此策略避免重複進場或保護失效，請人工核對"
            "實際部位與保護單後再手動重新啟動。"
        )
        _stop_with_reason(state, reason)
        logger.critical("resume_armed_strategies_from_snapshot[%s]: %s", strategy_id, reason)
        paused_any = True

    if paused_any:
        _persist_armed_snapshot()

# 各策略的實際成交紀錄（進場/出場），跟 _armed 分開存：策略停止後歷史紀錄仍保留，
# 給圖表畫「真正下單」的箭頭用，跟 klines_with_signals 那種「幾何上發生過的訊號」不同。
_trade_log: dict[str, list[dict[str, Any]]] = {}


def _record_trade(
    strategy_id: str,
    *,
    kind: str,
    bar_time: pd.Timestamp,
    direction: str,
    price: float,
    pnl: float | None = None,
) -> None:
    _trade_log.setdefault(strategy_id, []).append(
        {
            "strategy_id": strategy_id,
            "type": kind,  # "entry" | "exit"
            "time": to_unix_seconds(bar_time),
            "direction": direction,
            "price": price,
            "pnl": pnl,
        }
    )


# 委託狀態代碼裡，代表「broker 端有受理/成交跡象」的集合（詳見 capital_parse.py
# 的官方欄位定義）：2全部成交 4部分成交剩餘已取消 5部分成交剩餘可取消
# 7委託成功待成交 8取消失敗(原單還在) 9取消中(原單還在) F3動態退單-部分委託成功
_ORDER_HAS_FILL_EVIDENCE_CODES = {"2", "4", "5", "7", "8", "9", "F3"}


def _check_orphaned_fill(
    live_state: dict[str, Any], *, expected_side: str, qty: int, within_seconds: float = 30.0
) -> bool:
    """
    2026-07-27 事故：`svc.place_order()` 回報下單失敗（`success=False`），
    但券商端 SKCOM log 明確顯示同一筆委託其實成功成交——導致策略以為自己
    空手，實際上帳戶有一筆完全沒被追蹤、沒有任何停損保護的真實留倉。

    這個函式不嘗試猜測「這筆單該算進哪個策略的 held_qty」（多個策略可能同時
    交易同一商品，猜錯了反而更亂），只負責偵測風險：下單「失敗」之後，去看
    最新的 orders_raw 裡有沒有一筆方向、口數都相符、且狀態顯示有受理/成交
    跡象的委託，是在最近 `within_seconds` 秒內送出的。有的話代表很可能就是
    這次的委託其實成功了，呼叫端應該要用最大聲的方式停止策略、通知使用者
    人工核對，而不是把它當一般拒單繼續往下跑。
    """
    orders_raw = str((live_state or {}).get("orders_raw") or "")
    if not orders_raw:
        return False

    now = time.time()
    for row in parse_order_report_raw(orders_raw):
        if row.get("direction_key") != expected_side:
            continue
        try:
            row_qty = int(float(str(row.get("qty") or "0")))
        except ValueError:
            continue
        if row_qty != qty:
            continue
        status_code = str(row.get("status_code") or "")
        if status_code not in _ORDER_HAS_FILL_EVIDENCE_CODES:
            continue

        raw_line = str(row.get("raw") or "")
        parts = [p.strip() for p in raw_line.split(",")]
        if len(parts) <= 12:
            continue
        try:
            # 注意：不能用 pd.to_datetime(...).timestamp()，naive Timestamp 會被
            # pandas 當成 UTC 換算，跟 time.time()（真正的 UTC epoch）比對台北
            # 本地時間字串會整整差 8 小時。datetime.strptime(...).timestamp()
            # 對 naive datetime 是用系統本地時區換算，才會跟 time.time() 一致。
            ts = datetime.strptime(f"{parts[11]} {parts[12]}", "%Y%m%d %H%M%S").timestamp()
        except ValueError:
            continue
        if abs(now - ts) <= within_seconds:
            return True
    return False


def _canonical_position_product(product_code: str) -> str:
    """
    2026-08-27 實盤事故根因：策略內部用 state.product_code 記帳（TMF 具體
    月份碼像 "TM2609"），但群益 OnOpenInterest（GetOpenInterestGW）回報同一張
    持倉的 product 欄位卻是不含年份的短碼 "TM09"（見
    backend/capital_parse.py::parse_open_interest_line）。reconcile_after_
    manual_close() 原本直接拿兩邊字串比對 key，永遠對不起來，把明明對得上的
    真實持倉誤判成「內部記錄超過券商實際回報」，觸發自動撤單——連已經成功
    掛上的真實 STP 停損智慧單都被撤掉，留下裸倉超過半小時才被使用者發現。
    這裡統一轉成券商回報用的短碼格式再比對，兩邊都過這個函式，不管哪一種
    格式進來都能對上。非 TM 具體月份碼（TMFR1/TX00 等連續代碼）原樣返回。
    """
    match = re.fullmatch(r"TM\d{2}(\d{2})", product_code.strip().upper())
    if match:
        return f"TM{match.group(1)}"
    return product_code.strip().upper()


def _opposite_direction_position_exists(
    product_code: str, direction: str, *, exclude_strategy_id: str | None = None
) -> bool:
    """
    2026-09-08 實盤事故根因：群益（跟大多數台灣期貨帳戶一樣）同一帳戶、同一商品
    不支援真正的雙向留倉——即使送出的是「新倉」（new_close=0），只要方向跟既有
    持倉相反，券商還是會直接拿新單去沖銷舊部位，而不是變成兩筆各自獨立的部位。
    當天實測：breakout_short 抱著空單時，breakout_long 看到多頭訊號送出買進新倉單，
    直接把空單沖銷成空手，緊接著 breakout_long 自己的 OCO 保護單又因為「留倉部位
    不足」被拒單（[999]）——這筆多單因此完全沒有保護。

    這裡在任何策略準備開新倉前先檢查：目前武裝中的策略裡，有沒有「同一個商品、
    方向剛好相反、而且目前真的持倉中」的——有的話直接跳過這輪進場，不送出會被
    券商淨沖銷掉的委託，等對方真正靠 SL/TP/風控出場之後才放行。
    """
    opposite = "short" if direction == "long" else "long"
    target_product = _canonical_position_product(product_code)
    for other in _armed.values():
        if exclude_strategy_id is not None and other.strategy_id == exclude_strategy_id:
            continue
        if other.held_qty <= 0 or other.held_direction != opposite:
            continue
        if _canonical_position_product(other.product_code) != target_product:
            continue
        return True
    return False


def _settlement_month_from_tmf_code(product_code: str) -> str | None:
    """
    從 "TM2609" 這類 TMF 具體月份碼推導 STP 智慧單要用的 YYYYMM（"202609"）。
    STP 智慧單目前只實測驗證過 TMF；大台/小台常用的是 "TX00"/"MXFR1" 這種
    連續代碼，不是具體月份碼，沒辦法這樣推導，回傳 None 讓呼叫端跳過（改用
    軟停損頂著），不要對著沒驗證過的商品格式亂送真實委託。
    """
    match = re.fullmatch(r"TM(\d{2})(\d{2})", product_code.strip().upper())
    if not match:
        return None
    yy, mm = match.groups()
    return f"20{yy}{mm}"


def _place_oco_protection_for_state(
    state: StrategyState, svc: TradingService, *, reference_price: float | None = None
) -> None:
    """
    進場成交後呼叫：用單一 OCO（二擇一）智慧單，一次委託同時掛停損＋停利兩支
    腿保護這筆留倉。失敗（或非 TMF 商品）就放著 protection_order_smart_key=
    None，_tick_one 的軟停損停利檢查會接手頂著，不是「這筆單完全沒保護」。

    2026-08-27 實盤事故根因：原本分開送 STP+MIT 兩張獨立平倉智慧單，第二張
    被券商拒絕（[999] 勾選平倉而留倉部位不足）——兩張各自向券商聲請同一口
    的平倉額度，只能滿足其中一張。改用 SendFutureOCOOrderV1 一次委託帶兩支
    腿，用真實帳號驗證過不會再被拒（正式環境+遠離市價不會觸發的限價驗證，
    見 docs/order_execution_logic.md 第 8 節）。

    實測驗證過兩個官方文件沒寫清楚的規則：
    1. OCO 第一腳觸發價必須比第二腳高（[999]「OCO第二隻腳觸發價不能大於等於
       第一隻腳」），是純數值大小排序，不是「哪一腳是停損/停利」的語意順序
       ——多單空單哪一邊比較高會互換，所以這裡照數值排序、不是照方向指定。
    2. order_price_type=3（範圍市價）配 "P" 在 ROD 下會被拒（[519] 需要
       實際價差，格式未驗證過）；改用 order_price_type=2（限價）+ 實際數字
       價格，跟原本 STP/MIT 已經驗證過能用的方式一致。

    2026-09-11 加固：`reference_price`（呼叫端傳入，通常是最新K棒收盤價，見
    QUOTE_STALENESS_POINTS 說明）非 None 時，最後再做一次現價防呆——不管
    entry_price 是怎麼來的，只要算出來的觸發價已經在現價的「錯誤那一側」
    （多單停利價 <= 現價、或多單停損價 >= 現價，空單則相反），代表這張
    OCO 一掛上去就會立刻被現價觸發，直接用「現價 ± 點數」重新算過，不送出
    一張還沒送到就已經注定秒觸發的保護單。
    """
    settlement_month = _settlement_month_from_tmf_code(state.product_code)
    if settlement_month is None:
        state.last_action += "（非 TMF 具體月份碼，OCO 智慧單略過，改用軟停損停利保護）"
        return

    if state.held_direction == "long":
        side = "sell"
        stop_trigger = state.entry_price - abs(state.stop_loss_points)
        take_profit_trigger = state.entry_price + abs(state.take_profit_points)
        if reference_price is not None and reference_price > 0:
            if take_profit_trigger <= reference_price:
                take_profit_trigger = reference_price + abs(state.take_profit_points)
            if stop_trigger >= reference_price:
                stop_trigger = reference_price - abs(state.stop_loss_points)
    else:
        side = "buy"
        stop_trigger = state.entry_price + abs(state.stop_loss_points)
        take_profit_trigger = state.entry_price - abs(state.take_profit_points)
        if reference_price is not None and reference_price > 0:
            if take_profit_trigger >= reference_price:
                take_profit_trigger = reference_price - abs(state.take_profit_points)
            if stop_trigger <= reference_price:
                stop_trigger = reference_price + abs(state.stop_loss_points)

    high_trigger = max(stop_trigger, take_profit_trigger)
    low_trigger = min(stop_trigger, take_profit_trigger)

    # 2026-10-07：長效單（nLongActionFlag=1）能力已在 broker 層加上，且已透過
    # scripts/verify_long_action_oco.py 在群益正式環境實測驗證完整成功（送單成功進入洗價池、
    # 成功透過 GetStopLossReport 查得長效單序號、成功撤單完畢）。
    # 目前預設開啟（USE_LONG_ACTION_PROTECTION 預設為 True）。撤單時若長效單序號尚未查得，
    # 會在每輪 tick 自動補查（見 _refresh_long_action_key_for_state）。
    use_long_action = USE_LONG_ACTION_PROTECTION

    try:
        payload = {
            "stock_no": state.product_code,
            "settlement_month": settlement_month,
            "qty": state.held_qty,
            "side": side,
            "trigger_price": f"{high_trigger:.0f}",
            "price": f"{high_trigger:.0f}",
            "side2": side,
            "trigger_price2": f"{low_trigger:.0f}",
            "price2": f"{low_trigger:.0f}",
            "order_price_type": 2,
            "new_close": "close",
        }
        if use_long_action:
            payload["long_action_flag"] = True
        result = svc.place_oco_order(payload)
        order_result = result.get("order_result") or {}
        if not order_result.get("success"):
            state.last_action += f"（OCO 停損停利智慧單掛單失敗：{order_result.get('message') or '未知原因'}，改用軟停損停利保護）"
            return
        raw = str(order_result.get("raw") or "")
        parts = [p.strip() for p in raw.split(",")]
        # 回應格式（實測確認，跟 STP/MIT 一致）：日期,訊息(含「條件單號：X」),書號,智慧單號,13碼序號
        if len(parts) >= 5:
            state.protection_order_order_no = parts[2]
            state.protection_order_smart_key = parts[3]
            state.protection_order_seq_no = parts[4]
            state.protection_order_long_action_key = None
            if use_long_action:
                state.last_action += "（已掛真實 OCO 長效停損停利智慧單，查詢長效單號中）"
                _refresh_long_action_key_for_state(state, svc)
            else:
                state.last_action += "（已掛真實 OCO 停損停利智慧單）"
        else:
            # 送單回報格式不如預期，寧可當作沒有真正保護、退回軟停損停利，也
            # 不要記一個可能解析錯誤的智慧單號，之後撤單撤錯東西。
            state.last_action += "（OCO 送單成功但回應格式無法解析，改用軟停損停利保護，請人工核對）"
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategy_tick[%s] OCO 停損停利智慧單掛單失敗: %s", state.strategy_id, exc)


def _refresh_long_action_key_for_state(state: StrategyState, svc: TradingService) -> None:
    """
    長效單送單成功的回報裡沒有長效單序號——官方文件《8.下單-國內期選智慧單》確認過，
    送單成功只拿得到智慧單號、13碼序號、書號，長效單序號要另外呼叫 GetStopLossReport
    查詢，從查詢結果裡用智慧單號比對出同一筆，再讀「長效單號」欄位（欄位位置見
    backend/capital_parse.py 的 _STOP_LOSS_REPORT_*_TAIL_FIELDS）。

    查不到、或查到的那一筆長效單號是 "0"（非長效單的預設值，理論上不該出現在這裡，
    但防呆），就維持 None，撤單時照舊傳空字串——寧可保守地當作沒有長效單號，也不要
    存一個可能查錯、對應到別筆單的號碼，那樣撤單會更危險。
    """
    if not state.protection_order_smart_key:
        return
    try:
        from .capital_parse import parse_stop_loss_report_raw
    except ImportError:
        from capital_parse import parse_stop_loss_report_raw  # type: ignore

    try:
        result = svc.query_stop_loss_report(kind="OCO")
        rows = result.get("stop_loss_rows") or parse_stop_loss_report_raw(result.get("stop_loss_raw") or "")
        for row in rows:
            if row.get("smart_key") != state.protection_order_smart_key:
                continue
            key = row.get("long_action_key")
            if key and key != "0":
                state.protection_order_long_action_key = key
            break
    except Exception as exc:  # noqa: BLE001
        # 查詢本身失敗不影響已經掛成功的 OCO 保護單，只是長效單號沒查到、
        # 維持 None，撤單時照舊傳空字串，不要覆寫 state.last_action 誤導成
        # 「OCO 掛單失敗」——OCO 本身是掛成功的，失敗的只是這次事後查詢。
        logger.warning("strategy_tick[%s] 查詢長效單序號失敗: %s", state.strategy_id, exc)


def _cancel_protection_order_for_state(state: StrategyState, svc: TradingService) -> None:
    """
    盡力撤掉這個策略名下掛著的 OCO 停損停利保護單（倉位已經用別的方式平掉，
    這張單變成孤兒單）。撤單失敗就保留 smart_key，交給
    reconcile_orphan_stop_orders() 下一輪重試，不要在這裡就清空——清空了
    等於永遠沒人知道要再撤。
    """
    if not state.protection_order_smart_key:
        return
    try:
        result = svc.cancel_stop_order(
            {
                "smart_key": state.protection_order_smart_key,
                "seq_no": state.protection_order_seq_no,
                "order_no": state.protection_order_order_no,
                # 2026-10-07：有查到長效單號才帶，查不到就不帶（trading_service 預設
                # 會填 "0"，等同非長效單的填法——不要自己猜一個號碼送出去）。
                "long_action_key": state.protection_order_long_action_key or "",
                "trade_kind": 3,  # OCO，見 SmartOrderKind.OCO
            }
        )
        cancel_result = result.get("cancel_result") or {}
        if cancel_result.get("success"):
            state.protection_order_smart_key = None
            state.protection_order_seq_no = ""
            state.protection_order_order_no = ""
            state.protection_order_long_action_key = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategy_tick[%s] OCO 保護單撤單失敗: %s", state.strategy_id, exc)


def reconcile_orphan_stop_orders() -> list[str]:
    """
    2026-08-21：每一輪策略檢查都順便清一次孤兒 OCO 保護單——留倉已經是 0、但
    還記著智慧單號的策略，代表對應的委託平倉時撤單沒成功（或還沒來得及撤），
    這裡再試一次。用意是就算某一次撤單剛好失敗（網路、券商忙線），下一輪幾秒後
    也會被抓到，不會讓孤兒單一直掛著。

    2026-08-29 實盤事故：held_qty==0 不保證這筆倉位真的已經不在了——如果是
    reconcile_after_manual_close 誤判清空的（見該函式），這裡如果照樣立刻
    撤單，等於讓那邊拿掉自動撤單的修復完全白做。所以這裡也要有寬限期。

    2026-08-29 Gemini review 抓到的漏洞：寬限期原本沿用 entry_recorded_at
    （進場時間），但 reconcile_after_manual_close 只有在「entry_recorded_at
    的寬限期已經過了」才會清零 held_qty——代表走到這裡時，用同一個時間戳
    算出來的寬限期一定也已經過期，形同沒有寬限期，同一輪就會立刻補刀撤單。
    改成優先看 held_qty_cleared_at（held_qty 被清零的那一刻，見
    reconcile_after_manual_close），從那個時間點重新起算一段獨立的 30 秒，
    沒有這個欄位（例如舊資料、或不是被那個函式清零的）才退回用
    entry_recorded_at 當備援判斷依據。
    """
    svc = TradingService.get()
    now = time.time()
    cleaned: list[str] = []
    for state in _armed.values():
        if state.held_qty != 0:
            continue
        if not state.protection_order_smart_key:
            continue
        grace_anchor = (
            state.held_qty_cleared_at
            if state.held_qty_cleared_at is not None
            else state.entry_recorded_at
        )
        if grace_anchor is not None and now - grace_anchor < RECONCILE_ENTRY_GRACE_SECONDS:
            continue
        _cancel_protection_order_for_state(state, svc)
        if not state.protection_order_smart_key:
            cleaned.append(state.strategy_id)
    return cleaned


def trade_log(strategy_id: str | None = None) -> list[dict[str, Any]]:
    if strategy_id is not None:
        return list(_trade_log.get(strategy_id, []))
    all_trades: list[dict[str, Any]] = []
    for trades in _trade_log.values():
        all_trades.extend(trades)
    all_trades.sort(key=lambda t: t["time"])
    return all_trades


def resolve_strategy_product_code(product_code: str | None = None) -> str:
    """自動將空值、通用品種代碼（TMF/MTX/TX）或過期合約代碼解析成真實可交易的群益商品代碼。
    - None / "" / "TMF" / "TM" -> 自動計算當前近月微台（例如 TM2609，結算後自動 TM2610）
    - 過期微台代碼（如 TM2608 在 9 月已過期）-> 自動修正為當前近月 TM2609
    - "MTX" / "MXF" -> 小台近月（MTX）
    - "TX" -> 大台近月（TX）
    - 具體合約代碼（如 "TM2609"）-> 保持原樣
    """
    raw = str(product_code or "").strip().upper()
    if not raw or raw in {"TMF", "TM"}:
        return compute_current_tmf_code()
    if raw in {"MTX", "MXF"}:
        return "MTX"
    if raw == "TX":
        return "TX"
    current_tmf = compute_current_tmf_code()
    if raw.startswith("TM") and len(raw) == 6 and raw[2:].isdigit():
        if raw < current_tmf:
            return current_tmf
    return raw


def _contract_from_product(product_code: str) -> str:
    upper = product_code.upper()
    # 2026-08-21 實盤稽核發現：實際活著在用的微台商品碼是 "TM"+YYMM（例如
    # "TM2609"，見 trading_service.compute_current_tmf_code），不是 "TMF"開頭，
    # 原本只認 "TMF" 前綴，導致真實微台交易全部被誤判成大台（TX，point_value
    # 200），P&L／風控保險門檻算出來多了 20 倍（200/10）。"TM" 前綴目前在這個
    # 專案裡只有微台會用到，改成認整個 "TM" 前綴。
    if upper.startswith("TM"):
        return "TMF"
    if upper.startswith("MXF") or upper.startswith("MTX"):
        return "MTX"
    return "TX"


def _bars_from_kline_store(product_code: str, interval_minutes: int = 60) -> pd.DataFrame:
    klines = get_kline_store(product_code, interval_minutes=interval_minutes).get_klines(limit=500)
    if not klines:
        return pd.DataFrame()
    frame = pd.DataFrame(klines)
    # 2026-08-30 code review 抓到的第 4 次「naive 時間戳被當成 UTC」bug（前三次
    # 見 docs/vm_deployment_gotchas.md 第 5 節）：`get_klines()` 的 "time" 是用
    # `timeutil.to_unix_seconds()` 算出來的正確 UTC 秒數（已經把台北本地時間
    # 轉換過），但這裡原本直接 `pd.to_datetime(frame["time"], unit="s")` 還原，
    # 沒有再轉回 Asia/Taipei，等於把台北時間 13:45 的 K 棒標成 05:45（差 8
    # 小時）——導致跟 CSV 歷史資料（本來就是 naive 台北時間）合併時對不齊。
    frame["datetime"] = (
        pd.to_datetime(frame["time"], unit="s", utc=True).dt.tz_convert(TAIPEI_TZ).dt.tz_localize(None)
    )
    bars = frame.set_index("datetime")[["open", "high", "low", "close", "volume"]].sort_index()
    return add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )


def _load_bars(
    contract: str,
    product_code: str | None = None,
    *,
    interval_minutes: int = 60,
    min_bars_required: int | None = None,
) -> pd.DataFrame:
    if min_bars_required is None:
        min_bars_required = DEFAULT_STRATEGY.ma_slow + 2

    live_bars = pd.DataFrame()
    if product_code:
        live_bars = _bars_from_kline_store(product_code, interval_minutes=interval_minutes)
        if not live_bars.empty and len(live_bars) >= min_bars_required:
            return live_bars

    if interval_minutes != 60:
        # 沒有對應時間軸的 CSV 暖機檔可用；即時資料不足（甚至沒有 product_code）
        # 時一律直接回傳現況（可能是空的），不能退回 60 分鐘 CSV 合併，否則會把
        # 不同時間軸的K棒混在一起，弄亂 MA 計算。這個判斷刻意擺在 `if
        # product_code` 區塊之外、不巢狀在裡面——2026-09-30 最終審查抓到：擺在
        # 裡面時，如果哪天有個呼叫端漏傳 product_code，會直接漏接、意外落入
        # 下面的 60 分鐘 CSV 合併分支，讓這個不變量只靠呼叫端自律而非結構保證。
        return live_bars

    csv_bars = pd.DataFrame()
    candidates = [
        DATA_DIR / f"{contract}_60min_real.csv",
        DATA_DIR / f"{contract}_60min.csv",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        frame = pd.read_csv(path, parse_dates=["datetime"])
        frame = frame.sort_values("datetime")
        if len(frame) >= 2:
            gaps = frame["datetime"].diff().dropna().dt.total_seconds().median() / 60.0
            if gaps < 60.0:
                csv_bars = resample_to_60min(frame.set_index("datetime"))
            else:
                csv_bars = frame.set_index("datetime")
        else:
            csv_bars = frame.set_index("datetime")
        csv_bars.index = pd.to_datetime(csv_bars.index)
        break

    base_cols = ["open", "high", "low", "close", "volume"]
    if not live_bars.empty:
        live_clean = live_bars[base_cols] if all(c in live_bars.columns for c in base_cols) else live_bars
        if not csv_bars.empty:
            csv_clean = csv_bars[base_cols] if all(c in csv_bars.columns for c in base_cols) else csv_bars
            combined = pd.concat([csv_clean, live_clean])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        else:
            combined = live_clean
    else:
        combined = csv_bars

    if combined.empty:
        return pd.DataFrame()

    return add_moving_averages(
        combined.sort_index(),
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )


def _latest_closed_signal(signaled: pd.DataFrame) -> dict[str, Any] | None:
    if len(signaled) < 3:
        return None
    # 最後一根可能仍在形成中，取倒數第二根已收盤 K 棒
    idx = len(signaled) - 2
    row = signaled.iloc[idx]
    direction = row.get("signal")
    if direction not in ("long", "short"):
        return None
    bar_time = pd.Timestamp(signaled.index[idx])
    return {
        "time": bar_time.isoformat(),
        "direction": str(direction),
        "key": f"{bar_time.isoformat()}_{direction}",
    }


def _signaled_frame(strategy: str, bars: pd.DataFrame) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(bars)
    if strategy == "death_cross":
        return generate_death_cross_signals(bars)
    return generate_pullback_signals(bars)


def _estimate_pnl_ntd(
    *,
    contract: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    qty: int,
) -> float:
    spec = CONTRACT_SPECS.get(contract, CONTRACT_SPECS["TMF"])
    if direction == "long":
        points = exit_price - entry_price
    else:
        points = entry_price - exit_price
    return float(points * spec.point_value * qty)


def strategy_list() -> list[dict[str, Any]]:
    return [{"strategy_id": sid, **defs} for sid, defs in STRATEGY_DEFS.items()]


def klines_with_signals(
    product_code: str, limit: int = 500, interval_minutes: int = 60
) -> list[dict[str, Any]]:
    """給圖表用：K 棒 + MA(5/20/60) + 各策略訊號欄位。interval_minutes=60（預設）
    只算突破/回測；interval_minutes=15 額外算死叉訊號。"""
    contract = _contract_from_product(product_code)
    min_bars_required = (
        _min_bars_required_for_strategy("death_cross") if interval_minutes != 60 else None
    )
    bars = _load_bars(
        contract,
        product_code=product_code,
        interval_minutes=interval_minutes,
        min_bars_required=min_bars_required,
    )
    if bars.empty:
        return []
    bars = bars.tail(limit)

    breakout = _signaled_frame("breakout", bars)
    pullback = _signaled_frame("pullback", bars)
    death_cross = _signaled_frame("death_cross", bars) if interval_minutes != 60 else None

    def _num(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(f) else f

    rows: list[dict[str, Any]] = []
    for i, (idx, row) in enumerate(bars.iterrows()):
        row_dict: dict[str, Any] = {
            "time": to_unix_seconds(idx),
            "open": _num(row.get("open")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "close": _num(row.get("close")),
            "ma_fast": _num(row.get("ma_fast")),
            "ma_mid": _num(row.get("ma_mid")),
            "ma_slow": _num(row.get("ma_slow")),
            "breakout_signal": breakout.iloc[i].get("signal") if i < len(breakout) else None,
            "pullback_signal": pullback.iloc[i].get("signal") if i < len(pullback) else None,
        }
        if death_cross is not None:
            row_dict["death_cross_signal"] = (
                death_cross.iloc[i].get("signal") if i < len(death_cross) else None
            )
        rows.append(row_dict)
    return rows


def _fetch_equity_basis(st: dict[str, Any] | None = None) -> float:
    """風控本金基準：優先用目前帳戶真實權益數，抓不到才退回 .env 的固定預設值。"""
    try:
        if st is None:
            st = TradingService.get().status()
        equity = (st.get("rights") or {}).get("equity")
        if equity is not None and float(equity) > 0:
            return float(equity)
    except Exception:  # noqa: BLE001
        pass
    return _env_float("STRATEGY_INITIAL_CAPITAL", 100_000.0)


def strategy_config_defaults(strategy_id: str | None = None) -> dict[str, Any]:
    defs = STRATEGY_DEFS.get(strategy_id, {}) if strategy_id else {}
    strategy_kind = defs.get("strategy", "pullback")
    is_death_cross = strategy_kind == "death_cross"
    return {
        "strategy": strategy_kind,
        "direction_limit": defs.get("direction_limit", "long"),
        "label": defs.get("label", strategy_id or ""),
        "qty": _env_int("STRATEGY_QTY", 1),
        "initial_capital_ntd": _fetch_equity_basis(),
        "max_loss_ntd": _env_float("STRATEGY_MAX_LOSS_NTD", 10_000.0),
        "max_loss_pct": _env_float("STRATEGY_MAX_LOSS_PCT", 0.10),
        "stop_loss_points": 130.0 if is_death_cross else _env_float("STRATEGY_STOP_LOSS_POINTS", 100.0),
        "take_profit_points": 130.0 if is_death_cross else _env_float("STRATEGY_TAKE_PROFIT_POINTS", 250.0),
        "oco_enabled": True,
        "soft_stop_enabled": True,
        "risk_insurance_enabled": True,
        "reverse_signal_exit_enabled": not is_death_cross,
    }


def _state_to_dict(state: StrategyState) -> dict[str, Any]:
    return {
        "strategy_id": state.strategy_id,
        "label": state.label,
        "armed": not state.stopped,
        "stopped": state.stopped,
        "stop_reason": state.stop_reason,
        "product_code": state.product_code,
        "strategy": state.strategy,
        "direction_limit": state.direction_limit,
        "qty": state.qty,
        "held_qty": state.held_qty,
        "held_direction": state.held_direction,
        # 2026-08-27 起停損停利是同一張 OCO 保護單，兩個欄位都指向同一個
        # protection_order_smart_key——保留兩個 key 是為了前端相容，不用改
        # Dashboard 的顯示邏輯。
        "has_broker_stop_order": state.protection_order_smart_key is not None,
        "has_broker_take_profit_order": state.protection_order_smart_key is not None,
        "initial_capital_ntd": state.initial_capital_ntd,
        "realized_pnl_ntd": state.realized_pnl_ntd,
        "max_loss_ntd": state.max_loss_ntd,
        "max_loss_pct": state.max_loss_pct,
        "stop_loss_points": state.stop_loss_points,
        "take_profit_points": state.take_profit_points,
        "oco_enabled": state.oco_enabled,
        "soft_stop_enabled": state.soft_stop_enabled,
        "risk_insurance_enabled": state.risk_insurance_enabled,
        "reverse_signal_exit_enabled": state.reverse_signal_exit_enabled,
        "last_signal_key": state.last_signal_key,
        "last_checked_at": state.last_checked_at,
        "last_signal": state.last_signal,
        "last_action": state.last_action,
        "consecutive_failures": state.consecutive_failures,
    }


def strategy_status(strategy_id: str | None = None) -> dict[str, Any]:
    if strategy_id is not None:
        state = _armed.get(strategy_id)
        if state is None:
            return {
                "strategy_id": strategy_id,
                "armed": False,
                "config": strategy_config_defaults(strategy_id),
            }
        return _state_to_dict(state)
    return {sid: strategy_status(sid) for sid in STRATEGY_DEFS}


def start_strategy(
    strategy_id: str,
    product_code: str | None = None,
    *,
    qty: int | None = None,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
    initial_capital_ntd: float | None = None,
    max_loss_ntd: float | None = None,
    max_loss_pct: float | None = None,
    oco_enabled: bool | None = None,
    soft_stop_enabled: bool | None = None,
    risk_insurance_enabled: bool | None = None,
    reverse_signal_exit_enabled: bool | None = None,
) -> dict[str, Any]:
    if strategy_id not in STRATEGY_DEFS:
        raise ValueError(f"未知的策略 id: {strategy_id}（可用：{', '.join(STRATEGY_DEFS)}）")
    defaults = strategy_config_defaults(strategy_id)
    effective_product_code = resolve_strategy_product_code(product_code)
    state = StrategyState(
        strategy_id=strategy_id,
        product_code=effective_product_code,
        strategy=str(defaults["strategy"]),
        direction_limit=str(defaults["direction_limit"]),
        label=str(defaults["label"]),
        qty=int(qty if qty is not None else defaults["qty"]),
        initial_capital_ntd=float(initial_capital_ntd if initial_capital_ntd is not None else defaults["initial_capital_ntd"]),
        max_loss_ntd=float(max_loss_ntd if max_loss_ntd is not None else defaults["max_loss_ntd"]),
        max_loss_pct=float(max_loss_pct if max_loss_pct is not None else defaults["max_loss_pct"]),
        stop_loss_points=float(stop_loss_points if stop_loss_points is not None else defaults["stop_loss_points"]),
        take_profit_points=float(take_profit_points if take_profit_points is not None else defaults["take_profit_points"]),
        oco_enabled=bool(oco_enabled if oco_enabled is not None else defaults["oco_enabled"]),
        soft_stop_enabled=bool(soft_stop_enabled if soft_stop_enabled is not None else defaults["soft_stop_enabled"]),
        risk_insurance_enabled=bool(risk_insurance_enabled if risk_insurance_enabled is not None else defaults["risk_insurance_enabled"]),
        reverse_signal_exit_enabled=bool(reverse_signal_exit_enabled if reverse_signal_exit_enabled is not None else defaults["reverse_signal_exit_enabled"]),
    )

    # 啟動當下把「已經存在的舊訊號」預先標記為已消費，避免把啟動前就已成立的
    # MA 交叉狀態誤判成剛發生的新訊號，導致一開策略就對著舊狀態送出真實委託。
    contract = _contract_from_product(effective_product_code)
    bars = _load_bars(
        contract,
        product_code=effective_product_code,
        interval_minutes=_interval_minutes_for_strategy(state.strategy),
        min_bars_required=_min_bars_required_for_strategy(state.strategy),
    )
    if not bars.empty and len(bars) >= _min_bars_required_for_strategy(state.strategy):
        existing_signal = _latest_closed_signal(_signaled_frame(state.strategy, bars))
        if existing_signal:
            state.last_signal_key = existing_signal["key"]

    _armed[strategy_id] = state
    _persist_armed_snapshot()
    return strategy_status(strategy_id)


def stop_strategy(strategy_id: str) -> dict[str, Any]:
    state = _armed.get(strategy_id)
    if state is None:
        return strategy_status(strategy_id)
    if state.held_qty > 0:
        # 2026-08-21 實盤稽核發現：手動關閉開關如果直接把策略從 _armed 移除，
        # strategy_tick() 就再也不會呼叫到這個策略的 _tick_one()，代表它手上
        # 留倉的停損停利保護會直接消失（沒有任何真正的券商端停損單在保護，
        # 見 _tick_one 開頭註解——這裡的停損停利完全靠這個輪詢迴圈主動比價）。
        # 所以留倉沒平掉之前不能真的移除，只標記 stopped（不再開新倉），讓
        # _tick_one 開頭那段強制平倉檢查繼續跑，直到這筆倉位真的平掉為止。
        _stop_with_reason(
            state,
            "使用者手動停止（尚有留倉，持續監控停損停利中，平倉後才會完全停止）",
        )
    else:
        _armed.pop(strategy_id, None)
    _persist_armed_snapshot()
    return strategy_status(strategy_id)


def _reconcile_audit_log_path() -> Path:
    """
    reconcile_after_manual_close 稽核 log（持久化、只會 append），跟
    broker/capital_futures.py 的 _order_audit_log_path()／
    _position_audit_log_path() 同一個道理。

    2026-08-27：使用者質疑「為什麼你不把重要動作都 logging，就不用猜來猜去」
    ——這個函式判定「內部記錄跟券商回報不一致、要清空持倉並停止策略」是很
    關鍵的決定，之前完全沒有把當下比對的數字（內部記幾口、券商回報幾口、
    用的是哪份 broker 快照）寫進任何看得到的地方，事後只能從 SKCOM 自己的
    原始 Order.log 側面猜測（而且那份 log 不記錄我們自己的比對邏輯，猜不
    準）。這個檔案就是為了下次再發生類似狀況時，能直接查到當時真正比對的
    數字，不用再猜。
    """
    account_user_id = os.getenv("ACCOUNT_USER_ID", "").strip()
    if account_user_id:
        return DATA_DIR / "users" / account_user_id / "reconcile_audit.log"
    return DATA_DIR / "reconcile_audit.log"


def _append_reconcile_audit(
    *,
    product: str,
    direction: str,
    internal_total: int,
    actual: int,
    mismatch: bool,
    affected_strategy_ids: list[str],
    broker_positions_summary: str,
) -> None:
    try:
        path = _reconcile_audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = (
            f"{ts}\tproduct={product}\tdirection={direction}\t"
            f"internal_total={internal_total}\tactual={actual}\tmismatch={mismatch}\t"
            f"affected={','.join(affected_strategy_ids)}\t"
            f"broker_positions={broker_positions_summary}\n"
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - 稽核 log 寫入失敗不該影響 reconcile 主流程
        logger.warning("寫入 reconcile 稽核 log 失敗", exc_info=True)


def reconcile_after_manual_close(product_code: str | None = None) -> list[str]:
    """
    2026-08-21 實盤稽核發現：手動「平倉」/「一鍵平倉」會正確送出真實委託給券商，
    但策略自己的 held_qty/held_direction 記帳完全不知道這件事。平倉按鈕呼叫
    完之後應該叫這個函式：比對每個 (商品, 方向) 上，所有仍 armed 且
    held_qty>0 的策略，內部記錄的口數總和是否超過券商目前實際回報的淨部位。

    2026-08-29 事後討論調整：原本兜不起來時會停止策略、要求人工核對；後來
    確認這個專案實務上不會多策略共用同一商品同一方向，且券商本身不允許
    同一商品雙向持倉（見 docs/order_execution_logic.md）——走到這裡的成因
    只剩「手動平倉」或「真實保護單已在券商端觸發」，兩者都是正常、預期
    內的事件，不該被當成異常去停止監控。broker 回報的淨部位才是唯一真相，
    這裡只是把內部記帳同步過去，不觸發停止、也不觸發撤單（見下方寬限期
    與撤單邏輯的說明）。
    """
    svc = TradingService.get()
    st = svc.status()
    broker_positions = st.get("positions") or []

    broker_qty: dict[tuple[str, str], int] = {}
    for pos in broker_positions:
        direction_key = pos.get("direction_key")
        if direction_key not in {"long", "short"}:
            continue
        prod = _canonical_position_product(str(pos.get("product") or ""))
        try:
            qty = int(float(str(pos.get("qty", "0")).replace(",", "")))
        except ValueError:
            qty = 0
        key = (prod, direction_key)
        broker_qty[key] = broker_qty.get(key, 0) + qty

    now = time.time()
    groups: dict[tuple[str, str], list[StrategyState]] = {}
    for state in _armed.values():
        if state.held_qty <= 0 or not state.held_direction:
            continue
        if product_code is not None and state.product_code != product_code:
            continue
        if (
            state.entry_recorded_at is not None
            and now - state.entry_recorded_at < RECONCILE_ENTRY_GRACE_SECONDS
        ):
            # 剛進場、還在寬限期內——broker 持倉快照很可能還沒跟上，這一輪
            # 先跳過這個策略，不拿它去跟 broker 比對（也不記進 groups，避免
            # 拖累同組其他策略的比對）。稽核 log 留一筆紀錄，不是沒查，是
            # 主動選擇先不查。
            _append_reconcile_audit(
                product=_canonical_position_product(state.product_code),
                direction=state.held_direction,
                internal_total=state.held_qty,
                actual=-1,
                mismatch=False,
                affected_strategy_ids=[state.strategy_id],
                broker_positions_summary="SKIPPED_ENTRY_GRACE_PERIOD",
            )
            continue
        key = (_canonical_position_product(state.product_code), state.held_direction)
        groups.setdefault(key, []).append(state)

    broker_positions_summary = ";".join(
        f"{p.get('product')}/{p.get('direction_key')}x{p.get('qty')}" for p in broker_positions
    )

    affected: list[str] = []
    for (prod, direction), states in groups.items():
        internal_total = sum(s.held_qty for s in states)
        actual = broker_qty.get((prod, direction), 0)
        mismatch = internal_total > actual
        _append_reconcile_audit(
            product=prod,
            direction=direction,
            internal_total=internal_total,
            actual=actual,
            mismatch=mismatch,
            affected_strategy_ids=[s.strategy_id for s in states],
            broker_positions_summary=broker_positions_summary,
        )
        if not mismatch:
            continue
        for s in states:
            s.held_qty = 0
            s.held_direction = None
            s.entry_price = 0.0
            s.held_qty_cleared_at = now
            # 2026-08-29 實盤事故 + 事後討論：這裡原本會順手撤保護單、還會
            # 停止策略要求人工重啟。兩者都拿掉了：
            # 1. 撤保護單——那次事故證明這個比對可能建立在過期快照上，撤單
            #    是不可逆動作，交給 reconcile_orphan_stop_orders 處理（那裡
            #    對剛進場的部位也有寬限期保護）。
            # 2. 停止策略——後來確認這個專案實務上不會多策略共用同一商品同
            #    一方向，且券商本身不允許同一商品雙向持倉，會走到這個分支
            #    的成因只剩「手動平倉」或「真實保護單已在券商端觸發」兩種，
            #    都是正常、預期內的事件（保護單觸發甚至是它本來就該做的
            #    事），不該被當成異常去停止監控、要求人工介入。清空內部
            #    記帳、繼續正常運作即可——broker 才是唯一真相，這裡只是把
            #    內部記帳同步過去，不是「出事了」。
            s.last_action = (
                f"偵測到 {prod} {direction} 部位跟券商實際回報不一致，已同步"
                f"（本策略原記錄 {internal_total} 口，券商實際回報 {actual} 口，"
                "可能是手動平倉／一鍵平倉，或真實 OCO 保護單已在券商端觸發自動"
                "平倉），策略繼續正常監控。"
            )
            affected.append(s.strategy_id)
    return affected


def _stop_with_reason(state: StrategyState, reason: str) -> None:
    state.stopped = True
    state.stop_reason = reason
    state.last_action = reason


def _tick_one(state: StrategyState, svc: TradingService, st: dict[str, Any]) -> None:
    state.last_checked_at = time.time()

    # 若無持倉且為微台商品，自動對齊至當前近月合約（結算日換約自動無縫切換）
    if state.held_qty == 0 and state.product_code.upper().startswith("TM"):
        current_tmf = compute_current_tmf_code()
        if state.product_code.upper() != current_tmf.upper():
            state.product_code = current_tmf
            # 換約切換商品當下，必須重新對新商品預先標記「既有舊訊號為已消費」，
            # 避免把新商品換約前就已存在的歷史 MA 狀態誤判為剛產生的新訊號而誤發市價單！
            new_contract = _contract_from_product(current_tmf)
            new_bars = _load_bars(
                new_contract,
                product_code=current_tmf,
                interval_minutes=_interval_minutes_for_strategy(state.strategy),
                min_bars_required=_min_bars_required_for_strategy(state.strategy),
            )
            if not new_bars.empty and len(new_bars) >= _min_bars_required_for_strategy(state.strategy):
                existing_sig = _latest_closed_signal(_signaled_frame(state.strategy, new_bars))
                state.last_signal_key = existing_sig["key"] if existing_sig else None
            else:
                state.last_signal_key = None

    contract = _contract_from_product(state.product_code)
    quote_info = st.get("quote") or {}
    quote = quote_info.get("last_price")
    # 2026-09-30 抓到的架構缺陷：群益報價訂閱同時只能訂閱一個商品（見
    # capital_futures.py 的 self.live.subscribed_product 是單一變數），所有
    # 已武裝的策略共用同一份 st["quote"]，不是各自去查自己商品的報價。如果
    # 操作者在 Dashboard 把商品代碼框切去看別的商品（例如切到大台 TX00），
    # 這裡原本會直接拿切換後的商品價格，誤當成這個策略自己商品（例如TMF）的
    # 現價去判斷停損停利/風控——TX/MTX/TMF 都追蹤同一個台指指數、價位接近，
    # 這個錯誤不會一眼看出來是錯的，拿錯的商品價格做風控判斷比完全沒有報價
    # 更危險。這裡偵測到報價來源標示的商品跟這個策略自己的商品不是同一種
    # 合約類型時，直接當作沒有可用報價（等同真的沒收到報價），安全地跳過這
    # 一輪的軟停損停利判斷，寧可靠掛著的真實 OCO 保護單頂著，也不要用錯的
    # 商品價格做判斷。只在報價明確帶了 product_code 時才檢查——沒帶（例如
    # 舊測試/尚未收到任何報價的空字串）維持原本行為，不無故擋掉。
    quote_product_code = quote_info.get("product_code")
    if quote_product_code and _contract_from_product(quote_product_code) != contract:
        quote = None

    # 2026-10-07：長效單序號非同步補查。若已掛真實 OCO 長效保護單，但長效單序號尚未查得
    # （例如剛進場下單時券商中台尚未寫入資料庫），在每輪 tick 順便嘗試補查，確保平倉時能精確撤單
    if (
        USE_LONG_ACTION_PROTECTION
        and state.held_qty > 0
        and state.protection_order_smart_key
        and not state.protection_order_long_action_key
    ):
        _refresh_long_action_key_for_state(state, svc)

    # 持倉點數停損停利：2026-08-07 改成純點數門檻（進場價 ±點數）為主要判斷依據。
    # 2026-08-07 再加回 NTD/%浮動損益門檻，當作「額外保險」：正常情況點數停損會先
    # 觸發，但如果跳空、或單口損失金額換算後比 NTD 門檻寬鬆（例如多口交易），
    # 點數停損可能還沒到，但浮動虧損金額/比例已經超過帳戶能承受的範圍，這時候
    # 仍然要強制停止，不能只靠點數。這個檢查不管策略是否已經因為其他原因被停止
    # 都要跑，否則策略一停止、手上還有留倉的話就完全沒人保護了。觸發時直接強制
    # 送出真實市價平倉單，不等反向訊號、不等下一輪。
    if state.held_qty > 0 and state.entry_price > 0 and quote:
        try:
            current_price = float(quote)
        except (ValueError, TypeError):
            current_price = None

        trigger_kind: str | None = None
        floating_pnl: float | None = None
        if current_price is not None:
            try:
                floating_pnl = _estimate_pnl_ntd(
                    contract=contract,
                    direction=str(state.held_direction),
                    entry_price=state.entry_price,
                    exit_price=current_price,
                    qty=state.held_qty,
                )
            except (ValueError, TypeError):
                floating_pnl = None

            points = (
                current_price - state.entry_price
                if state.held_direction == "long"
                else state.entry_price - current_price
            )
            if state.soft_stop_enabled and points <= -abs(state.stop_loss_points):
                # Layer 2 (軟停損備援)：
                # 1) 若券商端無 OCO 保護單 (protection_order_smart_key is None)，由軟停損正常頂著
                # 2) 若已掛券商端 OCO，但現價已穿過停損點位超過安全緩衝（預設 10 點），
                #    代表券商 OCO 異常未觸發、跨盤失效、或主機斷線，此時強制啟動「緊急硬穿價軟停損」
                if state.protection_order_smart_key is None:
                    trigger_kind = "stop_loss"
                elif points <= -abs(state.stop_loss_points) - EMERGENCY_STOP_BUFFER_POINTS:
                    trigger_kind = "emergency_stop"
            elif state.soft_stop_enabled and points >= abs(state.take_profit_points) and state.protection_order_smart_key is None:
                # Layer 2 (軟停利備援)
                trigger_kind = "take_profit"
            elif state.risk_insurance_enabled and floating_pnl is not None and state.loss_limit_hit(floating_pnl):
                # Layer 3 (風控保險網 / 硬停損)
                trigger_kind = "risk_stop"

        if trigger_kind is not None:
            reason_label = {
                "stop_loss": "停損",
                "emergency_stop": "緊急穿價停損",
                "take_profit": "停利",
                "risk_stop": "風控保險",
            }[trigger_kind]
            if trigger_kind == "risk_stop":
                threshold_note = f"上限 -{state.max_loss_ntd:,.0f} 元 / -{state.max_loss_pct * 100:.0f}%"
            elif trigger_kind == "emergency_stop":
                threshold_note = f"門檻 {state.stop_loss_points:.0f} 點（穿價超額逾 {EMERGENCY_STOP_BUFFER_POINTS:.0f} 點，券商智慧單未觸發備援）"
            else:
                points_value = state.stop_loss_points if trigger_kind == "stop_loss" else state.take_profit_points
                threshold_note = f"門檻 {points_value:.0f} 點"
            close_side = "sell" if state.held_direction == "long" else "buy"
            try:
                # 2026-10-07 修正：先撤 OCO 保護單、再送平倉單（順序以前是反的）。
                # 2026-08-27 實盤事故的根因就是這個順序：平倉額度一口只能被一張
                # 委託認領，如果 OCO 其中一腿已經觸發、還掛在券商那邊佔著平倉額
                # 度，這時候才送市價平倉單，券商會直接回 [999] 勾選平倉而留倉
                # 部位不足，平倉失敗、也就永遠進不到撤單那一步，陷入死鎖。
                # _cancel_protection_order_for_state 本身是安全的 no-op（沒有
                # 追蹤中的保護單、或撤單本身失敗都只記警告，不會中斷流程）。
                _cancel_protection_order_for_state(state, svc)
                result = svc.place_order(
                    {
                        "product_code": state.product_code,
                        "side": close_side,
                        "qty": state.held_qty,
                        "price": "M",
                        "trade_type": 2,
                        "new_close": 1,
                    }
                )
                order_result = result.get("order_result") or {}
                if order_result.get("success"):
                    if floating_pnl is not None:
                        state.realized_pnl_ntd += floating_pnl
                    _record_trade(
                        state.strategy_id,
                        kind="exit",
                        bar_time=pd.Timestamp.now(),
                        direction=str(state.held_direction),
                        price=float(current_price),
                        pnl=floating_pnl,
                    )
                    pnl_note = f"，浮動損益 {floating_pnl:,.0f} 元" if floating_pnl is not None else ""
                    _stop_with_reason(
                        state,
                        f"觸發持倉{reason_label}，已強制平倉（{threshold_note}{pnl_note}）",
                    )
                    state.held_qty = 0
                    state.held_direction = None
                    state.entry_price = 0.0
                elif _check_orphaned_fill(
                    result.get("state") or {},
                    expected_side=("short" if close_side == "sell" else "long"),
                    qty=state.held_qty,
                ):
                    # 這裡特別重要：這個分支每一輪只要觸價還在，就會再試一次平倉。
                    # 如果實際上已經平倉成功、卻因為誤判失敗而繼續認為 held_qty>0，
                    # 下一輪會對著一個已經不存在的部位再送一次「平倉」單，等於
                    # 反手開一個新的相反部位——所以這裡直接把持倉狀態清空，
                    # 不只是停止而已。實際成交價未知，realized_pnl_ntd 不強行估算。
                    state.held_qty = 0
                    state.held_direction = None
                    state.entry_price = 0.0
                    # 這裡再補一次撤單當保險：上面 try 一開頭已經撤過一次，通常
                    # 這裡是 no-op；但如果那次撤單剛好失敗、而這筆成交其實是 OCO
                    # 自己觸發造成的，這裡再試一次還是能把孤兒單清掉（撤一張已經
                    # 不存在的單會失敗，無妨，見 reconcile_orphan_stop_orders）。
                    _cancel_protection_order_for_state(state, svc)
                    _stop_with_reason(
                        state,
                        f"⚠ {reason_label}平倉委託回報失敗，但偵測到近期有相符的真實成交紀錄，"
                        "研判實際已平倉成功，已清空本策略持倉紀錄並停止，"
                        "請務必人工核對實際帳戶損益！",
                    )
                else:
                    state.consecutive_failures += 1
                    state.last_action = f"{reason_label}平倉失敗: {order_result.get('message') or '券商拒單'}"
            except Exception as exc:  # noqa: BLE001
                logger.warning("strategy_tick[%s] %s close failed: %s", state.strategy_id, reason_label, exc)
                state.last_action = f"{reason_label}平倉失敗: {exc}"
            return

    if state.stopped:
        return

    # 空手時的風控：沿用累計已實現損益，避免連續多筆已平倉的虧損單還繼續開新倉
    # （跟上面的持倉浮動停損是兩件事：一個保護「現在這筆」，一個防止「一直虧下去」）。
    if state.held_qty == 0 and state.loss_limit_hit():
        _stop_with_reason(
            state,
            f"已達風控停損（累計已實現損益 {state.realized_pnl_ntd:,.0f} 元，"
            f"上限 -{state.max_loss_ntd:,.0f} 元 / -{state.max_loss_pct * 100:.0f}%）",
        )
        return

    bars = _load_bars(
        contract,
        product_code=state.product_code,
        interval_minutes=_interval_minutes_for_strategy(state.strategy),
        min_bars_required=_min_bars_required_for_strategy(state.strategy),
    )
    if bars.empty or len(bars) < _min_bars_required_for_strategy(state.strategy):
        state.last_action = "K 棒資料不足，略過本輪"
        return

    # 見上面 _has_abnormal_intraday_gap 的 2026-10-06 事故說明：訊號判斷用的是
    # bars 倒數第二、第三根（_latest_closed_signal 的 idx 跟它前一根），這兩根
    # 之間如果有同一盤內的異常缺口，代表 K 棒快取還沒補齊，先不判斷，等下一輪。
    # 只在 bars 是真正的 DataFrame（有真實時間索引）時才檢查——測試裡常用
    # MagicMock 頂替 bars 跳過真實 K 棒計算，這種情況下沒有索引可檢查，略過即可。
    if isinstance(bars, pd.DataFrame) and len(bars) >= 3:
        gap_idx = len(bars) - 2
        # window 對齊目前策略實際用到的 MA 根數（_min_bars_required_for_strategy
        # 本身就是 ma_mid/ma_slow + 2），檢查整個視窗範圍，不是只看最後一對。
        window = _min_bars_required_for_strategy(state.strategy) - 2
        try:
            has_gap = _has_abnormal_gap_in_recent_window(
                bars, gap_idx, window, _interval_minutes_for_strategy(state.strategy)
            )
        except Exception as exc:  # noqa: BLE001
            # strategy_tick() 沒有把每個策略的 _tick_one 包 try/except（見該函式），
            # 這裡任何例外（例如索引出現非預期型別的時間戳）會讓整輪 strategy_tick
            # 直接中斷，連這次事故真正要保護的其他策略、以及後面的
            # reconcile_after_manual_close／reconcile_orphan_stop_orders 都不會跑到——
            # 這個新加的防呆本身絕對不能變成新的全面停擺風險，寧可這次跳過檢查、
            # 退回沒有這層防護的舊行為，也不要讓整個 tick 迴圈掛掉。
            logger.warning("strategy_tick[%s] 缺口檢查本身發生例外，略過此檢查: %s", state.strategy_id, exc)
            has_gap = False
        if has_gap:
            state.last_action = "K 棒資料有缺口（疑似尚未補齊），本輪暫不判斷訊號"
            return

    signaled = _signaled_frame(state.strategy, bars)
    signal = _latest_closed_signal(signaled)
    state.last_signal = signal
    if not signal:
        state.last_action = "本輪無新訊號"
        return

    # 2026-10-07 加固：就算上面的缺口檢查都通過，訊號本身如果是「很久以前收盤的那根」
    # （例如因為某種延遲，好幾輪之後才真正處理到這根），也不該追價——只追「剛收盤」
    # 的訊號，過期的訊號視為已消費，安靜略過，不送單。
    try:
        signal_bar_time = pd.Timestamp(signal["time"])
        signal_age_minutes = (_now() - signal_bar_time).total_seconds() / 60.0
    except (KeyError, ValueError, TypeError):
        signal_age_minutes = 0.0
    interval_minutes = _interval_minutes_for_strategy(state.strategy)
    if signal_age_minutes > interval_minutes * 1.5:
        state.last_signal_key = signal["key"]
        state.last_action = f"訊號已過期（{signal['time']}），視為已消費，不追價"
        return

    if signal["key"] == state.last_signal_key:
        state.last_action = "訊號已處理，持倉不動"
        return

    target = signal["direction"]

    # 2026-07-25 查證（官方文件《7.下單-國內期選》FUTUREORDER 結構定義，SendFutureOrderCLR
    # 適用）：bstrPrice 搭配 IOC/FOK 時，市價要填字串 "M"（範圍市價填 "P"），不是 "0"。
    # 先前的 price="0" 是本檔案沒查證就寫的錯誤假設，群益後台把 "0" 當非法價格，
    # 就是 [530] 委託單類別(ORDER TYPE)輸入錯誤的根因（151 次拒單見
    # strategy-order-retry-storm-bug 記憶）。

    try:
        if target == state.direction_limit:
            if state.held_qty == 0 and _opposite_direction_position_exists(
                state.product_code, target, exclude_strategy_id=state.strategy_id
            ):
                state.last_action = (
                    f"偵測到同商品有反向策略持倉中，暫緩進場（避免被券商淨沖銷)，"
                    f"待該部位靠 SL/TP/風控真正出場後才會嘗試 {target}"
                )
            elif state.held_qty == 0:
                side = "buy" if target == "long" else "sell"
                result = svc.place_order(
                    {
                        "product_code": state.product_code,
                        "side": side,
                        "qty": state.qty,
                        "price": "M",
                        "trade_type": 2,
                        "new_close": 0,
                    }
                )
                order_result = result.get("order_result") or {}
                if not order_result.get("success"):
                    # place_order() 券商拒單時不會拋例外，只會回傳 success=False，
                    # 一定要檢查這個欄位，不然畫面會顯示「已進場」但實際上完全沒有真實委託單。
                    # 同一個訊號不論成敗都只消費一次（設 last_signal_key）：拒單絕不能
                    # 每輪盲目重試——2026-07-24 實測曾因重試語意造成每 64 秒轟一筆、
                    # 一晚 151 筆拒單。結構性拒單（如 [530]）重試一萬次也不會成功。
                    state.last_signal_key = signal["key"]
                    state.consecutive_failures += 1
                    state.last_action = f"下單失敗（{target}）: {order_result.get('message') or '券商拒單'}"
                    if _check_orphaned_fill(
                        result.get("state") or {}, expected_side=target, qty=state.qty
                    ):
                        # 2026-08-25：圖表上原本完全看不到這筆孤兒單，因為這個分支
                        # 只停用策略、沒有呼叫 _record_trade()——使用者得自己去對帳
                        # 才知道發生過什麼事。這裡補記一筆，價格用當下報價估計
                        # （orders_raw 裡沒有精確成交價可用），標註為估計值，
                        # 讓圖表至少看得到「這裡疑似有一筆」。不動 held_qty／
                        # held_direction／entry_price：那些欄位仍然刻意不猜測歸屬
                        # 哪個策略（見 _check_orphaned_fill 說明），只是補視覺紀錄。
                        try:
                            estimated_px = float(quote) if quote else 0.0
                        except (ValueError, TypeError):
                            estimated_px = 0.0
                        _record_trade(
                            state.strategy_id,
                            kind="entry",
                            bar_time=signal["time"],
                            direction=target,
                            price=estimated_px,
                        )
                        _stop_with_reason(
                            state,
                            "⚠ 委託系統回報失敗，但偵測到近期有相符的真實成交紀錄，"
                            "帳戶可能有未被追蹤的真實部位，已自動停止本策略，"
                            "請立即人工核對並視需要手動平倉！"
                            "（圖表上這筆進場標記價格為估計值，非真實成交價）",
                        )
                else:
                    try:
                        entry_px = float(quote) if quote else 0.0
                    except (ValueError, TypeError):
                        entry_px = 0.0
                    if entry_px > 0 and not bars.empty:
                        # 2026-09-11 加固：報價可能是跨盤沒更新的過期值（見上方
                        # QUOTE_STALENESS_POINTS 說明），跟可靠的K棒收盤價偏離太多
                        # 就不信任它，視同報價缺失，走後面的備援鏈。
                        try:
                            reference_close = float(bars.iloc[-1]["close"])
                            if reference_close > 0 and abs(entry_px - reference_close) > QUOTE_STALENESS_POINTS:
                                entry_px = 0.0
                        except (ValueError, TypeError, KeyError, IndexError):
                            pass
                    if entry_px <= 0:
                        # 2026-08-30 加固：即時報價缺失時，優先從下單後刷新的 broker positions
                        # 快照裡讀取真實成交均價（GetOpenInterestGW 回報的實際成本），
                        # 讀不到才退回用最新收盤 K 棒的收盤價當估計值。避免 entry_price=0
                        # 導致 OCO 保護單漏掛與軟停損失效。
                        try:
                            post_state = result.get("state") or st or {}
                            for pos in post_state.get("positions") or []:
                                pos_prod = _canonical_position_product(str(pos.get("product") or ""))
                                if pos_prod == _canonical_position_product(state.product_code) and pos.get("price"):
                                    entry_px = float(str(pos.get("price")).replace(",", ""))
                                    if entry_px > 0:
                                        break
                        except Exception:  # noqa: BLE001
                            pass
                    if entry_px <= 0:
                        try:
                            if not bars.empty:
                                entry_px = float(bars.iloc[-1]["close"])
                        except (ValueError, TypeError, KeyError, IndexError):
                            entry_px = 0.0
                    state.held_qty = state.qty
                    state.held_direction = target
                    state.entry_price = entry_px
                    state.entry_recorded_at = time.time()
                    state.last_action = f"訊號進場 {target} x{state.qty}"
                    state.last_signal_key = signal["key"]
                    state.consecutive_failures = 0
                    _record_trade(
                        state.strategy_id,
                        kind="entry",
                        bar_time=signal["time"],
                        direction=target,
                        price=state.entry_price,
                    )
                    if state.entry_price > 0 and state.oco_enabled:
                        oco_reference_price = None
                        try:
                            if not bars.empty:
                                oco_reference_price = float(bars.iloc[-1]["close"])
                        except (ValueError, TypeError, KeyError, IndexError):
                            oco_reference_price = None
                        _place_oco_protection_for_state(state, svc, reference_price=oco_reference_price)
            else:
                state.last_action = f"已持倉 {target} x{state.held_qty}，同向訊號不動作"
        else:
            # 方向限定策略：出現反向訊號只平倉出場，不反手做另一邊
            if state.held_qty > 0:
                if not state.reverse_signal_exit_enabled:
                    state.last_action = (
                        f"已持倉 {state.held_direction} x{state.held_qty}，"
                        "已關閉反向訊號出場，持倉不動"
                    )
                else:
                    close_side = "sell" if state.held_direction == "long" else "buy"
                    result = svc.place_order(
                        {
                            "product_code": state.product_code,
                            "side": close_side,
                            "qty": state.held_qty,
                            "price": "M",
                            "trade_type": 2,
                            "new_close": 1,
                        }
                    )
                    order_result = result.get("order_result") or {}
                    if not order_result.get("success"):
                        state.last_signal_key = signal["key"]
                        state.consecutive_failures += 1
                        state.last_action = f"平倉失敗（{state.held_direction}）: {order_result.get('message') or '券商拒單'}"
                        if _check_orphaned_fill(
                            result.get("state") or {},
                            expected_side=("short" if close_side == "sell" else "long"),
                            qty=state.held_qty,
                        ):
                            # 2026-08-21 修正：這裡跟上面持倉停損停利那段的孤兒單處理不一致，
                            # 之前只有停止、沒有清空 held_qty——代表偵測到「其實已經平倉」
                            # 之後，下一輪還是會當作有留倉繼續處理，等於白偵測。統一改成
                            # 跟上面一樣清空。
                            state.held_qty = 0
                            state.held_direction = None
                            state.entry_price = 0.0
                            _cancel_protection_order_for_state(state, svc)
                            _stop_with_reason(
                                state,
                                "⚠ 平倉委託回報失敗，但偵測到近期有相符的真實成交紀錄，"
                                "這筆倉位可能其實已經平倉、或狀態跟我們追蹤的不一致，"
                                "已自動停止本策略，請立即人工核對！",
                            )
                    else:
                        pnl = None
                        if state.entry_price > 0 and quote:
                            pnl = _estimate_pnl_ntd(
                                contract=contract,
                                direction=str(state.held_direction),
                                entry_price=state.entry_price,
                                exit_price=float(quote),
                                qty=state.held_qty,
                            )
                            state.realized_pnl_ntd += pnl
                        _record_trade(
                            state.strategy_id,
                            kind="exit",
                            bar_time=signal["time"],
                            direction=str(state.held_direction),
                            price=float(quote) if quote else state.entry_price,
                            pnl=pnl,
                        )
                        state.last_action = f"反向訊號，平倉 {state.held_direction} x{state.held_qty}"
                        state.held_qty = 0
                        state.held_direction = None
                        state.entry_price = 0.0
                        state.consecutive_failures = 0
                        state.last_signal_key = signal["key"]
                        _cancel_protection_order_for_state(state, svc)
            else:
                state.last_action = "空手，反向訊號不動作（本策略不做這個方向）"
        if state.loss_limit_hit():
            _stop_with_reason(
                state, f"已達風控停損（累計損益 {state.realized_pnl_ntd:,.0f} 元）"
            )
        elif state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            # 結構性拒單（例如委託參數被券商拒絕）不會因為重試而自己好——連續失敗
            # 到一定次數就自動停止並明確報錯，不要放著讓它每次新訊號都無聲失敗。
            _stop_with_reason(
                state,
                f"連續 {state.consecutive_failures} 次下單失敗已自動停止，"
                f"最後錯誤：{state.last_action}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategy_tick[%s] failed: %s", state.strategy_id, exc)
        state.last_action = f"下單失敗: {exc}"


def strategy_tick() -> dict[str, Any]:
    """單次策略檢查（由背景 loop 呼叫），依序處理所有已啟用的策略。"""
    if not _armed:
        return {}

    svc = TradingService.get()
    st = svc.status()
    if not st.get("connected"):
        return {}

    for state in list(_armed.values()):
        try:
            _tick_one(state, svc, st)
        except Exception as exc:  # noqa: BLE001
            logger.exception("strategy_tick[%s] 執行例外，隔離該策略避免影響其他策略與對帳: %s", state.strategy_id, exc)
            state.last_action = f"本輪執行異常: {exc}"

    # 2026-08-21：真正的 STP/MIT 智慧單是在券商端觸發的，我們的軟體不會收到
    # 任何主動通知，held_qty 不會自動歸零。每輪都主動核對一次broker實際部位，
    # 不然要等到反向訊號或風控保險剛好也觸發、送平倉單失敗時才會被動發現，
    # 中間這段時間畫面顯示的持倉是錯的，另一張智慧單也會一直孤兒掛著。
    reconcile_after_manual_close()
    reconcile_orphan_stop_orders()
    return strategy_status()