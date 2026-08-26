"""補齊 台指期量化/data/raw_tick/TMFR1 的缺口交易日（Shioaji 逐筆）。

金鑰/登入沿用 台股量化 專案的 .env + src/shioaji_client.py（本專案 .env 沒有 Shioaji 金鑰）。
輸出寫入本專案 data/raw_tick/TMFR1，schema 與既有的 Shioaji 原始檔一致
（code, date, ts, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type）。

用法（本專案根目錄執行）:
  python scripts/backfill_tmfr1_ticks_shioaji.py
  python scripts/backfill_tmfr1_ticks_shioaji.py --start 2025-04-28 --end 2025-04-28
  python scripts/backfill_tmfr1_ticks_shioaji.py --max-mb 420
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROJECT = ROOT.parent / "台股量化"
sys.path.insert(0, str(SOURCE_PROJECT / "src"))

from shioaji_client import login, usage_mb  # noqa: E402

GAP_START = date(2025, 4, 28)  # 已知缺口起點（前一天 2025-04-25 已存在）
OUT_DIR = ROOT / "data" / "raw_tick" / "TMFR1"
STATE_PATH = OUT_DIR / "_state_shioaji_backfill.json"


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def last_weekday_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"done": [], "empty": [], "failed": []}


def save_state(state: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def day_path(d: date) -> Path:
    return OUT_DIR / f"TMFR1_{d.isoformat()}.parquet"


def remaining_mb(api) -> float:
    return api.usage().remaining_bytes / 1024 / 1024


def ticks_to_df(ticks) -> pd.DataFrame:
    df = pd.DataFrame(ticks.dict())
    if df.empty:
        return df
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def fetch_one(api, contract, d: date) -> pd.DataFrame:
    ticks = api.ticks(contract=contract, date=d.isoformat())
    return ticks_to_df(ticks)


def main() -> None:
    parser = argparse.ArgumentParser(description="補齊 TMFR1 缺口逐筆（Shioaji）")
    parser.add_argument("--start", default="", help="YYYY-MM-DD，預設 2025-04-28")
    parser.add_argument("--end", default="", help="YYYY-MM-DD，預設昨天（略過週末）")
    parser.add_argument("--max-mb", type=float, default=420.0)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--retry", type=int, default=3)
    parser.add_argument(
        "--reverse", action="store_true", help="由新到舊回補（預設由舊到新）"
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else GAP_START
    end = (
        date.fromisoformat(args.end)
        if args.end
        else last_weekday_on_or_before(date.today() - timedelta(days=1))
    )
    if end < start:
        raise SystemExit(f"end {end} < start {start}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    done = set(state.get("done", []))
    empty = set(state.get("empty", []))
    failed = set(state.get("failed", []))

    days = [d for d in daterange(start, end) if is_weekday(d)]
    todo = []
    for d in days:
        key = d.isoformat()
        if day_path(d).exists():
            done.add(key)
            continue
        if key in empty:
            continue
        todo.append(d)
    if args.reverse:
        todo.reverse()
    state["done"] = sorted(done)
    save_state(state)

    print("=== TMFR1 缺口補齊 (Shioaji, 寫入台指期量化) ===")
    print(f"金鑰來源: {SOURCE_PROJECT / '.env'}")
    print(f"輸出目錄: {OUT_DIR}")
    print(f"區間: {start} ~ {end} | 工作日總數: {len(days)} | 待下載: {len(todo)}")
    print()

    api = login(fetch_contract=True)
    try:
        contract = api.Contracts.Futures.TMF.TMFR1
        print("合約:", contract)
        print(usage_mb(api))
        print()

        ok_n = empty_n = fail_n = rows_total = 0
        t0 = time.time()

        for i, d in enumerate(todo, 1):
            rem = remaining_mb(api)
            if rem < args.max_mb:
                print(f"\n[停止] 剩餘流量 {rem:.1f} MB < 門檻 {args.max_mb} MB")
                break

            key = d.isoformat()
            path = day_path(d)

            df = None
            err = None
            for attempt in range(1, args.retry + 1):
                try:
                    df = fetch_one(api, contract, d)
                    err = None
                    break
                except Exception as e:
                    err = e
                    print(f"  [{key}] 失敗 attempt {attempt}/{args.retry}: {e}")
                    time.sleep(1.0 * attempt)

            if err is not None:
                fail_n += 1
                failed.add(key)
                state["failed"] = sorted(failed)
                save_state(state)
                print(f"[{i}/{len(todo)}] {key} FAILED")
                time.sleep(args.sleep)
                continue

            assert df is not None
            n = len(df)
            if n == 0:
                empty_n += 1
                empty.add(key)
                failed.discard(key)
                state["empty"] = sorted(empty)
                state["failed"] = sorted(failed)
                save_state(state)
                print(f"[{i}/{len(todo)}] {key} empty | rem={remaining_mb(api):.1f}MB")
            else:
                df.insert(0, "code", "TMFR1")
                df.insert(1, "date", key)
                df.to_parquet(path, index=False)
                ok_n += 1
                rows_total += n
                done.add(key)
                failed.discard(key)
                state["done"] = sorted(done)
                state["failed"] = sorted(failed)
                save_state(state)
                print(
                    f"[{i}/{len(todo)}] {key} {n:>7,} ticks -> {path.name} | "
                    f"rem={remaining_mb(api):.1f}MB"
                )

            time.sleep(args.sleep)

        elapsed = time.time() - t0
        print()
        print("=== 本輪結束 ===")
        print(f"成功: {ok_n} | 空日: {empty_n} | 失敗: {fail_n} | 新增筆數: {rows_total:,}")
        print(f"累計完成: {len(done)} | 空日: {len(empty)} | 失敗: {len(failed)}")
        print(f"耗時: {elapsed:.1f}s")
        print(usage_mb(api))
        print("續傳：再執行同一指令即可跳過已完成日期")
    finally:
        try:
            api.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
