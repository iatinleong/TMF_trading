"""backend/shioaji_backfill.py — 群益 K 線回補「進行中交易時段」拿不到資料時的備援。

群益 SKQuoteLib_RequestKLineAMByDate 有個實測驗證過的限制：交易時段（日盤/夜盤）
進行中，就算是已經收盤的那幾根 K 棒，回補 API 也完全不給，要等整個時段真正
結束才會補齊（見 docs/vm_deployment_gotchas.md 第 5 節）。這在後端頻繁重開
（除錯、部署）時很常見——2026-08-25 實測案例：後端在夜盤進行到一半重開，
16:00~21:00 這幾根已經收盤的 K 棒完全拿不到，策略當下算出來的 5/20/60MA
會是失真的（rolling window 不知道中間跳過了幾個小時）。

永豐 Shioaji API 是完全獨立的另一個資料源，用逐筆 ticks 查詢不受這個限制
（2026-08-25 實測驗證：合約 target_code/reference 價位跟群益完全對得上，
同一個交易日、群益缺的部分，Shioaji 當下就查得到）。這裡拿它來補這個空窗期，
只補真正缺的，不覆蓋群益/即時 tick 已經有的資料。

需要 SJ_API_KEY / SJ_SEC_KEY（.env，見 https://www.sinotrade.com.tw/newweb/
PythonAPIKey/ 申請）。沒設定金鑰、套件沒裝、連線失敗、查無缺口——都安靜跳過，
不影響主流程（這只是錦上添花的備援，不是關鍵路徑）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

from .config import app_base_dir
from .kline_engine import bar_close_time_from_ts, get_store
from .secrets_store import load_remote_secrets
from .timeutil import TAIPEI_TZ, to_unix_seconds

logger = logging.getLogger(__name__)

BASE_DIR = app_base_dir(__file__)
ENV_PATH = BASE_DIR / ".env"


def _audit_log_path():
    return BASE_DIR / "data" / "shioaji_backfill_audit.log"


def _append_audit(line: str) -> None:
    """
    2026-08-26：這個模組原本只靠 logger.info/warning 記錄過程，但這個專案的
    logging 設定在一般部署下不會把 backend.* 的 logger 輸出接到任何看得到的
    地方（uvicorn 只設定了自己的 access/error log），導致「補缺失敗了但完全
    看不出來哪一步失敗」——VM 上回報「好像沒補成功」時，只能靠塞進去的診斷
    腳本才查得到，跟 order_audit.log/position_audit.log 一樣的教訓。這裡改成
    額外落地存一份，append-only，不受一般 logging 設定影響，失敗也不該擋
    主流程。
    """
    try:
        path = _audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{ts}\t{line}\n")
    except Exception:  # noqa: BLE001 - 稽核 log 寫入失敗不該影響補缺主流程
        logger.warning("寫入 Shioaji 補缺稽核 log 失敗", exc_info=True)


def _load_env() -> None:
    load_dotenv(ENV_PATH)
    # 2026-08-26 實測抓到的缺口：SJ_API_KEY/SJ_SEC_KEY 改成集中放 Supabase
    # 之後（見 backend/secrets_store.py），本機 .env 裡已經沒有這兩個值了。
    # 這裡原本只呼叫 load_dotenv() 讀本機 .env，沒有跟著呼叫遠端密鑰同步，
    # 導致 shioaji_available() 一律誤判成「沒設定金鑰」，補缺功能完全不會
    # 被觸發。跟 live_service.py::_load_project_env() 用同一套機制，失敗
    # 就安靜退回本機 .env，不影響呼叫端。
    try:
        load_remote_secrets()
    except Exception as exc:  # noqa: BLE001 - 遠端密鑰失敗不該擋補缺流程
        logger.debug("套用 Supabase 密鑰時發生例外，忽略並使用本機 .env: %s", exc)


def shioaji_available() -> bool:
    _load_env()
    return bool(os.getenv("SJ_API_KEY", "").strip() and os.getenv("SJ_SEC_KEY", "").strip())


def _is_production() -> bool:
    _load_env()
    return os.getenv("SJ_PRODUCTION", "true").strip().lower() in {"1", "true", "yes", "y"}


def _expected_day_session_labels(day: pd.Timestamp) -> list[pd.Timestamp]:
    """日盤收盤標籤 09:45..13:45（5 根），對齊 kline_engine.py 的分桶規則。"""
    base = day.normalize() + pd.Timedelta(hours=8, minutes=45)
    return [base + pd.Timedelta(minutes=60 * i) for i in range(1, 6)]


def _expected_night_session_labels(day: pd.Timestamp) -> list[pd.Timestamp]:
    """夜盤收盤標籤 16:00..次日 05:00（14 根），對齊 kline_engine.py 的分桶規則。"""
    base = day.normalize() + pd.Timedelta(hours=15)
    return [base + pd.Timedelta(minutes=60 * i) for i in range(1, 15)]


def _session_query_dates(day: pd.Timestamp) -> list[tuple[str, pd.Timestamp]]:
    """
    回傳 (Shioaji 查詢用的交易日字串, 收盤標籤) 清單，涵蓋 day 這個日曆日的
    日盤+夜盤全部理論收盤時間點。
    排除週末休市（週六與週日無當日開市的日盤與夜盤）。

    2026-08-25 實測驗證：Shioaji 的 ticks(date=X) 是「交易日」制，X 涵蓋
    「X 當天日盤 + X 前一天夜盤」；反過來說，某天日曆日的夜盤（15:00~次日
    05:00），要用「隔天」的日期去查才拿得到。日盤則直接用當天日期查即可。
    """
    result: list[tuple[str, pd.Timestamp]] = []
    weekday = day.dayofweek  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun

    # 只有週一至週五 (0..4) 才有日盤 (08:45 ~ 13:45)
    if 0 <= weekday <= 4:
        day_query = day.strftime("%Y-%m-%d")
        for label in _expected_day_session_labels(day):
            result.append((day_query, label))

    # 只有週一至週五 (0..4) 才有當天開出的夜盤 (15:00 ~ 次日05:00)
    # (週五夜盤開在週五 15:00，跨到週六 05:00 結束，查詢日是週六)
    if 0 <= weekday <= 4:
        night_query = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        for label in _expected_night_session_labels(day):
            result.append((night_query, label))

    return result


def find_missing_bars(
    product_code: str, *, lookback_days: int = 2
) -> list[tuple[str, pd.Timestamp]]:
    """
    列出最近 lookback_days 天內，已經收盤、但目前 store 裡缺的 K 棒。
    只檢查「已經過去」的收盤時間點（還沒開始/正在成型中的那一根不算缺）。
    回傳 (Shioaji 查詢用日期字串, 收盤標籤) 清單。
    """
    store = get_store(product_code)
    existing = {int(b["time"]) for b in store.get_klines(limit=0)}

    # 2026-08-30 修正：使用台北時間（Naive Asia/Taipei），不能用裸 pd.Timestamp.now()
    # 否則在 UTC 系統時區的 VM 上，now 會慢 8 小時，導致當天白天的 K 棒全被
    # 誤判成「未來的時間」而漏補。
    now = pd.Timestamp.now(TAIPEI_TZ).tz_localize(None)
    missing: list[tuple[str, pd.Timestamp]] = []
    for delta in range(lookback_days, -1, -1):
        day = now.normalize() - pd.Timedelta(days=delta)
        for query_date, label in _session_query_dates(day):
            if label >= now:
                continue
            key = to_unix_seconds(label)
            if key not in existing:
                missing.append((query_date, label))
    return missing


def _resample_ticks_to_bars(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    # 2026-08-30 修正：按時間排序，避免 API 回傳亂序時 open/close 抓錯第一筆/最後一筆
    df = df.sort_values("ts")
    df["bar_close"] = df["ts"].apply(bar_close_time_from_ts)
    df = df.dropna(subset=["bar_close"])
    if df.empty:
        return []
    grouped = df.groupby("bar_close").agg(
        open=("close", "first"),
        high=("close", "max"),
        low=("close", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    bars: list[dict] = []
    for bar_close, row in grouped.iterrows():
        bars.append(
            {
                "time": to_unix_seconds(bar_close),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
        )
    return bars


def backfill_via_shioaji(product_code: str, *, lookback_days: int = 2) -> int:
    """
    檢查缺口，有缺才連線 Shioaji 補；沒有缺口/沒設定金鑰/連線失敗都安靜跳過，
    回傳實際補進去的根數（0 不代表失敗，可能就是沒有缺口）。每一步都落地
    記一筆稽核 log（見 _append_audit 說明），失敗時才查得出卡在哪一步。
    """
    available = shioaji_available()
    if not available:
        _append_audit(f"product={product_code}\tskip=沒有可用的 SJ_API_KEY/SJ_SEC_KEY")
        return 0

    missing = find_missing_bars(product_code, lookback_days=lookback_days)
    if not missing:
        _append_audit(f"product={product_code}\tskip=沒有偵測到缺口")
        return 0

    _append_audit(f"product={product_code}\tmissing={len(missing)}\t開始連線查詢")
    logger.info("Shioaji 補缺：偵測到 %d 根缺口，準備連線查詢 %s", len(missing), product_code)

    try:
        import shioaji as sj
    except ImportError as exc:
        _append_audit(f"product={product_code}\tfail=shioaji 套件未安裝: {exc}")
        logger.warning("shioaji 套件未安裝，無法補缺口")
        return 0

    api = sj.Shioaji(simulation=not _is_production())
    added_total = 0
    try:
        try:
            # 2026-08-26 實測抓到：shioaji 1.7.3 的 login() 已經沒有
            # fetch_contract 這個參數了（舊版 1.5.x 才有，這裡曾經直接複製
            # 舊專案的呼叫方式導致 TypeError）。改用 comtypes 對照官方文件的
            # 做法在這裡行不通——shioaji 是 pip 套件，直接用
            # help(sj.Shioaji.login) 查證過目前真正的簽名。
            api.login(
                api_key=os.getenv("SJ_API_KEY", "").strip(),
                secret_key=os.getenv("SJ_SEC_KEY", "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            _append_audit(f"product={product_code}\tfail=login 失敗: {exc}")
            return 0

        try:
            # 只驗證過 TMF（微型台指期貨）近月連續代碼；其他商品先不處理，
            # 避免拿錯合約的資料混進去。
            # 2026-08-26：api.Contracts 在 shioaji 1.7.3 已標示 deprecated
            # （改用 api.contracts），但新版 contracts v2 的呼叫方式不是單純
            # 改小寫（api.contracts.futures 是方法不是命名空間，需要另外查證
            # 正確用法），這裡先維持用已驗證過真的能跑的舊路徑，deprecation
            # warning 先接受，等 shioaji 真的移除這個相容層再回來處理。
            contract = api.Contracts.Futures.TMF.TMFR1
        except Exception as exc:  # noqa: BLE001
            _append_audit(f"product={product_code}\tfail=取得合約失敗: {exc}")
            return 0

        query_dates = sorted({query_date for query_date, _ in missing})
        for query_date in query_dates:
            try:
                ticks = api.ticks(contract=contract, date=query_date)
            except Exception as exc:  # noqa: BLE001
                _append_audit(f"product={product_code}\tquery={query_date}\tfail=查詢失敗: {exc}")
                logger.warning("Shioaji 查詢 %s 失敗: %s", query_date, exc)
                continue
            df = pd.DataFrame(ticks.dict())
            if df.empty:
                _append_audit(f"product={product_code}\tquery={query_date}\tresult=空資料")
                continue
            bars = _resample_ticks_to_bars(df)
            if not bars:
                _append_audit(
                    f"product={product_code}\tquery={query_date}\t"
                    f"result=有 {len(df)} 筆 tick 但 resample 不出任何 K 棒（可能都不在交易時段內）"
                )
                continue
            store = get_store(product_code)
            added = store.merge_missing_bars(bars)
            added_total += added
            _append_audit(
                f"product={product_code}\tquery={query_date}\tticks={len(df)}\t"
                f"resampled={len(bars)}\tadded={added}"
            )
            logger.info("Shioaji 補缺：%s 查詢新增 %d 根", query_date, added)
    finally:
        try:
            api.logout()
        except Exception:  # noqa: BLE001
            pass

    _append_audit(f"product={product_code}\t完成\tadded_total={added_total}")
    return added_total
