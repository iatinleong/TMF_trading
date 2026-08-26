"""
analyze_intrabar_signals.py — 用真實逐筆資料驗證「盤中暫定MA穿越」與「收盤確認穿越」的差異

目的：回答使用者的疑問「盤中MA真的有穿越，難道要無視嗎？」
方法：
    對每一根60分鐘K棒，重播該K棒時間窗內的每一筆逐筆成交，把「當前最新成交價」
    當作這根未收盤K棒的暫定收盤價，即時算出 暫定5MA / 暫定20MA（沿用已收盘的
    前4/19根K棒收盤價 + 暫定值）。
    追蹤這根K棒進行期間，(暫定5MA - 暫定20MA) 的正負號變化次數：
        - 0次變化：盤中沒有出現「反悔」，暫定訊號與收盤訊號一致
        - >=1次變化：盤中至少出現一次「暫時穿越後又縮回」的假訊號（若逐筆進場，
          代表你可能在同一根K棒內被騙進場、平倉、甚至反向再進場多次）
    另外統計：若採用「逐筆即時穿越」進場邏輯，在這批資料中總共會比「60分鐘收盤
    確認」多觸發幾次訊號、其中有幾次最終跟收盤時的官方訊號方向不一致（=完全的假訊號）。

使用方式：
    python -m backend.analyze_intrabar_signals --symbol MTX
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

try:
    from .config import StrategyConfig
    from .load_taifex_tick import RAW_TICK_DIR, _load_and_filter
except ImportError:  # pragma: no cover
    from config import StrategyConfig  # type: ignore
    from load_taifex_tick import RAW_TICK_DIR, _load_and_filter  # type: ignore

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_all_ticks(symbol: str) -> pd.DataFrame:
    frames = []
    for csv_path in sorted(RAW_TICK_DIR.glob("Daily_*.csv")):
        day_df = _load_and_filter(csv_path, symbol)
        if not day_df.empty:
            frames.append(day_df)
    if not frames:
        raise RuntimeError(f"No cached tick files found for {symbol} under {RAW_TICK_DIR}")
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)


def analyze(symbol: str, fast: int = 5, mid: int = 20) -> None:
    bars = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"]).set_index("datetime")
    bars = bars.sort_index()
    ticks = _load_all_ticks(symbol)

    bar_closes = bars.index.to_list()
    closes = bars["close"].to_list()

    n_bars_examined = 0
    n_bars_with_flip = 0
    n_official_signal_bars = 0          # bars where close-confirmed 5MA/20MA relation flips vs prior bar
    n_extra_intrabar_signals = 0        # intrabar sign flips that do NOT match the eventual close-confirmed relation
    example_rows = []

    prev_official_sign = None
    for i in range(max(fast, mid), len(bar_closes)):
        bar_end = bar_closes[i]
        bar_start = bar_closes[i - 1]

        hist_fast = closes[i - fast : i]      # last `fast` CLOSED bars before this one (not including this bar)
        hist_mid = closes[i - mid : i]

        official_ma_fast = (sum(hist_fast[1:]) + closes[i]) / fast
        official_ma_mid = (sum(hist_mid[1:]) + closes[i]) / mid
        official_sign = 1 if official_ma_fast > official_ma_mid else -1

        bar_ticks = ticks[(ticks["datetime"] > bar_start) & (ticks["datetime"] <= bar_end)]
        if bar_ticks.empty:
            prev_official_sign = official_sign
            continue

        n_bars_examined += 1
        signs_seen = []
        for price in bar_ticks["close"]:
            prov_ma_fast = (sum(hist_fast[1:]) + price) / fast
            prov_ma_mid = (sum(hist_mid[1:]) + price) / mid
            signs_seen.append(1 if prov_ma_fast > prov_ma_mid else -1)

        n_flips = sum(1 for a, b in zip(signs_seen, signs_seen[1:]) if a != b)
        if n_flips > 0:
            n_bars_with_flip += 1

        is_official_signal_bar = prev_official_sign is not None and official_sign != prev_official_sign
        if is_official_signal_bar:
            n_official_signal_bars += 1

        # Count intrabar sign values that would have fired a "phantom" entry but do not
        # match what actually happens at bar close relative to the prior bar.
        distinct_signs = set(signs_seen) | {official_sign}
        if len(distinct_signs) > 1 or (n_flips > 0 and not is_official_signal_bar):
            n_extra_intrabar_signals += 1
            if len(example_rows) < 5:
                example_rows.append(
                    {
                        "bar_end": bar_end,
                        "n_ticks": len(bar_ticks),
                        "n_sign_flips_intrabar": n_flips,
                        "official_sign_at_close": "多頭(5>20)" if official_sign == 1 else "空頭(5<20)",
                        "official_signal_confirmed": is_official_signal_bar,
                    }
                )

        prev_official_sign = official_sign

    print(f"=== {symbol}  60分K：盤中暫定MA穿越 vs 收盤確認穿越 ===")
    print(f"檢視K棒數（扣除MA暖身期）: {n_bars_examined}")
    print(f"官方（收盤確認）訊號K棒數: {n_official_signal_bars}")
    print(f"盤中出現過至少1次暫定正負號翻轉的K棒數: {n_bars_with_flip}")
    print(f"若逐筆即時反應會多出的『假／不穩定』訊號K棒數: {n_extra_intrabar_signals}")
    print()
    print("範例（前5筆有假訊號嫌疑的K棒）:")
    for row in example_rows:
        print(f"  {row}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare intrabar provisional MA crossings vs bar-close-confirmed crossings using real tick data.")
    parser.add_argument("--symbol", required=True, choices=("TX", "MTX"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    analyze(args.symbol)


if __name__ == "__main__":
    main()
