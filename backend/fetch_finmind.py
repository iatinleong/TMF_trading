"""
fetch_finmind.py — 從 FinMind API 抓取台指期 (TXF) / 小台指 (MXF) 歷史分鐘K線資料

用法：
    python backend\\fetch_finmind.py --symbol TXF --start 2024-01-01 --end 2024-12-31
    python backend\\fetch_finmind.py --symbol MXF --start 2024-01-01 --end 2024-12-31

需求：
    專案根目錄下的 .env 檔案須包含 FINMIND_TOKEN=<您的免費API token>
    （於 https://finmindtrade.com 免費註冊取得）

輸出：
    data/<symbol>_1min_<start>_<end>.csv  （1分鐘K，欄位：datetime,open,high,low,close,volume）

注意：
    FinMind 免費帳號有每日 API 呼叫次數限制與歷史資料深度限制（通常僅近1-2年）。
    若請求區間超過免費額度所能提供的範圍，FinMind 會回傳空結果或部分資料，
    本程式會印出實際取得的資料起訖時間，方便判斷免費額度能回溯多深。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
DATASET = "TaiwanFuturesMinuteKBar"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def load_token() -> str:
    """從專案根目錄的 .env 讀取 FINMIND_TOKEN，若找不到則清楚報錯提示。"""
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        raise SystemExit(
            "找不到 FINMIND_TOKEN。請在 "
            f"{PROJECT_ROOT / '.env'} 中設定 FINMIND_TOKEN=<您的token>"
        )
    return token


def fetch_minute_kbar(symbol: str, start_date: str, end_date: str, token: str) -> pd.DataFrame:
    """向 FinMind v4 API 請求指定期貨商品的分鐘K線資料，回傳 DataFrame。"""
    params = {
        "dataset": DATASET,
        "data_id": symbol,
        "start_date": start_date,
        "end_date": end_date,
        "token": token,
    }
    response = requests.get(FINMIND_API_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()

    status = payload.get("status")
    if status != 200:
        raise RuntimeError(f"FinMind API 回傳錯誤 status={status}, msg={payload.get('msg')}")

    records = payload.get("data", [])
    if not records:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(records)
    # FinMind 欄位通常為: date, open, max, min, close, Trading_Volume, ...
    # 統一轉成我們專案內部使用的欄位命名
    rename_map = {
        "date": "datetime",
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume",
        "volume": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    required = ["datetime", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"FinMind 回傳資料缺少必要欄位: {missing}. 實際欄位: {list(df.columns)}")

    df = df[required].copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="從 FinMind 抓取台指期 / 小台指 歷史分鐘K線")
    parser.add_argument("--symbol", choices=["TXF", "MXF"], required=True, help="TXF=台指期(大台), MXF=小台指")
    parser.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="結束日期 YYYY-MM-DD")
    args = parser.parse_args()

    token = load_token()
    print(f"[fetch_finmind] 請求 {args.symbol} 分鐘K, {args.start} ~ {args.end} ...")

    df = fetch_minute_kbar(args.symbol, args.start, args.end, token)

    if df.empty:
        print(
            "[fetch_finmind] 警告：回傳資料為空。可能原因："
            "(1) 免費額度無法回溯這麼久遠的歷史 (2) 該區間無交易日 (3) API次數已用完。"
        )
        sys.exit(1)

    actual_start = df["datetime"].min()
    actual_end = df["datetime"].max()
    print(f"[fetch_finmind] 實際取得資料範圍: {actual_start} ~ {actual_end}, 共 {len(df)} 筆")

    if pd.Timestamp(args.start) < actual_start:
        print(
            f"[fetch_finmind] 注意：您要求的起始日 {args.start} 早於實際可取得的最早資料 {actual_start}，"
            "代表免費額度的歷史深度僅能回溯至此。"
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{args.symbol}_1min_{args.start}_{args.end}.csv"
    df.to_csv(out_path, index=False)
    print(f"[fetch_finmind] 已寫入: {out_path}")


if __name__ == "__main__":
    main()
