"""
load_taifex.py — 解析台灣期交所（TAIFEX）官方每日行情下載CSV，組成連續日K序列

背景／已知限制：
    TAIFEX官網免費下載的「期貨每日交易行情」只提供「日」頻率資料，並非60分鐘K。
    若要真正的60分鐘K，仍需券商API（如永豐Shioaji，2020-03至今）或FinMind付費Sponsor方案
    （逐筆資料，2011至今）。本模組僅處理TAIFEX官方免費日K，供「日線策略雛型驗證」使用；
    若要跑原本60分K策略，必須改用其他資料源（本專案backend/fetch_finmind.py、或未來
    Shioaji整合）取得分鐘級資料後再resample。

TAIFEX官方CSV已知特性（已透過使用者實際下載檔案驗證）：
    1. 編碼為 Big5（繁體中文傳統編碼），非UTF-8，需以 encoding='big5' 讀取，否則欄位/中文亂碼。
    2. 同一天會有「多筆」資料，原因有兩個維度：
       (a) 同一商品同時有多個「到期月份(週別)」合約掛牌（近月、次月、季月、或小台的週合約 W1/W2）。
       (b) 同一到期月份合約，交易時段(交易時段欄位)分「一般」(日盤)與「盤後」(夜盤) 兩筆。
    3. 「盤後」列所歸屬的交易日 D，實際上是「前一營業日 15:00 起、至 D 當日 05:00 止」的夜盤區段，
       也就是說：同一交易日D的「盤後」列，時間上其實發生在「一般」列之前（盤後先、日盤後）。
       若要組成單一連續日K，應以：
           open  = 盤後(夜盤) 的開盤價 (若無盤後資料則用一般的開盤價)
           close = 一般(日盤) 的收盤價 (若無一般資料則用盤後的收盤價)
           high  = max(兩時段 high)
           low   = min(兩時段 low)
           volume= 兩時段 volume 相加
       這是本模組 `_combine_sessions` 函式的依據。
    4. 無成交的合約列，開高低收會顯示 "-"，應視為缺值並剔除（volume=0）。
    5. 同一天可能有多個合約月份都有成交量：本模組採用「每日成交量最大者為主力合約」
       (常見的連續期貨/主力合約判定慣例) 組成連續日K序列，而非簡單串接近月合約，
       這樣可以避免近月合約在到期前流動性枯竭、跳空劇烈的雜訊。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_COLUMNS = {
    "交易日期": "trade_date",
    "契約": "product",
    "到期月份(週別)": "contract_month",
    "開盤價": "open",
    "最高價": "high",
    "最低價": "low",
    "收盤價": "close",
    "成交量": "volume",
    "交易時段": "session",
}


def _read_taifex_csv(path: Path) -> pd.DataFrame:
    """讀取單一 TAIFEX 官方每日行情 CSV（Big5編碼），回傳整理過的 DataFrame。"""
    df = pd.read_csv(path, encoding="big5", dtype=str)
    df.columns = [c.strip() for c in df.columns]

    missing = [c for c in RAW_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: 缺少預期欄位 {missing}. 實際欄位: {list(df.columns)}")

    df = df.rename(columns=RAW_COLUMNS)
    df = df[list(RAW_COLUMNS.values())].copy()

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col].str.replace(",", "", regex=False), errors="coerce")

    df["trade_date"] = pd.to_datetime(df["trade_date"].str.replace("/", "-", regex=False))
    df["session"] = df["session"].str.strip()
    return df


def _combine_sessions(day_group: pd.DataFrame) -> pd.Series | None:
    """將同一(日期,合約月份)下的「一般」(日盤)與「盤後」(夜盤)列合併成單一日K。

    盤後列時間上早於一般列（見模組docstring第3點），故 open 取盤後、close 取一般；
    若缺其中一段，則退回只用有資料的那一段。
    """
    day_group = day_group[day_group["volume"] > 0]
    if day_group.empty:
        return None

    day_row = day_group[day_group["session"] == "一般"]
    night_row = day_group[day_group["session"] == "盤後"]

    open_price = (
        night_row["open"].iloc[0] if not night_row.empty and pd.notna(night_row["open"].iloc[0])
        else (day_row["open"].iloc[0] if not day_row.empty else None)
    )
    close_price = (
        day_row["close"].iloc[0] if not day_row.empty and pd.notna(day_row["close"].iloc[0])
        else (night_row["close"].iloc[0] if not night_row.empty else None)
    )
    if open_price is None or close_price is None:
        return None

    return pd.Series(
        {
            "open": open_price,
            "high": day_group["high"].max(),
            "low": day_group["low"].min(),
            "close": close_price,
            "volume": day_group["volume"].sum(),
        }
    )


def build_continuous_daily_series(raw: pd.DataFrame, product: str) -> pd.DataFrame:
    """從多檔TAIFEX原始資料組成單一商品的連續日K序列（以每日成交量最大合約為主力合約）。"""
    subset = raw[raw["product"] == product].copy()
    if subset.empty:
        raise ValueError(f"找不到商品代碼 '{product}' 的資料，請確認 --product 參數。")

    combined_rows = []
    for (trade_date, contract_month), day_contract_group in subset.groupby(["trade_date", "contract_month"]):
        combined = _combine_sessions(day_contract_group)
        if combined is None:
            continue
        combined["trade_date"] = trade_date
        combined["contract_month"] = contract_month
        combined_rows.append(combined)

    if not combined_rows:
        raise ValueError(f"商品 '{product}' 沒有任何有效（有成交量）的資料列。")

    per_contract_daily = pd.DataFrame(combined_rows)

    # 每日挑選成交量最大的合約作為主力/連續合約
    continuous = (
        per_contract_daily.sort_values("volume", ascending=False)
        .groupby("trade_date", as_index=False)
        .first()
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    continuous = continuous.rename(columns={"trade_date": "date"})
    return continuous[["date", "open", "high", "low", "close", "volume", "contract_month"]]


def load_taifex_files(paths: list[Path], product: str) -> pd.DataFrame:
    """讀取一或多個 TAIFEX 原始CSV檔案，合併後組成指定商品的連續日K序列。"""
    frames = [_read_taifex_csv(p) for p in paths]
    raw = pd.concat(frames, ignore_index=True)
    return build_continuous_daily_series(raw, product)


def main() -> None:
    parser = argparse.ArgumentParser(description="解析TAIFEX官方每日行情CSV，組成連續日K序列")
    parser.add_argument("--input", nargs="+", required=True, help="一或多個TAIFEX原始CSV檔案路徑")
    parser.add_argument("--product", required=True, choices=["TX", "MTX", "TMF"], help="商品代碼")
    parser.add_argument("--output", required=True, help="輸出日K CSV路徑")
    args = parser.parse_args()

    paths = [Path(p) for p in args.input]
    result = load_taifex_files(paths, args.product)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)

    print(f"[load_taifex] 商品={args.product}, 輸出 {len(result)} 筆日K -> {output_path}")
    print(f"[load_taifex] 日期範圍: {result['date'].min().date()} ~ {result['date'].max().date()}")
    print(result.head())


if __name__ == "__main__":
    main()
