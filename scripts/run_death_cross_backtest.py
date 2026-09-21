"""執行死叉做空策略（15分鐘K）Phase 1 回測，全量 TMFR1 歷史逐筆資料。

用法：
    python -m scripts.run_death_cross_backtest
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.backtest_engine import run_direction_limited_backtest
from backend.config import ContractSpec, DEFAULT_COST
from backend.indicators import add_moving_averages, resample_to_nmin
from backend.signals import generate_death_cross_signals

TMFR1_DIR = Path(__file__).resolve().parent.parent / "data" / "raw_tick" / "TMFR1"
CONTRACT = ContractSpec(name="TMF", point_value=10.0, tick_size=1.0)


def load_15min_bars() -> pd.DataFrame:
    daily_bars: list[pd.DataFrame] = []
    for path in sorted(TMFR1_DIR.glob("TMFR1_*.parquet")):
        day_df = pd.read_parquet(path, columns=["ts", "close", "volume"])
        day_df = day_df.rename(columns={"ts": "datetime"})
        day_df["open"] = day_df["close"]
        day_df["high"] = day_df["close"]
        day_df["low"] = day_df["close"]
        bars_day = resample_to_nmin(day_df, minutes=15)
        if not bars_day.empty:
            daily_bars.append(bars_day)
    if not daily_bars:
        raise SystemExit(f"找不到任何 TMFR1 逐筆資料於 {TMFR1_DIR}")
    return pd.concat(daily_bars).sort_index(kind="stable")


def main() -> None:
    bars = load_15min_bars()
    print(f"載入 15 分鐘K棒：{len(bars)} 根，區間 {bars.index[0]} ~ {bars.index[-1]}")

    enriched = add_moving_averages(bars, fast=5, mid=20, slow=60)
    signaled = generate_death_cross_signals(enriched)

    trades, summary = run_direction_limited_backtest(
        signaled,
        strategy="death_cross",
        direction_limit="short",
        contract=CONTRACT,
        cost=DEFAULT_COST,
        stop_loss_points=130.0,
        take_profit_points=130.0,
        reverse_signal_exit_enabled=False,
    )

    print("\n=== 死叉做空（15分K，SL/TP=130/130）回測結果 ===")
    print(f"總交易數：{summary['total_trades']}")
    print(f"勝率：{summary['win_rate'] * 100:.1f}%")
    print(f"總損益：{summary['total_net_pnl_ntd']:,.0f} 元")
    print(f"最大回撤：{summary['max_drawdown_ntd']:,.0f} 元")
    print(f"獲利因子：{summary['profit_factor']:.2f}")
    print(f"平均獲利：{summary['avg_win_ntd']:,.0f} 元")
    print(f"平均虧損：{summary['avg_loss_ntd']:,.0f} 元")


if __name__ == "__main__":
    main()
