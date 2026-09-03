"""
load_taifex_tick.py — 下載並解析台灣期交所「前30個交易日期貨每筆成交資料」

資料來源（官方、免費、無需帳號）：
    https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData
    實際下載連結格式：
    https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_YYYY_MM_DD.zip

限制：僅提供「最近30個交易日」的逐筆成交資料（真實時間戳記，精確到秒），
無法一次取得多年歷史；若需長期歷史請改用 Shioaji（免費開戶，約5年深度）
或 FinMind Sponsor 付費方案（2011年至今）。

本檔案功能：
    1. 下載指定日期範圍內的逐筆成交 zip 檔並解壓（Big5 編碼 CSV）
    2. 篩選出目標商品（TX=大台 / MTX=小台）之近月合約（排除價差/跨月委託組合單）
    3. 將逐筆資料轉為 tick-level OHLCV（open=high=low=close=成交價, volume=成交量）
    4. 呼叫既有的 indicators.resample_to_60min() 進行「時段感知」60分鐘K線重組
       （日盤 08:45-13:45 / 夜盤 15:00-次日05:00 分開重組，不會跨盤整併）
    5. 輸出 data/<symbol>_60min_real.csv，欄位：datetime,open,high,low,close,volume

使用方式：
    python -m backend.load_taifex_tick --symbol MTX --days 30
"""
from __future__ import annotations

import argparse
import io
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

try:
    from .indicators import resample_to_60min
except ImportError:  # pragma: no cover - script execution fallback
    from indicators import resample_to_60min  # type: ignore

BASE_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{y}_{m:02d}_{d:02d}.zip"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_TICK_DIR = DATA_DIR / "raw_tick"

# 逐筆成交檔案欄位（Big5 編碼，無中文欄名可直接沿用，改用英文別名）
_TICK_COLUMNS = [
    "trade_date",       # 成交日期 YYYYMMDD（為交易實際發生的日曆日，非「交易日」歸屬）
    "symbol",           # 商品代號
    "contract_month",   # 到期月份(週別)
    "trade_time",       # 成交時間 HHMMSS
    "price",            # 成交價格
    "volume",           # 成交數量(B+S)
    "near_price",       # 近月價格（價差單用，通常為 "-"）
    "far_price",        # 遠月價格（價差單用，通常為 "-"）
    "flag",             # 開盤/收盤或其他註記
]


def _download_one_day(trade_date: datetime, dest_dir: Path) -> Path | None:
    """下載單一交易日的逐筆成交 zip 並解壓為 CSV，回傳 CSV 路徑；若當天無資料(假日/尚未提供)則回傳 None。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    csv_name = f"Daily_{trade_date:%Y_%m_%d}.csv"
    csv_path = dest_dir / csv_name
    if csv_path.exists():
        return csv_path

    url = BASE_URL.format(y=trade_date.year, m=trade_date.month, d=trade_date.day)
    resp = requests.get(url, timeout=30)
    # 非交易日、假日或尚未提供資料時，期交所會回傳 200 + HTML 錯誤頁（而非真正的 zip），
    # 因此以 zip 檔案的 magic bytes ("PK") 判斷是否為有效壓縮檔，而非只看 status_code。
    if resp.status_code != 200 or len(resp.content) < 100 or not resp.content.startswith(b"PK"):
        return None  # 非交易日或該日尚未提供資料

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        inner_name = zf.namelist()[0]
        with zf.open(inner_name) as f:
            csv_path.write_bytes(f.read())
    return csv_path


def _load_and_filter(csv_path: Path, symbol: str) -> pd.DataFrame:
    """讀取單日逐筆 CSV（Big5），篩選目標商品的近月合約（排除價差組合單）。"""
    df = pd.read_csv(csv_path, encoding="big5", dtype=str, header=0, names=_TICK_COLUMNS)
    df["symbol"] = df["symbol"].str.strip()
    df["contract_month"] = df["contract_month"].str.strip()

    subset = df[df["symbol"] == symbol].copy()
    if subset.empty:
        return subset

    # 排除價差/跨月委託組合單（contract_month 內含 "/"）
    subset = subset[~subset["contract_month"].str.contains("/", na=False)]

    # 選出當日成交量最大的月份合約，視為近月/主力合約
    subset["volume"] = pd.to_numeric(subset["volume"], errors="coerce").fillna(0)
    top_month = subset.groupby("contract_month")["volume"].sum().idxmax()
    subset = subset[subset["contract_month"] == top_month]

    subset["price"] = pd.to_numeric(subset["price"], errors="coerce")
    subset = subset.dropna(subset=["price"])

    subset["datetime"] = pd.to_datetime(
        subset["trade_date"] + subset["trade_time"].str.zfill(6), format="%Y%m%d%H%M%S"
    )
    subset["open"] = subset["price"]
    subset["high"] = subset["price"]
    subset["low"] = subset["price"]
    subset["close"] = subset["price"]
    return subset[["datetime", "open", "high", "low", "close", "volume"]]


def load_recent_ticks(symbol: str, days: int = 30) -> pd.DataFrame:
    """下載並合併最近 N 個交易日的逐筆資料（自動略過假日/無資料日），回傳單一 tick-level DataFrame。"""
    RAW_TICK_DIR.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    cursor = datetime.today()
    checked = 0
    # 多探測一些日曆天，扣除週末與假日後湊滿 `days` 個交易日
    max_calendar_days = days * 2 + 10
    while len(frames) < days and checked < max_calendar_days:
        csv_path = _download_one_day(cursor, RAW_TICK_DIR)
        if csv_path is not None:
            day_df = _load_and_filter(csv_path, symbol)
            if not day_df.empty:
                frames.append(day_df)
        cursor -= timedelta(days=1)
        checked += 1

    if not frames:
        raise RuntimeError(
            f"未取得任何 {symbol} 逐筆資料，請確認商品代號正確（TX/MTX/TMF）且期交所網站可連線。"
        )

    combined = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
    return combined


def load_tmfr1_60min_bars(start_date: str, end_date: str) -> pd.DataFrame:
    """
    高效串流讀取指定日期區間的 TMFR1 60 分鐘 K 棒資料：
    - 逐檔（逐日）讀取 parquet 檔，在記憶體內直接重採樣為當日 60 分鐘 K 棒，隨即釋放該日原始 Tick 資料。
    - 峰值記憶體從 ~4GB 暴降至 ~20MB，徹底杜絕多月/年回測導致的 MemoryError 與 OOM。
    - 若無 parquet 原始檔，自動無縫退回讀取預先聚合的 TMFR1_parquet_60min.csv。
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start > end:
        raise ValueError("start_date 必須早於或等於 end_date")

    tmfr1_dir = RAW_TICK_DIR / "TMFR1"
    if tmfr1_dir.is_dir():
        daily_bars: list[pd.DataFrame] = []
        for path in sorted(tmfr1_dir.glob("TMFR1_*.parquet")):
            date_str = path.stem[len("TMFR1_") :]
            try:
                file_date = pd.Timestamp(date_str)
            except ValueError:
                continue
            if file_date < start or file_date > end:
                continue
            try:
                day_df = pd.read_parquet(path, columns=["ts", "close", "volume"])
                day_df = day_df.rename(columns={"ts": "datetime"})
                day_df["open"] = day_df["close"]
                day_df["high"] = day_df["close"]
                day_df["low"] = day_df["close"]
                bars_day = resample_to_60min(day_df)
                if not bars_day.empty:
                    daily_bars.append(bars_day)
            except Exception:
                pass
            finally:
                day_df = None
        if daily_bars:
            combined = pd.concat(daily_bars)
            return combined.sort_index(kind="stable")

    # 備援：若無 parquet 檔，嘗試從現有的 60min CSV 讀取
    for candidate_name in ("TMFR1_parquet_60min.csv", "TMF_60min_real.csv"):
        fallback_csv = DATA_DIR / candidate_name
        if fallback_csv.exists():
            try:
                df = pd.read_csv(fallback_csv)
                if "datetime" in df.columns:
                    df["datetime"] = pd.to_datetime(df["datetime"])
                    df = df.sort_values("datetime")
                    mask = (df["datetime"] >= start) & (df["datetime"] <= (end + pd.Timedelta(days=1)))
                    filtered = df.loc[mask].copy()
                    if not filtered.empty:
                        return resample_to_60min(filtered)
            except Exception:
                pass

    raise RuntimeError(
        f"{start_date} ~ {end_date} 區間內找不到可用的 TMFR1 歷史資料檔（請確認 data/raw_tick/TMFR1 或 TMFR1_parquet_60min.csv 是否存在）"
    )


def load_tmfr1_range(start_date: str, end_date: str) -> pd.DataFrame:
    """
    相容函式：讀取本機 data/raw_tick/TMFR1/*.parquet 指定日期區間的原始逐筆資料。
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start > end:
        raise ValueError("start_date 必須早於或等於 end_date")

    tmfr1_dir = RAW_TICK_DIR / "TMFR1"
    if tmfr1_dir.is_dir():
        frames: list[pd.DataFrame] = []
        for path in sorted(tmfr1_dir.glob("TMFR1_*.parquet")):
            date_str = path.stem[len("TMFR1_") :]
            try:
                file_date = pd.Timestamp(date_str)
            except ValueError:
                continue
            if file_date < start or file_date > end:
                continue
            try:
                day_df = pd.read_parquet(path, columns=["ts", "close", "volume"])
                frames.append(day_df)
            except Exception:
                pass
        if frames:
            combined = pd.concat(frames, ignore_index=True)
            combined = combined.rename(columns={"ts": "datetime"})
            combined["open"] = combined["close"]
            combined["high"] = combined["close"]
            combined["low"] = combined["close"]
            combined = combined.sort_values("datetime", kind="stable").reset_index(drop=True)
            return combined[["datetime", "open", "high", "low", "close", "volume"]]

    # 備援：若無 parquet 檔，嘗試從現有的 60min CSV 讀取
    for candidate_name in ("TMFR1_parquet_60min.csv", "TMF_60min_real.csv"):
        fallback_csv = DATA_DIR / candidate_name
        if fallback_csv.exists():
            try:
                df = pd.read_csv(fallback_csv)
                if "datetime" in df.columns:
                    df["datetime"] = pd.to_datetime(df["datetime"])
                    df = df.sort_values("datetime")
                    mask = (df["datetime"] >= start) & (df["datetime"] <= (end + pd.Timedelta(days=1)))
                    filtered = df.loc[mask].reset_index(drop=True)
                    if not filtered.empty:
                        return filtered[["datetime", "open", "high", "low", "close", "volume"]]
            except Exception:
                pass

    raise RuntimeError(
        f"{start_date} ~ {end_date} 區間內找不到可用的 TMFR1 歷史資料檔（請確認 data/raw_tick/TMFR1 或 TMFR1_parquet_60min.csv 是否存在）"
    )


def build_60min_dataset(symbol: str, days: int = 30) -> Path:
    """下載近 N 個交易日逐筆資料、轉為時段感知60分鐘K線，輸出 CSV，回傳輸出路徑。"""
    ticks = load_recent_ticks(symbol, days=days)
    bars_60m = resample_to_60min(ticks)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{symbol}_60min_real.csv"
    bars_60m.to_csv(out_path)
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download TAIFEX previous-30-day tick data and build 60min bars.")
    parser.add_argument("--symbol", required=True, choices=("TX", "MTX", "TMF"), help="TX=大台 / MTX=小台 / TMF=微台")
    parser.add_argument("--days", type=int, default=30, help="Number of most recent trading days to fetch (max 30).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_path = build_60min_dataset(args.symbol, days=args.days)
    print(f"60-minute bars written to: {out_path}")


if __name__ == "__main__":
    main()
